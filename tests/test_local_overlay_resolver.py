"""Unit tests for the local.yaml plugin/extension/marketplace overlay resolvers.

Spec: SPEC 2. Validates the semantics of
:func:`setforge.local_overlay.resolve_plugin_overlay`,
:func:`setforge.local_overlay.resolve_extension_overlay`, and
:func:`setforge.local_overlay.resolve_marketplace_overlay` — the
contract that ``compare`` / ``install`` / ``validate`` rely on to
display provenance tags per SPEC 2's ``[from local.yaml]`` (adds) and
U+2212-prefixed ``removed via local.yaml`` (removes) shape — see
:func:`setforge.local_overlay.display_tag` for the verbatim wording.

Also pins :func:`setforge.local_overlay.display_tag` as the SINGLE
source of truth for the tag wording so a future inline f-string in
install / compare / validate code paths will fail one of the
substring asserts at the bottom of this file.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from setforge.config import MarketplaceSourceKind
from setforge.errors import ConfigError
from setforge.local_overlay import (
    LocalOverlayError,
    OverlayOrigin,
    ResolvedExtension,
    ResolvedMarketplace,
    ResolvedPlugin,
    display_tag,
    has_local_overlay,
    resolve_extension_overlay,
    resolve_marketplace_overlay,
    resolve_plugin_overlay,
)
from setforge.source import (
    ExtensionOverlay,
    MarketplaceOverlay,
    PluginOverlay,
    _MarketplaceLocalDecl,
)

# ---------------------------------------------------------------------------
# resolve_plugin_overlay
# ---------------------------------------------------------------------------


def test_plugin_empty_overlay_returns_profile_entries_tagged_profile() -> None:
    resolved = resolve_plugin_overlay(
        profile_plugins=["a", "b"],
        profile_name="vm-headless",
        overlay=PluginOverlay(),
    )
    assert resolved == [
        ResolvedPlugin("a", OverlayOrigin.PROFILE),
        ResolvedPlugin("b", OverlayOrigin.PROFILE),
    ]


def test_plugin_add_only_appends_local_add_after_profile() -> None:
    resolved = resolve_plugin_overlay(
        profile_plugins=["a"],
        profile_name="vm-headless",
        overlay=PluginOverlay(add=["x@mp", "y@mp"]),
    )
    assert resolved == [
        ResolvedPlugin("a", OverlayOrigin.PROFILE),
        ResolvedPlugin("x@mp", OverlayOrigin.LOCAL_ADD),
        ResolvedPlugin("y@mp", OverlayOrigin.LOCAL_ADD),
    ]


def test_plugin_remove_tags_profile_entry_as_local_remove() -> None:
    resolved = resolve_plugin_overlay(
        profile_plugins=["a", "b"],
        profile_name="vm-headless",
        overlay=PluginOverlay(remove=["a"]),
    )
    assert resolved == [
        ResolvedPlugin("a", OverlayOrigin.LOCAL_REMOVE),
        ResolvedPlugin("b", OverlayOrigin.PROFILE),
    ]


def test_plugin_redundant_add_silently_dedup() -> None:
    """A redundant ``add`` of a plugin already in the profile chain stays
    PROFILE — duplicate emit would force local.yaml to mirror the entire
    chain, overly strict for a benign no-op."""
    resolved = resolve_plugin_overlay(
        profile_plugins=["a"],
        profile_name="p",
        overlay=PluginOverlay(add=["a"]),
    )
    assert resolved == [ResolvedPlugin("a", OverlayOrigin.PROFILE)]


def test_plugin_collision_add_and_remove_same_value_raises() -> None:
    with pytest.raises(LocalOverlayError) as exc_info:
        resolve_plugin_overlay(
            profile_plugins=["a"],
            profile_name="p",
            overlay=PluginOverlay(add=["x"], remove=["x"]),
        )
    assert "in both add and remove" in str(exc_info.value)
    assert "'x'" in str(exc_info.value)


def test_plugin_remove_not_in_profile_raises() -> None:
    with pytest.raises(LocalOverlayError) as exc_info:
        resolve_plugin_overlay(
            profile_plugins=["a"],
            profile_name="p",
            overlay=PluginOverlay(remove=["nope"]),
        )
    assert "not in profile-resolved set" in str(exc_info.value)
    assert "'nope'" in str(exc_info.value)


def test_plugin_local_overlay_error_subclasses_config_error() -> None:
    with pytest.raises(ConfigError):
        resolve_plugin_overlay(
            profile_plugins=[],
            profile_name="p",
            overlay=PluginOverlay(add=["x"], remove=["x"]),
        )


# ---------------------------------------------------------------------------
# resolve_extension_overlay
# ---------------------------------------------------------------------------


def test_extension_empty_overlay_returns_profile_entries_tagged_profile() -> None:
    resolved = resolve_extension_overlay(
        profile_extensions=["ms-python.python", "rust-lang.rust"],
        profile_name="vm-headless",
        overlay=ExtensionOverlay(),
    )
    assert resolved == [
        ResolvedExtension("ms-python.python", OverlayOrigin.PROFILE),
        ResolvedExtension("rust-lang.rust", OverlayOrigin.PROFILE),
    ]


def test_extension_add_remove_combination() -> None:
    resolved = resolve_extension_overlay(
        profile_extensions=["redhat.vscode-yaml"],
        profile_name="vm-headless",
        overlay=ExtensionOverlay(
            add=["ms-python.python"], remove=["redhat.vscode-yaml"]
        ),
    )
    assert resolved == [
        ResolvedExtension("redhat.vscode-yaml", OverlayOrigin.LOCAL_REMOVE),
        ResolvedExtension("ms-python.python", OverlayOrigin.LOCAL_ADD),
    ]


def test_extension_collision_raises() -> None:
    with pytest.raises(LocalOverlayError) as exc_info:
        resolve_extension_overlay(
            profile_extensions=["a.b"],
            profile_name="p",
            overlay=ExtensionOverlay(add=["x.y"], remove=["x.y"]),
        )
    assert "in both add and remove" in str(exc_info.value)


def test_extension_remove_not_in_profile_raises() -> None:
    with pytest.raises(LocalOverlayError) as exc_info:
        resolve_extension_overlay(
            profile_extensions=["a.b"],
            profile_name="p",
            overlay=ExtensionOverlay(remove=["c.d"]),
        )
    assert "not in profile-resolved set" in str(exc_info.value)


# ---------------------------------------------------------------------------
# resolve_marketplace_overlay
# ---------------------------------------------------------------------------


def _mk_decl(repo: str = "owner/repo") -> _MarketplaceLocalDecl:
    return _MarketplaceLocalDecl(source=MarketplaceSourceKind.GITHUB, repo=repo)


def test_marketplace_empty_overlay_returns_profile_entries_tagged_profile() -> None:
    resolved = resolve_marketplace_overlay(
        profile_marketplaces=["official"],
        profile_name="vm-headless",
        overlay=MarketplaceOverlay(),
    )
    assert resolved == [ResolvedMarketplace("official", OverlayOrigin.PROFILE)]


def test_marketplace_add_remove_combination() -> None:
    resolved = resolve_marketplace_overlay(
        profile_marketplaces=["official"],
        profile_name="vm-headless",
        overlay=MarketplaceOverlay(
            add={"work-internal": _mk_decl("work-corp/claude-plugins")},
            remove=[],
        ),
    )
    assert resolved == [
        ResolvedMarketplace("official", OverlayOrigin.PROFILE),
        ResolvedMarketplace("work-internal", OverlayOrigin.LOCAL_ADD),
    ]


def test_marketplace_collision_raises() -> None:
    with pytest.raises(LocalOverlayError) as exc_info:
        resolve_marketplace_overlay(
            profile_marketplaces=["existing"],
            profile_name="p",
            overlay=MarketplaceOverlay(add={"x": _mk_decl()}, remove=["x"]),
        )
    assert "in both add and remove" in str(exc_info.value)


def test_marketplace_remove_not_in_profile_raises() -> None:
    with pytest.raises(LocalOverlayError) as exc_info:
        resolve_marketplace_overlay(
            profile_marketplaces=["existing"],
            profile_name="p",
            overlay=MarketplaceOverlay(remove=["ghost"]),
        )
    assert "not in profile-resolved set" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _MarketplaceLocalDecl _exactly_one validator (mirror MarketplaceSource)
# ---------------------------------------------------------------------------


def test_marketplace_local_decl_requires_exactly_one_of_repo_path() -> None:
    with pytest.raises(ValueError, match="exactly one of repo/path"):
        _MarketplaceLocalDecl(source=MarketplaceSourceKind.GITHUB)
    with pytest.raises(ValueError, match="exactly one of repo/path"):
        _MarketplaceLocalDecl(
            source=MarketplaceSourceKind.GITHUB,
            repo="a/b",
            path=Path("/tmp/x"),
        )


def test_marketplace_local_decl_accepts_repo_only() -> None:
    decl = _MarketplaceLocalDecl(source=MarketplaceSourceKind.GITHUB, repo="a/b")
    assert decl.repo == "a/b"
    assert decl.path is None


def test_marketplace_local_decl_accepts_path_only() -> None:
    decl = _MarketplaceLocalDecl(source=MarketplaceSourceKind.PATH, path=Path("/tmp/x"))
    assert decl.path == Path("/tmp/x")
    assert decl.repo is None


# ---------------------------------------------------------------------------
# display_tag — single source of truth for SPEC 2 wording
# ---------------------------------------------------------------------------


def test_display_tag_local_add_wording() -> None:
    """SPEC 2 mockup line: ``+ secure-code-review@work-internal [from local.yaml]``."""
    assert display_tag(OverlayOrigin.LOCAL_ADD) == "[from local.yaml]"


def test_display_tag_local_remove_wording() -> None:
    """SPEC 2 mockup remove line carries U+2212 MINUS SIGN, not ASCII '-'.

    The leading minus is U+2212 (decimal 8722), NOT U+002D HYPHEN-MINUS
    — matches the mockup verbatim. The expected literal is constructed
    from chr(0x2212) to avoid embedding U+2212 in the test file (RUF001).
    """
    minus = chr(0x2212)
    expected = f"[{minus} removed via local.yaml]"
    tag = display_tag(OverlayOrigin.LOCAL_REMOVE)
    assert tag == expected
    assert minus in tag


def test_display_tag_profile_is_empty_string() -> None:
    assert display_tag(OverlayOrigin.PROFILE) == ""


def test_display_tag_is_exhaustive_over_overlay_origin() -> None:
    """Every :class:`OverlayOrigin` member must round-trip through display_tag.

    Sentinel: extending :class:`OverlayOrigin` without updating
    :func:`display_tag` would surface here as an exhaustiveness gap.
    """
    for origin in OverlayOrigin:
        tag = display_tag(origin)
        assert isinstance(tag, str)


# ---------------------------------------------------------------------------
# Anti-smell: display_tag is the SOLE site of the tag literals
# ---------------------------------------------------------------------------


def _stringify_joined(node: ast.JoinedStr) -> str:
    """Render an :class:`ast.JoinedStr` with interpolations as placeholders.

    Concatenates :class:`ast.Constant` ``str`` children verbatim; renders
    every :class:`ast.FormattedValue` interpolation as ``"{}"``. The
    rendered form preserves the f-string's *literal-segment shape*,
    which is what the parity tests scan for tag-assembly smells like
    ``f"[from {src}]"`` (renders to ``"[from {}]"``).
    """
    parts: list[str] = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        elif isinstance(v, ast.FormattedValue):
            parts.append("{}")
    return "".join(parts)


def _collect_docstring_ids(tree: ast.AST) -> set[int]:
    """Collect ``id()`` of every module / class / function docstring node."""
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            docstring_expr = ast.get_docstring(node, clean=False)
            if docstring_expr is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(
                    first.value, ast.Constant
                ):
                    docstring_ids.add(id(first.value))
    return docstring_ids


def _scan_for_tag_substring(
    tree: ast.AST,
    tag: str,
    docstring_ids: set[int],
    *,
    fstring_smell: Callable[[str], bool] | None = None,
) -> list[int]:
    """Return line numbers where ``tag`` substring appears in code.

    Checks THREE shapes:

    - :class:`ast.Constant` ``str`` nodes (regular string literals and
      atomic f-string segments), skipping docstring positions —
      catches the full-literal shape ``"[from local.yaml]"``.
    - :class:`ast.JoinedStr` rendered via :func:`_stringify_joined`
      with the tag substring present in the rendered text — catches
      multi-segment f-string assemblies whose literal Constant
      children together contain the tag.
    - :class:`ast.JoinedStr` whose rendered text satisfies
      ``fstring_smell(rendered)`` — catches the regression shape the
      round-1 parity test missed: ``f"[from {src}]"`` with
      ``src="local.yaml"`` renders statically to ``"[from {}]"`` and
      contains no ``local.yaml`` literal, but its *shape* (literal
      segments with one interpolation slot in the middle) signals tag
      assembly. The smell predicate filters legitimate non-tag
      f-strings (e.g. ``f"[from profile {x}]"``).
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and tag in node.value
            and id(node) not in docstring_ids
        ):
            hits.append(node.lineno)
        elif isinstance(node, ast.JoinedStr):
            rendered = _stringify_joined(node)
            if tag in rendered or (
                fstring_smell is not None and fstring_smell(rendered)
            ):
                hits.append(node.lineno)
    return hits


def _add_tag_fstring_smell(rendered: str) -> bool:
    """Flag f-strings shaped like a SPEC 2 ADD-tag assembly.

    The ADD tag is ``[from local.yaml]``. The literal-segment shape of
    a tag-assembling f-string is ``[from {}]`` (interpolation in the
    middle). Legitimate ``[from profile {x}]`` f-strings contain the
    word ``profile`` after ``[from `` and are NOT flagged.

    Heuristic: rendered text starts with ``[from `` AND ends with
    ``]`` AND contains an interpolation placeholder ``{}`` AND does
    NOT contain the legitimate word ``profile`` between ``[from `` and
    ``]``. The startswith / endswith check is intentionally loose —
    a JoinedStr that wraps the tag template inside a larger format
    (rare) still gets caught by the segment-substring branch above.
    """
    if "{}" not in rendered:
        return False
    if "[from " not in rendered:
        return False
    # Slice the literal between "[from " and the next "]" if present.
    start = rendered.index("[from ") + len("[from ")
    end = rendered.find("]", start)
    if end == -1:
        return False
    middle = rendered[start:end]
    return "profile" not in middle


def _remove_tag_fstring_smell(rendered: str) -> bool:
    """Flag f-strings shaped like a SPEC 2 REMOVE-tag assembly.

    The REMOVE tag is ``[<U+2212> removed via local.yaml]`` — the
    leading minus is U+2212, NOT U+002D HYPHEN-MINUS. SPEC 8
    (``preserve_user_keys``) declares its own ``[removed via
    local.yaml — overwritten with tracked value]`` tag with a U+002D
    leading character; that variant is SPEC 8's concern and is NOT
    flagged here.

    Heuristic: rendered text contains ``removed via local.yaml`` AND
    EITHER contains the U+2212 minus literal OR has an interpolation
    placeholder ``{}`` immediately preceding the ``removed`` token
    (signalling a SPEC-2 f-string with the minus interpolated). The
    U+2212-vs-U+002D distinction is what gives SPEC 2's remove tag
    its unique identity; without that signal we fall through to the
    full-literal Constant scan, which already catches the bare-string
    construction case.
    """
    if "removed via local.yaml" not in rendered:
        return False
    minus = chr(0x2212)
    if minus in rendered:
        return True
    # f-string with the minus interpolated: ``f"[{m} removed via local.yaml]"``.
    return "{} removed via local.yaml" in rendered


def test_display_tag_single_source_of_truth_in_codebase() -> None:
    """Assert no other module constructs the SPEC 2 ``LOCAL_REMOVE`` tag.

    Walks every ``setforge/**/*.py`` for the U+2212-prefixed
    ``removed via local.yaml`` literal (the SPEC-2-specific tag with
    U+2212 MINUS SIGN); allows it only inside
    ``setforge/local_overlay.py`` (definition site). Any other site is
    an anti-smell hit — the caller MUST go through :func:`display_tag`.

    The ``[from local.yaml]`` tag is intentionally NOT checked here:
    :mod:`setforge.preserved_keys` (SPEC 8) declares its own SoT for
    the same wording on a different concern (preserve_user_keys). The
    SPEC-2 tag with the unicode minus is the unique marker we own.

    Hardened (round 4): walks BOTH :class:`ast.Constant` string nodes
    AND :class:`ast.JoinedStr` rendered via :func:`_stringify_joined`,
    so f-string assemblies like ``f"[{minus} removed via local.yaml]"``
    with an interpolated minus (uncommon but defense-in-depth) are
    caught even when the literal is split across f-string segments.
    """
    import setforge

    pkg_root = Path(setforge.__file__).parent
    minus = chr(0x2212)
    remove_tag = f"[{minus} removed via local.yaml]"
    offenders: list[tuple[Path, int]] = []
    for py in pkg_root.rglob("*.py"):
        if py.name == "local_overlay.py":
            continue
        text = py.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(py))
        docstring_ids = _collect_docstring_ids(tree)
        for lineno in _scan_for_tag_substring(
            tree,
            remove_tag,
            docstring_ids,
            fstring_smell=_remove_tag_fstring_smell,
        ):
            offenders.append((py, lineno))
    assert not offenders, (
        f"SPEC 2 remove tag literal constructed outside display_tag(): "
        f"{offenders}. Route all tag wording through "
        "setforge.local_overlay.display_tag."
    )


def test_display_tag_local_add_single_source_of_truth_in_codebase() -> None:
    """Assert no other module constructs ``[from local.yaml]`` (ADD tag).

    SPEC 2 mandates that all overlay-provenance tag wording (ADD and
    REMOVE) route through :func:`display_tag` — never inlined as
    f-string or bare string-literal constructions at the call site.
    This complements the REMOVE parity test above and closes the
    loophole that let ``setforge/cli/profile.py:_tag_provenance`` build
    the bare ADD tag inline (caught by Phase 5 review).

    Two legitimate definition sites are allow-listed:

    - ``setforge/local_overlay.py`` — SPEC 2 SoT (plugin / extension
      / marketplace overlay tags).
    - ``setforge/preserved_keys.py`` — SPEC 8 SoT (``preserve_user_keys``
      overlay tag — independent concern with the same wording, declared
      separately by design).

    Every other ``setforge/**/*.py`` MUST source the literal via
    :func:`setforge.local_overlay.display_tag` (or
    :func:`setforge.preserved_keys.display_tag` for SPEC 8 callers).

    Hardened (round 4): the original implementation walked only
    :class:`ast.Constant` string nodes and substring-matched ``.value``.
    That missed the regression shape it was *intended* to catch — an
    f-string like ``f"[from {src}]"`` with ``src="local.yaml"`` AST-
    decomposes into a :class:`ast.JoinedStr` whose children are
    ``Constant("[from ")``, ``FormattedValue(...)``, ``Constant("]")`` —
    no individual Constant contains the full literal, so substring
    matching against Constant.value silently passed.

    This version also walks :class:`ast.JoinedStr` nodes via
    :func:`_stringify_joined` (renders interpolations as ``"{}"``
    placeholders, concatenates Constant segments) and substring-matches
    the rendered text. Empirically validated by re-injecting
    ``f'[from {src}]'`` in ``setforge/cli/profile.py:_tag_provenance``
    before this change — the original test passed; after this change
    it correctly fails.
    """
    import setforge

    pkg_root = Path(setforge.__file__).parent
    add_tag = "[from local.yaml]"
    allow = {"local_overlay.py", "preserved_keys.py"}
    offenders: list[tuple[Path, int]] = []
    for py in pkg_root.rglob("*.py"):
        if py.name in allow:
            continue
        text = py.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(py))
        docstring_ids = _collect_docstring_ids(tree)
        for lineno in _scan_for_tag_substring(
            tree,
            add_tag,
            docstring_ids,
            fstring_smell=_add_tag_fstring_smell,
        ):
            offenders.append((py, lineno))
    assert not offenders, (
        f"SPEC 2 ADD tag literal constructed outside display_tag(): "
        f"{offenders}. Route all tag wording through "
        "setforge.local_overlay.display_tag (SPEC 2) or "
        "setforge.preserved_keys.display_tag (SPEC 8)."
    )


# ---------------------------------------------------------------------------
# has_local_overlay gate
# ---------------------------------------------------------------------------


def test_has_local_overlay_true_for_local_add_only() -> None:
    assert has_local_overlay(
        [
            ResolvedPlugin("a", OverlayOrigin.PROFILE),
            ResolvedPlugin("x", OverlayOrigin.LOCAL_ADD),
        ]
    )


def test_has_local_overlay_true_for_local_remove_only() -> None:
    assert has_local_overlay([ResolvedPlugin("a", OverlayOrigin.LOCAL_REMOVE)])


def test_has_local_overlay_false_for_all_profile() -> None:
    assert not has_local_overlay(
        [
            ResolvedPlugin("a", OverlayOrigin.PROFILE),
            ResolvedPlugin("b", OverlayOrigin.PROFILE),
        ]
    )


def test_has_local_overlay_false_for_empty_list() -> None:
    assert not has_local_overlay([])
