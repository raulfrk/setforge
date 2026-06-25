"""Safety + generation tests for :mod:`scripts.author_wizard`.

The scaffold takes UNTRUSTED ``--name`` input and emits two draft files (a
wizard module + its paired INV test). The contract mirrors
:mod:`scripts.author_config_entry`: validate the name FIRST, confine the output
path, ``"x"``-mode writes (never clobber), no user-controlled format string,
and a generated module body that is imports/classes/defs only (no module-level
call / ``__main__`` TUI launch).
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from scripts.author_wizard import draft, main

# ---------------------------------------------------------------------------
# Rejection: bad --name must never reach the filesystem or a template.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "a.b",  # dotted
        "../escape",  # traversal
        "a/b",  # path separator
        "a{b}",  # brace (format-string injection vector)
        "a b",  # whitespace
        "",  # empty
        "1abc",  # leading digit (not a valid identifier)
        "a-b",  # dash
    ],
)
def test_rejects_bad_name(tmp_path: Path, bad_name: str) -> None:
    with pytest.raises(ValueError, match="invalid --name"):
        draft(bad_name, dest_root=tmp_path)
    # And the CLI surfaces a nonzero exit for the same input.
    assert main(["--name", bad_name, "--dest-root", str(tmp_path)]) != 0


def test_valid_name_emits_two_files(tmp_path: Path) -> None:
    module_path, test_path = draft("demo", dest_root=tmp_path)
    assert module_path.exists()
    assert test_path.exists()
    assert module_path != test_path
    # Both land under the confined scratch dir.
    assert tmp_path in module_path.parents
    assert tmp_path in test_path.parents


def test_never_clobbers(tmp_path: Path) -> None:
    draft("demo", dest_root=tmp_path)
    with pytest.raises(FileExistsError):
        draft("demo", dest_root=tmp_path)
    # CLI re-run on an existing target returns a nonzero (non-2) exit.
    assert main(["--name", "demo", "--dest-root", str(tmp_path)]) == 1


def test_classname_fallback(tmp_path: Path) -> None:
    # A name that CamelCases to empty (all-underscores) falls back to "Wizard"
    # for the human-facing title.
    module_path, _ = draft("_", dest_root=tmp_path)
    source = module_path.read_text(encoding="utf-8")
    assert 'title="Wizard"' in source


def test_generated_module_has_no_toplevel_call(tmp_path: Path) -> None:
    module_path, _ = draft("demo", dest_root=tmp_path)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    # Every top-level statement is an import / class / function def / docstring
    # / __future__ — never a bare call or an ``if __name__`` TUI launch.
    allowed = (
        ast.Import,
        ast.ImportFrom,
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        assert isinstance(node, allowed), (
            f"unexpected top-level node {type(node).__name__} in generated module"
        )


def test_generated_module_imports_widget(tmp_path: Path) -> None:
    module_path, _ = draft("demo", dest_root=tmp_path)
    source = module_path.read_text(encoding="utf-8")
    assert "button_bar" in source
    assert "from setforge.ui.widgets import" in source


def test_generated_module_is_valid_python(tmp_path: Path) -> None:
    module_path, test_path = draft("demo", dest_root=tmp_path)
    # Both files parse — a syntactically broken template is a hard failure.
    ast.parse(module_path.read_text(encoding="utf-8"))
    ast.parse(test_path.read_text(encoding="utf-8"))


def test_generated_module_importable(tmp_path: Path) -> None:
    # The drafted wizard module imports cleanly against the real widget.
    module_path, _ = draft("demo", dest_root=tmp_path)
    spec = importlib.util.spec_from_file_location("drafted_demo_wizard", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The drafted wizard exposes a callable entry that returns a value/CANCEL.
    entries = [getattr(module, n) for n in dir(module) if "wizard" in n.lower()]
    assert any(callable(e) for e in entries)


def test_generated_test_passes_ux1_and_esc(tmp_path: Path) -> None:
    """The generated INV test (a) asserts UX-1 clean and (b) Esc→CANCEL — and
    actually passes when run against the real widget."""
    _module_path, test_path = draft("demo", dest_root=tmp_path)
    test_source = test_path.read_text(encoding="utf-8")
    # The two INV claims are present in the generated test.
    assert "check_source" in test_source  # (a) UX-1 lint
    assert "CANCEL" in test_source  # (b) Esc→CANCEL

    # Execute the generated test's assertions in-process: load it as a module
    # and call every ``test_*`` function. It imports the drafted wizard by
    # absolute path via the MODULE_PATH constant the scaffold injected.
    spec = importlib.util.spec_from_file_location("drafted_demo_inv", test_path)
    assert spec is not None
    assert spec.loader is not None
    test_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_mod)
    test_fns = [getattr(test_mod, n) for n in dir(test_mod) if n.startswith("test_")]
    assert test_fns, "generated test exposes no test_* functions"
    for fn in test_fns:
        fn()


def test_cli_emits_and_prints_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--name", "okname", "--dest-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    # Both emitted paths are reported to stdout.
    assert "okname" in out
