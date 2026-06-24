"""Tests for the generative-scaffold CLIs (``scripts/author_*.py``).

Both scaffolds DRAFT a conformant skeleton for human review and never
auto-commit anything. The properties asserted here are the safety contract:

* a drafted file is syntactically valid Python that *collects* under
  ``tmp_path`` (it imports / parses cleanly);
* a scaffold refuses to overwrite an existing file (``exist_ok=False``);
* a scaffold rejects a ``../`` or otherwise non-identifier component / key
  (path / template-injection guard).

The CLIs are standalone scripts, so the tests import their module-level
helper functions directly rather than shelling out — same coverage, no
subprocess flakiness.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import author_config_entry, author_invariants


def _assert_parses(path: Path) -> ast.Module:
    """The drafted file must be syntactically valid (it 'collects')."""
    assert path.exists()
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --- author_invariants -----------------------------------------------------


def test_invariants_drafts_collectable_file(tmp_path: Path) -> None:
    out = author_invariants.draft(
        component="setforge.reconcile.merge",
        inv="INV-2",
        dest_root=tmp_path,
    )
    tree = _assert_parses(out)
    # A RuleBasedStateMachine subclass with an @invariant()-decorated method.
    classdefs = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert classdefs, "scaffold drafted no class"
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    decorated = [f for f in funcs if f.decorator_list]
    assert decorated, "scaffold drafted no decorated invariant method"


def test_invariants_refuses_overwrite(tmp_path: Path) -> None:
    author_invariants.draft(
        component="setforge.reconcile.merge", inv="INV-2", dest_root=tmp_path
    )
    with pytest.raises(FileExistsError):
        author_invariants.draft(
            component="setforge.reconcile.merge", inv="INV-2", dest_root=tmp_path
        )


@pytest.mark.parametrize(
    "component",
    ["../escape", "/abs/path", "has space", "has-dash", "semi;colon", ""],
)
def test_invariants_rejects_bad_component(tmp_path: Path, component: str) -> None:
    with pytest.raises(ValueError, match="invalid --component"):
        author_invariants.draft(component=component, inv="INV-1", dest_root=tmp_path)


@pytest.mark.parametrize("inv", ["../INV", "INV 2", "INV;rm", ""])
def test_invariants_rejects_bad_inv(tmp_path: Path, inv: str) -> None:
    with pytest.raises(ValueError, match="invalid --inv"):
        author_invariants.draft(
            component="setforge.reconcile.x", inv=inv, dest_root=tmp_path
        )


def test_invariants_stays_under_dest_root(tmp_path: Path) -> None:
    out = author_invariants.draft(
        component="setforge.reconcile.merge", inv="INV-3", dest_root=tmp_path
    )
    # Resolved path must be confined to dest_root (no traversal escape).
    assert tmp_path.resolve() in out.resolve().parents


# --- author_config_entry ---------------------------------------------------


def test_config_entry_drafts_collectable_file(tmp_path: Path) -> None:
    out = author_config_entry.draft(key="retry_count", pytype="int", dest_root=tmp_path)
    tree = _assert_parses(out)
    classdefs = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert classdefs, "scaffold drafted no schema-entry class"
    # A round-trip test function is part of the skeleton.
    funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert any(name.startswith("test_") for name in funcs), "no round-trip test drafted"


def test_config_entry_refuses_overwrite(tmp_path: Path) -> None:
    author_config_entry.draft(key="retry_count", pytype="int", dest_root=tmp_path)
    with pytest.raises(FileExistsError):
        author_config_entry.draft(key="retry_count", pytype="int", dest_root=tmp_path)


@pytest.mark.parametrize(
    "key",
    ["../escape", "/abs/path", "has space", "has-dash", "semi;colon", "with.dot", ""],
)
def test_config_entry_rejects_bad_key(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="invalid --key"):
        author_config_entry.draft(key=key, pytype="int", dest_root=tmp_path)


@pytest.mark.parametrize("pytype", ["int", "str", "bool", "float"])
def test_config_entry_accepts_known_types(tmp_path: Path, pytype: str) -> None:
    out = author_config_entry.draft(
        key=f"opt_{pytype}", pytype=pytype, dest_root=tmp_path
    )
    _assert_parses(out)


@pytest.mark.parametrize("pytype", ["list[int]", "../bad", "os.system", "Evil()", ""])
def test_config_entry_rejects_bad_type(tmp_path: Path, pytype: str) -> None:
    with pytest.raises(ValueError, match="invalid --type"):
        author_config_entry.draft(key="ok_key", pytype=pytype, dest_root=tmp_path)


def test_config_entry_stays_under_dest_root(tmp_path: Path) -> None:
    out = author_config_entry.draft(key="retry_count", pytype="int", dest_root=tmp_path)
    assert tmp_path.resolve() in out.resolve().parents
