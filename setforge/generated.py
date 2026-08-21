"""Deterministic rendering for portable generated tracked-file intent."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from jinja2 import StrictUndefined, TemplateError, nodes
from jinja2.sandbox import SandboxedEnvironment

from setforge.config import GeneratedContent, HostInputKind
from setforge.errors import ConfigError
from setforge.paths import vscode_user_dir


@dataclass(frozen=True, slots=True)
class GeneratedResolution:
    """Frozen host facts and rendered bytes for one generated resource."""

    inputs: tuple[tuple[str, HostInputKind, str], ...]
    rendered: str
    fingerprint: str


def _resolve_input(kind: HostInputKind) -> str:
    if kind is HostInputKind.HOME:
        path = Path.home()
    elif kind is HostInputKind.VSCODE_USER_DIR:
        path = vscode_user_dir() / "User"
    else:  # pragma: no cover - exhaustive StrEnum boundary
        raise ConfigError(f"unsupported generated host input: {kind!r}")
    return str(path.expanduser().resolve(strict=False))


def resolve_generated(source: str, spec: GeneratedContent) -> GeneratedResolution:
    """Resolve declared host facts and render ``source`` in a closed sandbox."""
    inputs = tuple(
        (name, kind, _resolve_input(kind)) for name, kind in sorted(spec.inputs.items())
    )
    host = SimpleNamespace(**{name: value for name, _kind, value in inputs})
    try:
        environment = SandboxedEnvironment(
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )
        environment.globals.clear()
        parsed = environment.parse(source)
        if next(parsed.find_all(nodes.Call), None) is not None:
            raise ConfigError(
                "generated tracked-file templates cannot call functions or methods"
            )
        if next(parsed.find_all((nodes.Filter, nodes.Test)), None) is not None:
            raise ConfigError(
                "generated tracked-file templates cannot use filters or tests"
            )
        for attribute in parsed.find_all(nodes.Getattr):
            if not (
                isinstance(attribute.node, nodes.Name)
                and attribute.node.name == "host"
                and attribute.attr in host.__dict__
            ):
                raise ConfigError(
                    "generated tracked-file templates may only read declared "
                    "host.<name> values"
                )
        if next(parsed.find_all(nodes.Getitem), None) is not None:
            raise ConfigError(
                "generated tracked-file templates may only read declared "
                "host.<name> values"
            )
        rendered = environment.from_string(source).render(host=host)
    except ConfigError:
        raise
    except TemplateError as exc:
        raise ConfigError(
            f"generated tracked-file template cannot render: {exc}"
        ) from exc
    payload = json.dumps(
        {
            "inputs": [(name, kind.value, value) for name, kind, value in inputs],
            "rendered": rendered,
            "source": source,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return GeneratedResolution(
        inputs=inputs,
        rendered=rendered,
        fingerprint=hashlib.sha256(payload).hexdigest(),
    )


def rendered_source(path: Path, spec: GeneratedContent | None) -> str:
    """Read one source and render it only when generator intent is declared."""
    source = path.read_text(encoding="utf-8")
    return source if spec is None else resolve_generated(source, spec).rendered
