"""Unit tests for the deterministic policy/AST lint gates (check_policy_lints.py).

Covers, per the F2a acceptance contract: each lint FIRES on a seeded violation
(SAFE-1/2, UX-1/3) and stays CLEAN on the look-alike false positives; f-string +
bytes ANSI; import variants; the legacy-API namespace scope; the allowlist
exact-match boundary; the SAFE-10 ratchet (pinned set) + stale-entry self-check;
fail-closed on an unparseable file; and the PreToolUse hook's exit-code mapping.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.check_policy_lints import (
    LEGACY_MODULES,
    Violation,
    allowlist_self_check,
    check_source,
)

HOOK = str(Path(__file__).resolve().parent.parent / "scripts" / "policy_lint_hook.py")


def _ids(viols: list[Violation], rule: str) -> list[Violation]:
    return [v for v in viols if v.rule_id == rule]


# --------------------------------------------------------------------------- #
# SAFE-1  shell=True-ban  (repo-wide)
# --------------------------------------------------------------------------- #
def test_shell_true_fires() -> None:
    vs = check_source(
        "import subprocess\nsubprocess.run(cmd, shell=True)\n", "setforge/new.py"
    )
    hits = _ids(vs, "SAFE-1")
    assert len(hits) == 1
    assert hits[0].line == 2


def test_shell_true_clean_and_false_positives() -> None:
    src = (
        "# never use shell=True here\n"
        '"""Docstring mentioning shell=True is fine."""\n'
        "subprocess.run(cmd, shell=False)\n"
        "subprocess.run(cmd, shell=flag)\n"
        "subprocess.run(cmd, shell=1)\n"
        'subprocess.run(["git", "status"])\n'
    )
    assert _ids(check_source(src, "setforge/new.py"), "SAFE-1") == []


def test_shell_true_is_repo_wide_even_in_allowlisted_file() -> None:
    # config.py is grandfathered for the UX lints, but shell=True is repo-wide.
    vs = check_source("subprocess.run(c, shell=True)\n", "setforge/config.py")
    assert len(_ids(vs, "SAFE-1")) == 1


# --------------------------------------------------------------------------- #
# SAFE-2  legacy-API-ban  (namespace-scoped to the new-engine packages)
# --------------------------------------------------------------------------- #
def test_legacy_api_fires_in_enforced_pkg() -> None:
    vs = check_source(
        "from setforge.spans_overlay import apply\n", "setforge/reconcile/merge.py"
    )
    assert len(_ids(vs, "SAFE-2")) == 1


def test_legacy_api_not_enforced_outside_enforced_pkgs() -> None:
    # A current consumer outside the new-engine packages is NOT flagged.
    vs = check_source(
        "from setforge._legacy_markers import X\n", "setforge/cli/override.py"
    )
    assert _ids(vs, "SAFE-2") == []


def test_legacy_api_import_variants() -> None:
    for src in (
        "import setforge._legacy_markers as s\n",
        "from setforge import _legacy_markers\n",
        "from setforge.spans_overlay import apply\n",
        "from setforge.section_wizard import run\n",
    ):
        vs = check_source(src, "setforge/provision/x.py")
        assert _ids(vs, "SAFE-2"), f"expected SAFE-2 for: {src!r}"


# --------------------------------------------------------------------------- #
# UX-1  wizard-letter-ban
# --------------------------------------------------------------------------- #
def test_wizard_letter_fires() -> None:
    src = (
        "from setforge.wizard import read_one_choice\n"
        "x = read_one_choice('k/t', set())\n"
    )
    hits = _ids(check_source(src, "setforge/newui.py"), "UX-1")
    assert len(hits) == 1
    assert hits[0].line == 2


def test_wizard_letter_attribute_form() -> None:
    vs = check_source("x = wizard.read_one_choice('k/t', set())\n", "setforge/newui.py")
    assert len(_ids(vs, "UX-1")) == 1


# --------------------------------------------------------------------------- #
# UX-3  theme-hardcode-ban
# --------------------------------------------------------------------------- #
def test_theme_hardcode_ansi_fires() -> None:
    vs = check_source('p = "\\033[33mwarn\\033[0m"\n', "setforge/newui.py")
    assert len(_ids(vs, "UX-3")) == 1


def test_theme_hardcode_fstring_and_bytes_ansi() -> None:
    src = 'x = f"\\x1b[{n}m"\ny = b"\\x1b[0m"\n'
    assert len(_ids(check_source(src, "setforge/newui.py"), "UX-3")) == 2


def test_theme_hardcode_hex_whole_token_and_color_kw() -> None:
    src = 'a = "#7aa2f7"\nb = widget(color="#ff0000")\n'
    assert len(_ids(check_source(src, "setforge/newui.py"), "UX-3")) == 2


def test_theme_hardcode_issue_number_in_prose_is_safe() -> None:
    # #142916 is 6 hex digits but NOT a whole-token literal → must not fire.
    src = 'x = "fixed CPython bug #142916 in the syscall path"\n'
    assert _ids(check_source(src, "setforge/new.py"), "UX-3") == []


# --------------------------------------------------------------------------- #
# Allowlist: exact-match boundary + ratchet + stale-entry
# --------------------------------------------------------------------------- #
def test_allowlist_boundary_is_exact_not_prefix() -> None:
    src = "x = read_one_choice('a/b', set())\n"
    # deploy.py is NOT allowlisted (capture.py is) → flagged
    assert _ids(check_source(src, "setforge/deploy.py"), "UX-1")
    # the allowlisted file itself is exempt
    assert _ids(check_source(src, "setforge/capture.py"), "UX-1") == []


def test_legacy_modules_allowlist_is_pinned() -> None:
    # SAFE-10 ratchet: this set may only SHRINK. Update deliberately (and only
    # downward) when a grandfathered module is replaced.
    expected = {
        "setforge/config.py",
        "setforge/capture.py",
        "setforge/wizard.py",
    }
    assert expected == LEGACY_MODULES


def test_allowlist_entries_all_exist_on_main() -> None:
    assert allowlist_self_check() == []


# --------------------------------------------------------------------------- #
# Fail-closed parsing
# --------------------------------------------------------------------------- #
def test_unparseable_source_is_a_violation() -> None:
    assert _ids(check_source("def (:\n    pass\n", "setforge/broken.py"), "PARSE")


# --------------------------------------------------------------------------- #
# PreToolUse hook — exit-code mapping (fail-closed)
# --------------------------------------------------------------------------- #
def _run_hook(command: str, lint_cmd: str | None) -> subprocess.CompletedProcess[str]:
    import json
    import os

    env = dict(os.environ)
    if lint_cmd is not None:
        env["POLICY_LINT_CMD"] = lint_cmd
    return subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        env=env,
    )


def test_hook_allows_non_commit_command() -> None:
    r = _run_hook("ls -la", lint_cmd="python3 -c 'import sys; sys.exit(1)'")
    assert r.returncode == 0  # not a commit → never even runs the lints


def test_hook_allows_commit_when_lints_clean() -> None:
    r = _run_hook("git commit -m x", lint_cmd="python3 -c 'import sys; sys.exit(0)'")
    assert r.returncode == 0


def test_hook_blocks_commit_when_lints_fail_with_exit_2() -> None:
    stub = "python3 -c \"import sys; sys.stderr.write('SAFE-1 boom'); sys.exit(1)\""
    r = _run_hook("git commit -m x", lint_cmd=stub)
    assert r.returncode == 2
    assert "SAFE-1 boom" in r.stderr


def test_hook_fails_closed_on_crash() -> None:
    # any non-zero / unexpected lint outcome must block (exit 2), never fail open
    r = _run_hook("git commit -m x", lint_cmd="python3 -c 'import sys; sys.exit(3)'")
    assert r.returncode == 2


def test_hook_detects_commit_inside_compound_command() -> None:
    r = _run_hook(
        "cd /repo && git commit -m x",
        lint_cmd="python3 -c 'import sys; sys.exit(1)'",
    )
    assert r.returncode == 2
