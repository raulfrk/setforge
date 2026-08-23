"""Native Codex configuration, instruction, and skill resource adapters."""

from __future__ import annotations

import os
import stat
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import tomlkit
from tomlkit.container import Container
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Item

from setforge.config import (
    CodexConfigRef,
    CodexFileScope,
    CodexHttpMcpServerRef,
    CodexInstructionRef,
    CodexMcpServerRef,
    CodexSkillRef,
    CodexSkillScope,
    CodexStdioMcpServerRef,
    Config,
    ResolvedProfile,
    TrackedFile,
    TreeOrphanPolicy,
    TreePolicy,
)
from setforge.errors import ConfigError, SetforgeError

type TomlPath = tuple[str, ...]


class CodexResourceError(SetforgeError):
    """A Codex resource cannot be reconciled safely."""


class CodexTomlConflict(CodexResourceError):
    """Live and tracked content changed the same managed TOML leaf."""


@dataclass(frozen=True, slots=True)
class CodexResourcePath:
    """A validated portable source and its native Codex destination."""

    source: Path
    destination: Path
    project: Path | None = None


@dataclass(frozen=True, slots=True)
class CodexConfigPlan:
    """Frozen reconciliation inputs for one native Codex config file."""

    resource_id: str
    destination: Path
    project: Path | None
    sources: tuple[Path, ...]
    source_bytes: tuple[bytes, ...]
    generated_bytes: tuple[bytes, ...]
    mcp_marker_id: str | None
    desired: bytes
    base: bytes | None
    live: bytes | None
    result: bytes

    @property
    def changed(self) -> bool:
        return self.live != self.result


_MCP_MARKER_PREFIX = "codex/mcp-target/"


def mcp_target_marker(destination: Path, project: Path | None) -> str:
    scope = "project" if project is not None else "user"
    encoded = urlsafe_b64encode(str(destination).encode()).decode()
    return f"{_MCP_MARKER_PREFIX}{scope}/{encoded}"


def _decode_mcp_target_marker(marker: str) -> tuple[Path, Path | None]:
    suffix = marker.removeprefix(_MCP_MARKER_PREFIX)
    scope, separator, encoded = suffix.partition("/")
    if not separator or scope not in {"user", "project"}:
        raise CodexResourceError(f"malformed Codex MCP target marker: {marker}")
    try:
        destination = Path(urlsafe_b64decode(encoded.encode()).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise CodexResourceError(
            f"malformed Codex MCP target marker: {marker}"
        ) from exc
    if not destination.is_absolute():
        raise CodexResourceError(f"relative Codex MCP target marker: {marker}")
    project = destination.parent.parent if scope == "project" else None
    if project is not None:
        safe_destination = _safe_destination(project, Path(".codex/config.toml"))
        if destination != safe_destination:
            raise CodexResourceError(f"malformed project MCP target marker: {marker}")
    else:
        safe_destination = _safe_destination(codex_home(), Path("config.toml"))
        if destination != safe_destination:
            raise CodexResourceError(f"stale user MCP target marker: {marker}")
    return safe_destination, project


def codex_home() -> Path:
    """Return the canonical Codex home without accepting a relative override."""
    raw = os.environ.get("CODEX_HOME")
    home = Path(raw).expanduser() if raw else Path.home() / ".codex"
    if not home.is_absolute():
        raise CodexResourceError("CODEX_HOME must be an absolute path")
    return home.resolve(strict=False)


def _source(repo_root: Path, relative: Path) -> Path:
    tracked = (repo_root / "tracked").resolve(strict=True)
    candidate = tracked / relative
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CodexResourceError(f"Codex source does not exist: {candidate}") from exc
    if resolved != tracked and tracked not in resolved.parents:
        raise CodexResourceError(
            f"Codex source escapes the tracked directory: {relative}"
        )
    return resolved


def _project(
    project: Path | None, project_paths: Mapping[str, Path] | None = None
) -> Path:
    if project is None:
        raise CodexResourceError("project-scoped Codex resource has no project")
    try:
        host_project = (project_paths or {}).get(str(project), project)
        resolved = host_project.expanduser().resolve(strict=True)
    except OSError as exc:
        raise CodexResourceError(f"Codex project does not exist: {project}") from exc
    if not resolved.is_dir():
        raise CodexResourceError(f"Codex project is not a directory: {resolved}")
    return resolved


def _parse(text: str, *, label: str) -> Container:
    try:
        document = tomlkit.parse(text)
    except (TOMLKitError, ValueError, TypeError) as exc:
        raise CodexResourceError(f"malformed TOML in {label}: {exc}") from exc
    return document


def project_is_trusted(project: Path, *, home: Path | None = None) -> bool:
    """Read Codex's native trust declaration; never create or change it."""
    config_path = (home or codex_home()) / "config.toml"
    try:
        descriptor = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CodexResourceError(
            f"Codex trust configuration is not a regular file: {config_path}"
        ) from exc
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise CodexResourceError(
                f"Codex trust configuration is not a regular file: {config_path}"
            )
        payload = stream.read()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexResourceError(
            f"Codex trust configuration is not UTF-8: {config_path}"
        ) from exc
    document = _parse(text, label=str(config_path))
    projects = document.get("projects")
    if not isinstance(projects, Mapping):
        return False
    canonical = str(project.resolve(strict=True))
    declaration = projects.get(canonical)
    return (
        isinstance(declaration, Mapping) and declaration.get("trust_level") == "trusted"
    )


def _trusted_project(
    project: Path | None, project_paths: Mapping[str, Path] | None = None
) -> Path:
    resolved = _project(project, project_paths)
    if not project_is_trusted(resolved):
        trust_config = codex_home() / "config.toml"
        raise CodexResourceError(
            f"Codex project is not trusted in {trust_config}: {resolved}"
        )
    return resolved


def _safe_destination(root: Path, relative: Path) -> Path:
    """Resolve a native destination without following scoped child symlinks."""
    destination = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise CodexResourceError(
                f"Codex destination contains a symbolic link: {current}"
            )
    return destination


def resolve_config(
    ref: CodexConfigRef,
    repo_root: Path,
    project_paths: Mapping[str, Path] | None = None,
) -> CodexResourcePath:
    project = (
        None
        if ref.scope is CodexFileScope.USER
        else _trusted_project(ref.project, project_paths)
    )
    root = codex_home() if project is None else project
    relative = Path("config.toml") if project is None else Path(".codex/config.toml")
    destination = _safe_destination(root, relative)
    return CodexResourcePath(_source(repo_root, ref.source), destination, project)


def resolve_mcp_destination(
    ref: CodexMcpServerRef,
    project_paths: Mapping[str, Path] | None = None,
) -> tuple[Path, Path | None]:
    """Resolve one MCP declaration to its native Codex config destination."""
    project = (
        None
        if ref.scope is CodexFileScope.USER
        else _trusted_project(ref.project, project_paths)
    )
    root = codex_home() if project is None else project
    relative = Path("config.toml") if project is None else Path(".codex/config.toml")
    return _safe_destination(root, relative), project


def _render_common_mcp(
    server: MutableMapping[str, object], ref: CodexMcpServerRef
) -> None:
    server["enabled"] = ref.enabled
    server["required"] = ref.required
    optional = {
        "startup_timeout_sec": ref.startup_timeout_sec,
        "tool_timeout_sec": ref.tool_timeout_sec,
        "enabled_tools": ref.enabled_tools or None,
        "disabled_tools": ref.disabled_tools or None,
        "default_tools_approval_mode": (
            ref.default_tools_approval_mode.value
            if ref.default_tools_approval_mode is not None
            else None
        ),
    }
    for key, value in optional.items():
        if value is not None:
            server[key] = value
    if ref.tools:
        tools = tomlkit.table()
        for tool_name, policy in ref.tools.items():
            tool = tomlkit.table()
            tool["approval_mode"] = policy.approval_mode.value
            tools[tool_name] = tool
        server["tools"] = tools


def render_mcp_server(
    name: str,
    ref: CodexMcpServerRef,
    environment_vars: Mapping[str, str],
) -> bytes:
    """Render one value-free portable declaration as native Codex TOML."""
    document = tomlkit.document()
    servers = tomlkit.table()
    server = tomlkit.table()
    if isinstance(ref, CodexStdioMcpServerRef):
        server["command"] = ref.command
        if ref.args:
            server["args"] = ref.args
        if ref.cwd is not None:
            server["cwd"] = str(ref.cwd)
        if ref.env_vars:
            server["env_vars"] = [environment_vars[value] for value in ref.env_vars]
    elif isinstance(ref, CodexHttpMcpServerRef):
        server["url"] = ref.url
        if ref.bearer_token_env_var is not None:
            server["bearer_token_env_var"] = environment_vars[ref.bearer_token_env_var]
        if ref.env_http_headers:
            server["env_http_headers"] = {
                header: environment_vars[value]
                for header, value in ref.env_http_headers.items()
            }
    _render_common_mcp(server, ref)
    servers[name] = server
    document["mcp_servers"] = servers
    return tomlkit.dumps(document).encode("utf-8")


def _mcp_destination_refs(
    config: Config, resolved: ResolvedProfile
) -> Iterator[tuple[str, CodexMcpServerRef]]:
    """Yield selected refs plus safely locatable prior-ownership destinations."""
    if resolved.codex is None or config.codex is None:
        return
    selected = set(resolved.codex.mcp_servers)
    for name, ref in config.codex.mcp_servers.items():
        locator = None if ref.project is None else str(ref.project)
        if (
            name in selected
            or ref.scope is CodexFileScope.USER
            or locator in config._codex_project_paths
        ):
            yield name, ref


def resolve_instruction(
    ref: CodexInstructionRef,
    repo_root: Path,
    project_paths: Mapping[str, Path] | None = None,
) -> CodexResourcePath:
    project = (
        None
        if ref.scope is CodexFileScope.USER
        else _trusted_project(ref.project, project_paths)
    )
    root = codex_home() if project is None else project
    destination = _safe_destination(root, Path("AGENTS.md"))
    source = _source(repo_root, ref.source)
    if not source.is_file():
        raise CodexResourceError(
            f"Codex instruction source is not a regular file: {source}"
        )
    return CodexResourcePath(source, destination, project)


def resolve_skill(
    name: str,
    ref: CodexSkillRef,
    repo_root: Path,
    project_paths: Mapping[str, Path] | None = None,
) -> CodexResourcePath:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ConfigError(f"unsafe Codex skill name: {name!r}")
    project = (
        None
        if ref.scope is CodexSkillScope.USER
        else _trusted_project(ref.project, project_paths)
    )
    scope_root = codex_home() if project is None else project
    relative_root = Path("skills") if project is None else Path(".agents/skills")
    root = _safe_destination(scope_root, relative_root)
    source = _source(repo_root, ref.source)
    if not source.is_dir():
        raise CodexResourceError(f"Codex skill source is not a directory: {source}")
    return CodexResourcePath(source, _safe_destination(root, Path(name)), project)


def expand_filesystem_resources(
    config: Config, resolved: ResolvedProfile, repo_root: Path
) -> None:
    """Expose selected instructions and skills to the shared file lifecycle."""
    selected = resolved.codex
    if selected is None:
        return
    registry = config.codex
    if registry is None:  # Defensive: reference validation normally catches this.
        raise CodexResourceError("Codex profile has no Codex resource registry")
    entries: list[tuple[str, TrackedFile]] = []
    for name in selected.instructions:
        instruction_ref = registry.instructions[name]
        paths = resolve_instruction(
            instruction_ref, repo_root, config._codex_project_paths
        )
        entries.append(
            (
                f"codex.instruction.{name}",
                TrackedFile(src=instruction_ref.source, dst=str(paths.destination)),
            )
        )
    for name in selected.skills:
        skill_ref = registry.skills[name]
        paths = resolve_skill(name, skill_ref, repo_root, config._codex_project_paths)
        entries.append(
            (
                f"codex.skill.{name}",
                TrackedFile(
                    src=skill_ref.source,
                    dst=str(paths.destination),
                    tree=TreePolicy(orphans=TreeOrphanPolicy.REMOVE_OWNED),
                ),
            )
        )
    for resource_id, tracked in entries:
        prior = config.tracked_files.get(resource_id)
        if prior is not None and prior != tracked:
            raise CodexResourceError(
                f"Codex resource id collides with tracked file {resource_id!r}"
            )
        config.tracked_files[resource_id] = tracked
        if resource_id not in resolved.tracked_files:
            resolved.tracked_files.append(resource_id)


def _regular_bytes(path: Path, *, absent_ok: bool) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if absent_ok:
            return None
        raise CodexResourceError(f"Codex resource does not exist: {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise CodexResourceError(f"Codex resource is not a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CodexResourceError(f"cannot read Codex resource {path}: {exc}") from exc


def plan_config_resources(  # noqa: C901 - one pass freezes all destination inputs
    config: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    *,
    read_base: Callable[[str], bytes | None],
    stored_ids: tuple[str, ...] = (),
    reconcile: bool = True,
) -> tuple[CodexConfigPlan, ...]:
    """Freeze selected fragments, merge bases, live bytes, and results.

    ``read_base`` is injected by the lifecycle boundary so this leaf adapter
    remains independent of the host-state store and is easy to verify.
    """
    selected = resolved.codex
    registry = config.codex
    if selected is not None and registry is None:
        raise CodexResourceError("Codex profile has no Codex resource registry")
    grouped: dict[Path, tuple[Path | None, list[Path], list[bytes]]] = {}
    for name in selected.config if selected is not None else ():
        assert registry is not None
        paths = resolve_config(
            registry.config[name], repo_root, config._codex_project_paths
        )
        project, sources, _generated = grouped.setdefault(
            paths.destination, (paths.project, [], [])
        )
        if project != paths.project:  # pragma: no cover - destination invariant
            raise CodexResourceError("Codex destination has inconsistent project scope")
        sources.append(paths.source)
    selected_mcp = set(selected.mcp_servers) if selected is not None else set()
    for name, ref in (
        _mcp_destination_refs(config, resolved) if selected is not None else ()
    ):
        destination, mcp_project = resolve_mcp_destination(
            ref, config._codex_project_paths
        )
        project, _sources, generated = grouped.setdefault(
            destination, (mcp_project, [], [])
        )
        if project != mcp_project:  # pragma: no cover - destination invariant
            raise CodexResourceError("Codex destination has inconsistent MCP scope")
        if name in selected_mcp:
            generated.append(
                render_mcp_server(name, ref, config._codex_environment_vars)
            )
    for marker in stored_ids:
        if not marker.startswith(_MCP_MARKER_PREFIX):
            continue
        destination, marker_project = _decode_mcp_target_marker(marker)
        if marker_project is not None and not project_is_trusted(marker_project):
            raise CodexResourceError(
                "Codex project is not trusted for prior MCP ownership: "
                f"{marker_project}"
            )
        grouped.setdefault(destination, (marker_project, [], []))
    plans: list[CodexConfigPlan] = []
    for destination, (project, sources, generated) in grouped.items():
        tracked_bytes = tuple(
            payload
            for source in sources
            if (payload := _regular_bytes(source, absent_ok=False)) is not None
        )
        source_bytes = tracked_bytes
        desired, owned = compose_fragments((*tracked_bytes, *generated))
        if project is None and any(path[:1] == ("projects",) for path in owned):
            raise CodexResourceError(
                "managed Codex config fragments cannot own native project trust"
            )
        digest = sha256(str(destination).encode("utf-8")).hexdigest()[:16]
        resource_id = f"codex/config/{digest}"
        base = read_base(resource_id)
        live = _regular_bytes(destination, absent_ok=True)
        result = (
            reconcile_toml(base=base, live=live, desired=desired)
            if reconcile
            else (live if live is not None else desired)
        )
        plans.append(
            CodexConfigPlan(
                resource_id=resource_id,
                destination=destination,
                project=project,
                sources=tuple(sources),
                source_bytes=source_bytes,
                generated_bytes=tuple(generated),
                mcp_marker_id=(
                    mcp_target_marker(destination, project) if generated else None
                ),
                desired=desired,
                base=base,
                live=live,
                result=result,
            )
        )
    return tuple(plans)


def assert_config_plan_current(plan: CodexConfigPlan) -> None:
    """Refuse a source or live-state change after plan/confirmation."""
    current_sources = tuple(
        payload
        for source in plan.sources
        if (payload := _regular_bytes(source, absent_ok=False)) is not None
    )
    if current_sources != plan.source_bytes:
        raise CodexResourceError("Codex config fragments changed after planning; retry")
    if plan.project is not None and not project_is_trusted(plan.project):
        raise CodexResourceError(
            f"Codex project trust changed after planning: {plan.project}"
        )
    if _regular_bytes(plan.destination, absent_ok=True) != plan.live:
        raise CodexResourceError("live Codex config changed after planning; retry")


def config_target_roots(
    config: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    *,
    stored_ids: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    """Return descriptor-lock roots for selected native config destinations."""
    selected = resolved.codex
    registry = config.codex
    if selected is not None and registry is None:
        raise CodexResourceError("Codex profile has no Codex resource registry")
    registry_config = registry.config if registry is not None else {}
    roots = {
        resolve_config(
            registry_config[name], repo_root, config._codex_project_paths
        ).destination.parent
        for name in (selected.config if selected is not None else ())
    }
    roots.update(
        resolve_mcp_destination(ref, config._codex_project_paths)[0].parent
        for _name, ref in (
            _mcp_destination_refs(config, resolved) if selected is not None else ()
        )
    )
    roots.update(
        destination.parent
        for marker in stored_ids
        if marker.startswith(_MCP_MARKER_PREFIX)
        for destination, _project in (_decode_mcp_target_marker(marker),)
    )
    return tuple(sorted(roots, key=str))


def selected_trusted_projects(
    config: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    *,
    stored_ids: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    """Freeze every selected project whose native resources require trust."""
    selected = resolved.codex
    registry = config.codex
    if selected is not None and registry is None:
        raise CodexResourceError("Codex profile has no Codex resource registry")
    registry_config = registry.config if registry is not None else {}
    registry_instructions = registry.instructions if registry is not None else {}
    registry_skills = registry.skills if registry is not None else {}
    projects = {
        *(
            paths.project
            for name in (selected.config if selected is not None else ())
            if (
                paths := resolve_config(
                    registry_config[name], repo_root, config._codex_project_paths
                )
            ).project
            is not None
        ),
        *(
            project
            for _name, ref in (
                _mcp_destination_refs(config, resolved) if selected is not None else ()
            )
            if (project := resolve_mcp_destination(ref, config._codex_project_paths)[1])
            is not None
        ),
        *(
            paths.project
            for name in (selected.instructions if selected is not None else ())
            if (
                paths := resolve_instruction(
                    registry_instructions[name], repo_root, config._codex_project_paths
                )
            ).project
            is not None
        ),
        *(
            paths.project
            for name in (selected.skills if selected is not None else ())
            if (
                paths := resolve_skill(
                    name, registry_skills[name], repo_root, config._codex_project_paths
                )
            ).project
            is not None
        ),
    }
    projects.update(
        project
        for marker in stored_ids
        if marker.startswith(_MCP_MARKER_PREFIX)
        for _destination, project in (_decode_mcp_target_marker(marker),)
        if project is not None
    )
    return tuple(
        sorted((project for project in projects if project is not None), key=str)
    )


def assert_projects_trusted(projects: tuple[Path, ...]) -> None:
    """Refuse publication after any selected project's trust is revoked."""
    for project in projects:
        if not project_is_trusted(project):
            raise CodexResourceError(
                f"Codex project trust changed after planning: {project}"
            )


def apply_config_plan(
    plan: CodexConfigPlan,
    *,
    write: Callable[[Path, bytes], object],
    record_base: Callable[[str, bytes], object],
    record_marker: Callable[[str, bytes], object] | None = None,
) -> bool:
    """Apply one frozen plan atomically and advance its managed merge base."""
    assert_config_plan_current(plan)
    if plan.changed:
        write(plan.destination, plan.result)
    record_base(plan.resource_id, plan.desired)
    if plan.mcp_marker_id is not None and record_marker is not None:
        record_marker(plan.mcp_marker_id, b"")
    return plan.changed


def capture_config_plan(
    plan: CodexConfigPlan, *, write: Callable[[Path, bytes], object]
) -> bool:
    """Capture managed live leaves back to their owning source fragments."""
    assert_config_plan_current(plan)
    if plan.live is None:
        raise CodexResourceError(f"live Codex config is absent: {plan.destination}")
    changed = False
    for source, tracked in zip(plan.sources, plan.source_bytes, strict=True):
        captured = capture_toml(tracked=tracked, live=plan.live)
        if captured != tracked:
            write(source, captured)
            changed = True
    return changed


def config_plan_matches_live(plan: CodexConfigPlan) -> bool:
    """Return whether every managed fragment leaf already matches live."""
    if plan.live is None:
        return False
    try:
        projected = reconcile_toml(base=plan.base, live=plan.live, desired=plan.desired)
    except CodexTomlConflict:
        return False
    return projected == plan.live


def _leaves(
    node: Mapping[str, object], prefix: TomlPath = ()
) -> Iterator[tuple[TomlPath, object]]:
    for key, value in node.items():
        path = (*prefix, key)
        if isinstance(value, Mapping):
            yield from _leaves(value, path)
        else:
            yield path, value


def compose_fragments(
    fragments: tuple[bytes, ...],
) -> tuple[bytes, frozenset[TomlPath]]:
    """Compose fragments, rejecting two declarations of the same leaf."""
    output = tomlkit.document()
    owned: set[TomlPath] = set()
    for index, payload in enumerate(fragments):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodexResourceError(
                f"Codex TOML fragment {index} is not UTF-8"
            ) from exc
        fragment = _parse(text, label=f"Codex fragment {index}")
        for path, value in _leaves(fragment):
            if any(
                path == prior
                or path[: len(prior)] == prior
                or prior[: len(path)] == path
                for prior in owned
            ):
                raise CodexTomlConflict(
                    f"multiple Codex fragments claim TOML key {'.'.join(path)}"
                )
            _set(output, path, value)
            owned.add(path)
    return tomlkit.dumps(output).encode(), frozenset(owned)


def _plain(value: object) -> object:
    return value.unwrap() if isinstance(value, Item) else value


def _get(document: Mapping[str, object], path: TomlPath) -> tuple[bool, object]:
    node: object = document
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, _plain(node)


def _set(document: MutableMapping[str, object], path: TomlPath, value: object) -> None:
    node = document
    for part in path[:-1]:
        existing = node.get(part)
        if existing is None:
            node[part] = tomlkit.table()
            existing = node[part]
        if not isinstance(existing, MutableMapping):
            raise CodexTomlConflict(f"TOML key {'.'.join(path[:-1])} is not a table")
        node = existing
    node[path[-1]] = value


def _delete(document: MutableMapping[str, object], path: TomlPath) -> None:
    ancestors: list[tuple[MutableMapping[str, object], str]] = []
    node = document
    for part in path[:-1]:
        existing = node.get(part)
        if not isinstance(existing, MutableMapping):
            return
        ancestors.append((node, part))
        node = existing
    node.pop(path[-1], None)
    for parent, part in reversed(ancestors):
        child = parent.get(part)
        if isinstance(child, Mapping) and not child:
            parent.pop(part, None)
        else:
            break


def reconcile_toml(*, base: bytes | None, live: bytes | None, desired: bytes) -> bytes:
    """Three-way reconcile desired leaves into live, preserving all other keys."""
    desired_doc = _parse(desired.decode("utf-8"), label="desired Codex config")
    base_doc = _parse((base or b"").decode("utf-8"), label="Codex merge base")
    live_doc = _parse((live or b"").decode("utf-8"), label="live Codex config")
    desired_leaves = dict(_leaves(desired_doc))
    base_leaves = dict(_leaves(base_doc))
    for path in sorted(set(desired_leaves) | set(base_leaves)):
        wanted_present = path in desired_leaves
        wanted = desired_leaves.get(path)
        base_present, old = _get(base_doc, path)
        live_present, current = _get(live_doc, path)
        wanted_plain = _plain(wanted) if wanted_present else None
        live_changed = live_present != base_present or current != old
        tracked_changed = wanted_present != base_present or wanted_plain != old
        converged = live_present == wanted_present and (
            not wanted_present or current == wanted_plain
        )
        if live_changed and tracked_changed and not converged:
            raise CodexTomlConflict(
                f"live and tracked Codex config both changed {'.'.join(path)}"
            )
        if wanted_present:
            _set(live_doc, path, wanted)
        else:
            _delete(live_doc, path)
    return tomlkit.dumps(live_doc).encode("utf-8")


def capture_toml(*, tracked: bytes, live: bytes) -> bytes:
    """Copy only tracked-owned leaves from live into a tracked fragment."""
    tracked_doc = _parse(tracked.decode("utf-8"), label="tracked Codex config")
    live_doc = _parse(live.decode("utf-8"), label="live Codex config")
    for path, _value in tuple(_leaves(tracked_doc)):
        present, value = _get(live_doc, path)
        if not present:
            _delete(tracked_doc, path)
        else:
            _set(tracked_doc, path, value)
    return tomlkit.dumps(tracked_doc).encode("utf-8")
