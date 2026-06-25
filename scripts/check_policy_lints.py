#!/usr/bin/env python3
"""Deterministic policy/AST lint gates for setforge (RULES.md SAFE-1/2, UX-1/3).

A STANDALONE script (NOT a pytest test — pytest is skippable via markers and
``addopts``, which would silently disarm the contract; same reasoning as
:mod:`scripts.check_schema_gates`). It walks every tracked ``.py`` file
(``git ls-files``) and runs four AST-anchored lints:

============  ======  =======================================================
Rule          ID      What it bans
============  ======  =======================================================
shell=True    SAFE-1  ``subprocess(..., shell=True)`` — pass an argv list
legacy-API    SAFE-2  new-engine code importing the legacy disposition /
                      sections / spans / overlay subsystem
wizard-letter UX-1    ``setforge.wizard.read_one_choice`` letter menus
theme-hardcode UX-3   raw ANSI escapes / hex colours outside the theme
============  ======  =======================================================

Scoping (RFC §5 — a DETERMINISTIC rule must BLOCK, but stay ~zero false
positive, else hard-blocking breeds fatigue):

* **shell=True** — every tracked ``.py`` (the repo is clean today).
* **legacy-API** — *namespace-scoped*: only files under :data:`ENFORCED_PKGS`
  (the fresh A/B engine packages) are checked. The legacy subsystem pervades
  the *current* code, so it is NOT grandfathered by a flat allowlist; the ban
  instead guards the new engine the moment it lands. Removing the legacy code
  is tracked as its own follow-up.
* **wizard-letter / theme-hardcode** — all of ``setforge/**`` except the small
  pinned :data:`LEGACY_MODULES` allowlist (the handful of pre-existing
  violators). The allowlist may only SHRINK (SAFE-10 ratchet, pinned by a
  meta-test) and is self-checked for stale entries.

All four lints are AST-anchored — they inspect :class:`ast.Constant` /
:class:`ast.Call` / import nodes, never raw source text. Comments and
docstrings are therefore invisible to them, so doc mentions like
``"never shell=True"`` and issue numbers like ``#142916`` in prose cannot
false-positive.

Exit ``0`` clean / ``1`` on any violation (printed to stderr as
``RULE-ID path:line  message``). The :mod:`scripts.policy_lint_hook` PreToolUse
wrapper maps nonzero/crash to exit ``2`` (fail-closed). Wired as its own
pre-commit hook + CI step.

Invocation::

    uv run python scripts/check_policy_lints.py
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- legacy subsystem (SAFE-2) -------------------------------------------------
# The four old mechanisms: disposition / sections / spans / overlays. A new-engine
# module importing any of these (by leaf module name) is a violation.
LEGACY_MODULES_BANNED: frozenset[str] = frozenset(
    {
        "disposition_merge",
        "sections",
        "section_detect",
        "section_mode",
        "section_promote",
        "section_reconcile",
        "section_templates",
        "section_wizard",
        "spans",
        "spans_overlay",
        "spans_store",
        "spans_relocation",
        "markdown_spans",
        "overlay_deploy",
        "overlay_inject",
        "overlay_body_wizard",
        "overlay_migration",
        "local_overlay",
    }
)

# New-engine packages whose code must NOT reach back into the legacy subsystem.
# Empty of files today (the new engine lands here later); the ban activates
# automatically the moment a file appears under one of these prefixes.
ENFORCED_PKGS: tuple[str, ...] = ("setforge/reconcile/", "setforge/provision/")

# --- UX allowlist (UX-1 / UX-3) ------------------------------------------------
# Pre-existing violators grandfathered for the UX lints ONLY. RATCHET: this set
# may only SHRINK (pinned by test_policy_lints.py); a path here must exist
# (a stale entry is a hard error). Each entry names the rule it grandfathers.
LEGACY_MODULES: frozenset[str] = frozenset(
    {
        "setforge/config.py",  # UX-3: hardcoded \033[ warning prefixes
        "setforge/conflict_wizard.py",  # UX-1: read_one_choice letter menu
        "setforge/section_wizard.py",  # UX-1: read_one_choice
        "setforge/capture.py",  # UX-1: read_one_choice
        "setforge/wizard.py",  # UX-1: defines + uses read_one_choice
    }
)

_HEX_TOKEN_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")  # WHOLE-token only → prose-safe
# colour-bearing keyword arguments, for hex appearing as `color="#abc123"`
_COLOR_KEYS: frozenset[str] = frozenset(
    {
        "color",
        "colour",
        "fg",
        "bg",
        "fgcolor",
        "bgcolor",
        "foreground",
        "background",
        "style",
    }
)
_HEX_SUB_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


@dataclass(frozen=True, slots=True)
class Violation:
    """One policy-lint hit: a stable rule id, a ``file:line``, and a fix hint."""

    rule_id: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.rule_id} {self.path}:{self.line}  {self.message}"


# The one module where raw hex / SGR colour literals legitimately live: UX-3
# (the hardcoded-colour ban) explicitly applies "outside the theme module".
THEME_MODULE = "setforge/ui/theme.py"


def _is_setforge_source(path: str) -> bool:
    """A first-party engine module (the scope for the UX lints — not tests/scripts)."""
    return path.startswith("setforge/") and path.endswith(".py")


def lint_shell_true(tree: ast.AST, path: str) -> list[Violation]:
    """SAFE-1, repo-wide: flag ``subprocess(..., shell=True)`` (constant ``True``)."""
    out: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "shell"  # `kw.arg is None` for **kwargs — excluded
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True  # `is True`, not `== True`/truthy
            ):
                out.append(
                    Violation(
                        "SAFE-1",
                        path,
                        kw.value.lineno,
                        "subprocess called with shell=True — pass an argv list instead",
                    )
                )
    return out


def _imported_modules(node: ast.AST) -> list[str]:
    """Dotted module name(s) an import node pulls in (real names, not aliases)."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]  # `import setforge.sections [as s]`
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""  # None for `from . import x`
        names = [mod] if mod else []
        # `from setforge import sections` → submodule name is the legacy target
        names += [f"{mod}.{a.name}" if mod else a.name for a in node.names]
        return names
    return []


def lint_legacy_api(tree: ast.AST, path: str) -> list[Violation]:
    """SAFE-2, namespace-scoped: new-engine code must not import legacy modules."""
    if not path.startswith(ENFORCED_PKGS):
        return []
    out: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for name in _imported_modules(node):
            leaf = name.rsplit(".", 1)[-1]
            if leaf in LEGACY_MODULES_BANNED:
                out.append(
                    Violation(
                        "SAFE-2",
                        path,
                        node.lineno,
                        f"new-engine code must not import the legacy module "
                        f"'{leaf}' (disposition/sections/spans/overlay subsystem)",
                    )
                )
                break
    return out


def lint_wizard_letter(tree: ast.AST, path: str) -> list[Violation]:
    """UX-1: flag calls to the legacy letter-prompt primitive ``read_one_choice``."""
    if not _is_setforge_source(path) or path in LEGACY_MODULES:
        return []
    out: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr
        else:
            name = None
        if name == "read_one_choice":
            out.append(
                Violation(
                    "UX-1",
                    path,
                    node.lineno,
                    "letter-menu prompt via read_one_choice — use a button-bar "
                    "(button_dialog / radiolist_dialog) instead",
                )
            )
    return out


def lint_theme_hardcode(tree: ast.AST, path: str) -> list[Violation]:
    """UX-3: flag raw ANSI escapes + whole-token / colour-keyword hex literals."""
    # The theme module is exempt: it is where colour literals legitimately live.
    if not _is_setforge_source(path) or path in LEGACY_MODULES or path == THEME_MODULE:
        return []
    out: list[Violation] = []
    for node in ast.walk(tree):
        # (a) string/bytes literals (incl. f-string chunks, which ast.walk yields
        #     as their own Constant nodes): raw ANSI, or a WHOLE hex colour token.
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            value = node.value
            if isinstance(value, bytes):
                has_ansi = b"\x1b" in value
                text = value.decode("latin-1")
            else:
                has_ansi = "\x1b" in value
                text = value
            if has_ansi:
                out.append(
                    Violation(
                        "UX-3",
                        path,
                        node.lineno,
                        "raw ANSI escape in a string literal — route colour through "
                        "the theme module (semantic roles only)",
                    )
                )
            elif _HEX_TOKEN_RE.match(text):
                out.append(
                    Violation(
                        "UX-3",
                        path,
                        node.lineno,
                        f"hardcoded hex colour {text!r} — use a semantic theme role",
                    )
                )
        # (b) hex inside a colour-bearing keyword arg: `widget(color="#ff0000")`.
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if (
                    kw.arg in _COLOR_KEYS
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                    and _HEX_SUB_RE.search(kw.value.value)
                ):
                    out.append(
                        Violation(
                            "UX-3",
                            path,
                            kw.value.lineno,
                            f"hardcoded hex colour in {kw.arg}= — use a theme role",
                        )
                    )
    return out


_LINTS = (lint_shell_true, lint_legacy_api, lint_wizard_letter, lint_theme_hardcode)


def check_source(source: str, path: str) -> list[Violation]:
    """Run all four lints over one already-read file. Unparseable → a PARSE
    violation (fail-closed: a file that won't parse cannot silently skip the gate)."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", 0) or 0
        return [Violation("PARSE", path, line, f"could not parse file: {exc}")]
    out: list[Violation] = []
    for lint in _LINTS:
        out.extend(lint(tree, path))
    # One violation per (rule, line): the two theme-hardcode checks can both
    # hit a single colour literal (whole-token AND inside a `color=` kwarg).
    seen: set[tuple[str, int]] = set()
    deduped: list[Violation] = []
    for v in out:
        key = (v.rule_id, v.line)
        if key not in seen:
            seen.add(key)
            deduped.append(v)
    return deduped


def _check_file(path: str) -> list[Violation]:
    try:
        with tokenize.open(REPO_ROOT / path) as fh:  # honors the PEP 263 cookie
            source = fh.read()
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return [Violation("PARSE", path, 0, f"could not read file: {exc}")]
    return check_source(source, path)


def _tracked_py_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def allowlist_self_check() -> list[Violation]:
    """Stale-entry guard: every LEGACY_MODULES path must still exist on disk."""
    return [
        Violation(
            "LINT-SELF",
            p,
            0,
            "stale LEGACY_MODULES entry — file does not exist; remove it "
            "(the allowlist only shrinks)",
        )
        for p in sorted(LEGACY_MODULES)
        if not (REPO_ROOT / p).exists()
    ]


def main() -> int:
    violations: list[Violation] = list(allowlist_self_check())
    for path in _tracked_py_files():
        violations.extend(_check_file(path))  # already deduped per (rule, line)

    if violations:
        print("Policy lint violations (see docs/RULES.md):", file=sys.stderr)
        for v in sorted(violations, key=lambda v: (v.path, v.line, v.rule_id)):
            print(f"  {v}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
