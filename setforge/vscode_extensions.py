"""VSCode extension reconcile, driven by the ``code`` CLI.

All subprocess invocations honor the locked hygiene rules: the ``code``
binary is resolved via :func:`setforge.binaries.resolve_binary` (which
walks CLI flag → env var → host-local config → PATH), raising
:class:`ExtensionToolMissing` if every layer comes up empty.
``subprocess.run`` always uses ``check=True, text=True,
capture_output=True, timeout=30``, and args are always a list with no
``shell=True``.

Also exposes YAML-edit helpers used by the ``ext`` subcommand group to
mutate a profile's ``extensions.include`` / ``extensions.exclude`` lists
in ``setforge.yaml`` without losing comments.
"""

import hashlib
import hmac
import io
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import (
    CommentedMap,
    CommentedSeq,
)

from setforge.atomicio import atomic_write_text
from setforge.binaries import resolve_binary, stderr_of
from setforge.config import (
    Config,
    Extensions,
    ReconcilePolicy,
    load_config,
    resolve_profile,
)
from setforge.errors import (
    ConfigError,
    ExtensionInstallFailed,
    ExtensionToolMissing,
    ProfileNotFound,
)
from setforge.provision import driver
from setforge.provision.protocol import Identity, Outcome, ProvisionItem
from setforge.provision.resolve.protocol import ResolvedPin

LOGGER: logging.Logger = logging.getLogger(__name__)

_CODE_BIN = "code"
_TIMEOUT_S = 30
# Extension IDs are publisher.name where each part is alphanum + hyphens.
# Real IDs commonly carry uppercase letters (e.g. `GitHub.copilot`,
# `VisualStudioExptTeam.vscodeintellicode`), so the character class is
# case-insensitive — a lowercase-only filter would silently drop those
# installed extensions. The Remote-SSH `code` CLI prepends an
# "Extensions installed on SSH: <ip>:" header line (it has spaces and a
# colon) which this regex still rejects.
_EXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*\.[A-Za-z0-9][A-Za-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Summary of what reconcile did (or would do for REPORT / dry_run).

    ``failed`` lists ``(ext_id, error_msg)`` tuples for individual
    install/uninstall calls that failed; the rest of the loop continues
    so one bad extension doesn't abort the whole reconcile.
    """

    policy: ReconcilePolicy
    to_install: list[str]
    to_uninstall: list[str]
    dry_run: bool
    failed: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.to_install or self.to_uninstall)


def _ensure_code() -> str:
    """Resolve the ``code`` binary via :func:`resolve_binary` or raise."""
    path = resolve_binary(_CODE_BIN)
    if path is None:
        raise ExtensionToolMissing(
            f"{_CODE_BIN!r} CLI not found on PATH; install VSCode "
            "or open a terminal in a VSCode session. "
            f"Tip: set 'binaries.{_CODE_BIN}' in "
            "~/.config/setforge/local.yaml to override."
        )
    return str(path)


def list_installed() -> set[str]:
    """Return the set of currently-installed extension IDs.

    Lines that don't match the ``publisher.name`` extension-ID format are
    silently dropped — the Remote-SSH ``code`` CLI emits a header line
    (``"Extensions installed on SSH: <ip>:"``) on stdout before the IDs.

    Raises :class:`ExtensionInstallFailed` if the underlying ``code``
    invocation exits non-zero or times out.
    """
    code = _ensure_code()
    try:
        result = subprocess.run(
            [code, "--list-extensions"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        raise ExtensionInstallFailed(
            f"`code --list-extensions` failed: {stderr_of(exc)}"
        ) from exc
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if _EXT_ID_RE.match(line.strip())
    }


def reconcile(
    ext: Extensions,
    *,
    dry_run: bool = False,
    pins: dict[str, ResolvedPin] | None = None,
) -> ReconcileReport:
    """Reconcile installed VSCode extensions to the declared set.

    - effective_declared = ``include - exclude`` (exclude always wins).
    - ``ADDITIVE``: install missing only; never uninstall.
    - ``PRUNE``: install missing AND uninstall everything not declared
      (extras the user added, plus any explicit ``exclude`` entries that
      happen to be installed).
    - ``REPORT``: compute both diffs, run no subprocess install/uninstall,
      return a non-empty report.

    ``dry_run=True`` logs intended actions without invoking subprocess.

    ``pins`` (from a loaded ``setforge.lock``) maps a casefolded ``publisher.name``
    to its :class:`ResolvedPin`; a pinned extension takes the BYTE-STRONG install
    path (download + verify the locked VSIX). Without pins, or for an extension
    with no pin, install falls back to ``code --install-extension <id>`` unchanged.
    """
    from setforge.provision.extension import ExtensionProvisioner

    code = _ensure_code()
    # `exclude` always wins, and VSCode treats extension IDs
    # case-insensitively, so subtract on casefolded keys — otherwise a
    # `github.copilot` exclude would silently fail to drop an included
    # `GitHub.copilot`, defeating the documented invariant.
    exclude_keys = {e.casefold() for e in ext.exclude}
    effective = {i for i in ext.include if i.casefold() not in exclude_keys}

    items = [
        ProvisionItem(type="extension", identity=Identity(key=e.casefold(), display=e))
        for e in effective
    ]
    report_only = dry_run or ext.reconcile is ReconcilePolicy.REPORT
    result = driver.reconcile(
        ExtensionProvisioner(pins=pins),
        items,
        policy=ReconcilePolicy.ADDITIVE,
        report_only=report_only,
    )
    to_install = sorted(i.display for i in result.delta.installed)
    failed: list[tuple[str, str]] = [
        (o.item.identity.display, o.detail)
        for o in result.outcomes
        if o.outcome is Outcome.HARD
    ]

    if ext.reconcile is ReconcilePolicy.ADDITIVE:
        to_uninstall: list[str] = []
    else:
        installed = list_installed()
        installed_by_key = {e.casefold(): e for e in installed}
        effective_keys = {e.casefold() for e in effective}
        to_uninstall = sorted(
            display
            for key, display in installed_by_key.items()
            if key not in effective_keys
        )

    if ext.reconcile is ReconcilePolicy.REPORT or dry_run:
        return ReconcileReport(
            policy=ext.reconcile,
            to_install=to_install,
            to_uninstall=to_uninstall,
            dry_run=dry_run,
        )

    if ext.reconcile is ReconcilePolicy.PRUNE:
        for name in to_uninstall:
            LOGGER.info("uninstalling extension: %s", name)
            try:
                subprocess.run(
                    [code, "--uninstall-extension", name],
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=_TIMEOUT_S,
                )
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                OSError,
            ) as exc:
                msg = stderr_of(exc)
                LOGGER.warning("uninstall failed for %s: %s", name, msg)
                failed.append((name, msg))

    return ReconcileReport(
        policy=ext.reconcile,
        to_install=to_install,
        to_uninstall=to_uninstall,
        dry_run=False,
        failed=failed,
    )


def install_one(ext_id: str, *, pin: ResolvedPin | None = None) -> None:
    """Install a single extension via ``code --install-extension``.

    Without ``pin`` (no lock, or no lock entry for this extension), runs the
    marketplace-id install unchanged: ``code --install-extension <publisher.name>``.

    With ``pin`` (the lock pins this extension), takes the BYTE-STRONG path:
    download the VSIX for the pinned CONCRETE version, verify its sha256 against
    the locked integrity (fail-closed — a mismatch NEVER installs), write the
    verified bytes to a temp ``.vsix``, and install THAT file path so ``code``
    consumes exactly the locked bytes rather than re-resolving the latest.

    Raises :class:`ExtensionInstallFailed` on non-zero exit, timeout, a hash
    mismatch, or a download failure (with the captured stderr in the message).
    """
    if pin is not None:
        _install_pinned(ext_id, pin)
        return
    _run_install(ext_id, ext_id)


def _run_install(ext_id: str, install_arg: str) -> None:
    """Run ``code --install-extension <install_arg>`` (an id OR a .vsix path).

    ``ext_id`` names the extension in error messages; ``install_arg`` is what
    ``code`` actually installs.
    """
    code = _ensure_code()
    try:
        subprocess.run(
            [code, "--install-extension", install_arg],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        raise ExtensionInstallFailed(
            f"install of {ext_id!r} failed: {stderr_of(exc)}"
        ) from exc


def _install_pinned(ext_id: str, pin: ResolvedPin) -> None:
    """Download + verify the pinned VSIX, then install it from a temp file.

    Fail-closed: the sha256 of the downloaded bytes must equal the locked
    integrity BEFORE any ``code --install-extension`` runs; a mismatch raises
    and installs nothing. The download itself goes through the guarded fetch
    (HTTPS-only, timeout, wire cap) inside :func:`download_vsix`.

    Dependency-ordering note: a VSIX whose manifest declares an as-yet
    uninstalled dependency extension can make ``code`` exit non-zero; that
    surfaces cleanly as :class:`ExtensionInstallFailed` (dep resolution is not
    solved here) rather than crashing.
    """
    from setforge.provision.resolve.extension import _split_ext_id, download_vsix

    publisher, name = _split_ext_id(ext_id)
    try:
        data = download_vsix(publisher, name, pin.version)
    except Exception as exc:  # ResolveError (guarded fetch) or malformed id
        raise ExtensionInstallFailed(
            f"install of {ext_id!r} failed: could not download the pinned "
            f"VSIX ({pin.version}): {exc}"
        ) from exc
    _verify_vsix_hash(ext_id, data, pin)
    with _temp_vsix(data) as vsix_path:
        _run_install(ext_id, str(vsix_path))


def _verify_vsix_hash(ext_id: str, data: bytes, pin: ResolvedPin) -> None:
    """Fail-closed sha256 check of downloaded VSIX bytes against the locked hash.

    Uses :func:`hmac.compare_digest` (constant-time, same discipline as
    :func:`setforge.provision.installer._verify_checksum`). Any mismatch, or a
    non-``sha256:`` integrity, raises :class:`ExtensionInstallFailed` so the
    caller never hands unverified bytes to ``code``.
    """
    algo, _, expected_hex = pin.integrity.partition(":")
    if algo != "sha256" or not expected_hex:
        raise ExtensionInstallFailed(
            f"install of {ext_id!r} failed: unsupported lock integrity "
            f"{pin.integrity!r}; expected 'sha256:<hex>'"
        )
    actual_hex = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_hex, expected_hex.strip().lower()):
        raise ExtensionInstallFailed(
            f"install of {ext_id!r} failed: VSIX checksum mismatch — the "
            f"downloaded {pin.version} bytes do not match the locked hash "
            f"(expected sha256 {expected_hex}, got {actual_hex}); not installing"
        )


@contextmanager
def _temp_vsix(data: bytes) -> Iterator[Path]:
    """Yield a Path to a temp file holding ``data`` with a ``.vsix`` suffix.

    ``code --install-extension`` only treats a path as a VSIX when it ends in
    ``.vsix``, so the suffix is mandatory. The file is unlinked on BOTH the
    success and the exception path (``finally``), so a failed install leaks no
    temp file.
    """
    fd, raw_path = tempfile.mkstemp(prefix="setforge-ext-", suffix=".vsix")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        yield path
    finally:
        path.unlink(missing_ok=True)


def uninstall_one(ext_id: str) -> None:
    """Uninstall a single extension via ``code --uninstall-extension``.

    Raises :class:`ExtensionInstallFailed` on non-zero exit or timeout,
    with the captured stderr in the message.
    """
    code = _ensure_code()
    try:
        subprocess.run(
            [code, "--uninstall-extension", ext_id],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        raise ExtensionInstallFailed(
            f"uninstall of {ext_id!r} failed: {stderr_of(exc)}"
        ) from exc


def _load_yaml_doc(config_path: Path) -> tuple[YAML, CommentedMap]:
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    yaml = YAML(typ="rt")
    # Match the indent style used in setforge.yaml so list edits don't
    # noisy-reformat the rest of the file (lists indented under their key).
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml, yaml.load(fh)


def _dump_yaml_doc(yaml: YAML, doc: CommentedMap, config_path: Path) -> None:
    """Atomically serialize ``doc`` back to ``config_path``.

    Dumps to an in-memory buffer first, then writes via
    :func:`atomicio.atomic_write_text` (write-temp + fsync + os.replace)
    so a crash, SIGTERM, disk-full, or ruamel serialization error
    mid-dump can never truncate the live ``setforge.yaml`` — the single
    source of truth for every profile / tracked-file. The file's
    permission bits are preserved across the replace.
    """
    buf = io.StringIO()
    yaml.dump(doc, buf)
    mode = config_path.stat().st_mode & 0o777 if config_path.exists() else None
    atomic_write_text(config_path, buf.getvalue(), mode=mode)


def _profile_extensions_block(doc: CommentedMap, profile: str) -> CommentedMap:
    """Return the profile's ``extensions:`` block, creating it if absent."""
    profiles = doc.get("profiles")
    if profiles is None or profile not in profiles:
        raise ProfileNotFound(f"profile not found: {profile}")
    profile_block = profiles[profile]
    if "extensions" not in profile_block:
        profile_block["extensions"] = CommentedMap()
    return profile_block["extensions"]


def _ensure_list(block: CommentedMap, key: str) -> CommentedSeq:
    if key not in block:
        block[key] = CommentedSeq()
    return block[key]


def add_to_include(config_path: Path, profile: str, ext_id: str) -> bool:
    """Append ``ext_id`` to ``profiles.<profile>.extensions.include`` in
    ``config_path``. Idempotent: returns ``False`` if already present.

    Comments and key order in the YAML document are preserved via
    ruamel.yaml round-trip mode.

    Raises :class:`ConfigError` if ``ext_id`` is in this profile's
    literal ``exclude`` list — or in any ancestor profile's ``exclude``
    via the ``extends:`` chain — since "exclude wins" (exclude is merged
    across the chain) would silently drop the new addition on the next
    reconcile.
    """
    cfg = load_config(config_path)
    if profile not in cfg.profiles:
        raise ProfileNotFound(f"profile not found: {profile}")
    if ext_id in cfg.profiles[profile].extensions.exclude:
        raise ConfigError(
            f"{ext_id!r} is in {profile}.extensions.exclude — remove it from "
            "exclude first (e.g. by editing setforge.yaml) before adding to include"
        )
    excluding_ancestor = _ancestor_excluding(cfg, profile, ext_id)
    if excluding_ancestor is not None:
        raise ConfigError(
            f"{ext_id!r} is in inherited profile {excluding_ancestor!r}'s "
            f"exclude list — remove it from {excluding_ancestor}.extensions.exclude "
            "first (e.g. by editing setforge.yaml) before adding to include, since "
            "the merged 'exclude wins' would silently drop the addition on reconcile"
        )
    yaml, doc = _load_yaml_doc(config_path)
    ext_block = _profile_extensions_block(doc, profile)
    include = _ensure_list(ext_block, "include")
    if ext_id in include:
        return False
    include.append(ext_id)
    _dump_yaml_doc(yaml, doc, config_path)
    return True


def _ancestor_declaring(cfg: Config, profile: str, ext_id: str) -> str | None:
    """Walk the extends: chain of ``profile`` (excluding ``profile`` itself)
    and return the first ancestor whose literal ``include`` lists ``ext_id``,
    or ``None``."""
    current = cfg.profiles[profile].extends
    while current is not None:
        if current not in cfg.profiles:
            return None
        if ext_id in cfg.profiles[current].extensions.include:
            return current
        current = cfg.profiles[current].extends
    return None


def _ancestor_excluding(cfg: Config, profile: str, ext_id: str) -> str | None:
    """Walk the extends: chain of ``profile`` (excluding ``profile`` itself)
    and return the first ancestor whose literal ``exclude`` lists ``ext_id``,
    or ``None``. Mirrors :func:`_ancestor_declaring` for the exclude side:
    merged exclude "always wins", so an inherited exclude would silently drop
    an addition to a child's ``include`` on reconcile."""
    current = cfg.profiles[profile].extends
    while current is not None:
        if current not in cfg.profiles:
            return None
        if ext_id in cfg.profiles[current].extensions.exclude:
            return current
        current = cfg.profiles[current].extends
    return None


def capture_extensions(config_path: Path, profile: str) -> bool:
    """Replace ``profiles.<profile>.extensions.include`` with the current
    installed set minus the resolved profile's ``exclude``.

    Per locked decision (spec § Locked implementation decisions #7):
    capture only ever edits ``include``. ``exclude`` is never auto-touched
    — to remove something from the declared set, use ``setforge ext remove``.

    Returns ``True`` iff the YAML changed. Comments and key order survive
    via ruamel.yaml round-trip.
    """
    cfg = load_config(config_path)
    resolved = resolve_profile(cfg, profile)
    installed = list_installed()
    # Subtract `exclude` case-insensitively (VSCode extension IDs are
    # case-insensitive) so a lowercase exclude can't leak a differently
    # cased installed ID back into `include`.
    exclude_keys = {e.casefold() for e in resolved.extensions.exclude}
    new_include = sorted(i for i in installed if i.casefold() not in exclude_keys)

    yaml, doc = _load_yaml_doc(config_path)
    ext_block = _profile_extensions_block(doc, profile)
    current = list(ext_block.get("include", []))
    if list(current) == new_include:
        return False
    ext_block["include"] = CommentedSeq(new_include)
    _dump_yaml_doc(yaml, doc, config_path)
    return True


def remove_from_include(
    config_path: Path,
    profile: str,
    ext_id: str,
    *,
    add_to_exclude_list: bool = False,
) -> bool:
    """Remove ``ext_id`` from ``profiles.<profile>.extensions.include``.

    If ``add_to_exclude_list`` is true, also append to ``exclude``
    (idempotent). Returns ``True`` iff any change was made.

    Raises :class:`ConfigError` if ``ext_id`` isn't in this profile's
    literal ``include`` but IS declared by an inherited profile —
    silently no-op'ing in that case would be confusing UX. Pass
    ``--exclude`` (which sets ``add_to_exclude_list=True``) to override
    inherited declarations via the ``exclude`` mechanism.
    """
    cfg = load_config(config_path)
    if profile not in cfg.profiles:
        raise ProfileNotFound(f"profile not found: {profile}")

    in_literal_include = ext_id in cfg.profiles[profile].extensions.include
    if not in_literal_include and not add_to_exclude_list:
        ancestor = _ancestor_declaring(cfg, profile, ext_id)
        if ancestor is not None:
            raise ConfigError(
                f"{ext_id!r} is declared in inherited profile {ancestor!r}; "
                f"remove it from {ancestor} or pass --exclude to override "
                f"the inherited declaration via {profile}.extensions.exclude"
            )

    yaml, doc = _load_yaml_doc(config_path)
    ext_block = _profile_extensions_block(doc, profile)
    include = _ensure_list(ext_block, "include")
    changed = False
    if ext_id in include:
        include.remove(ext_id)
        changed = True
    if add_to_exclude_list:
        exclude = _ensure_list(ext_block, "exclude")
        if ext_id not in exclude:
            exclude.append(ext_id)
            changed = True
    if not changed:
        return False
    _dump_yaml_doc(yaml, doc, config_path)
    return True
