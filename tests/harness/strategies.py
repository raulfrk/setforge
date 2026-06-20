"""Hypothesis strategies for valid-ish setforge config inputs (RFC 0001 §6).

Composable building blocks the invariant state machine draws from. Every
:func:`configs` example validates as a real :class:`setforge.config.Config`,
so a generated input can be fed straight into the reconcile seam without a
separate "is this loadable?" guard.

Composition map (leaf → aggregate):

    identifiers()            tracked-file ids / profile names / package names
    rel_src_paths()          tracked/<src> source paths
    dst_templates()          ~-rooted dst path templates
    dispositions()           Disposition | None
    tracked_files()          dict[str, TrackedFile]
    profiles()               dict[str, Profile] (refs only declared ids)
    configs()                Config (the aggregate the state machine uses)

    tracked_file_contents()  file *bodies* (the bytes a verb reconciles)
    verbs()                  install | sync | revert | migrate

These are deliberately conservative (valid-ish, not adversarial): the
scaffold's job is to be runnable and meta-tested. A later task widens the
strategies toward the edge cases each INV must survive (unicode, CRLF,
trailing-newline drift, deep ``extends`` chains, span anchors).
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import strategies as st

from setforge.config import (
    Config,
    Disposition,
    Profile,
    TrackedFile,
)

# A small, stable identifier alphabet: lowercase + digit + underscore,
# always starting with a letter. Keeps generated ids human-readable in a
# falsifying example and dodges the control-char path validator.
_IDENT = st.from_regex(r"[a-z][a-z0-9_]{0,11}", fullmatch=True)


def identifiers() -> st.SearchStrategy[str]:
    """Tracked-file ids, profile names, and package names."""
    return _IDENT


def rel_src_paths() -> st.SearchStrategy[Path]:
    """A ``tracked/<segment>[/<segment>]`` relative source path."""
    return st.lists(_IDENT, min_size=1, max_size=3).map(
        lambda parts: Path("tracked", *parts)
    )


def dst_templates() -> st.SearchStrategy[str]:
    """A ``~``-rooted destination path template."""
    return st.lists(_IDENT, min_size=1, max_size=3).map(
        lambda parts: "~/" + "/".join(parts)
    )


def dispositions() -> st.SearchStrategy[Disposition | None]:
    """A file-level reconciliation policy, or ``None`` (verbatim deploy)."""
    return st.sampled_from([None, *list(Disposition)])


def tracked_file_contents() -> st.SearchStrategy[str]:
    """A file body — the bytes a verb reconciles.

    Restricted to printable + newline text so the scaffold round-trips
    cleanly through utf-8 and the unified-diff transition format. A later
    task widens this toward CRLF / trailing-newline / unicode edge cases
    the byte-exact invariants (INV-2/3/6) must survive.
    """
    return st.text(
        alphabet=st.characters(
            min_codepoint=0x20,
            max_codepoint=0x7E,
        )
        | st.just("\n"),
        max_size=200,
    )


@st.composite
def tracked_files(draw: st.DrawFn) -> dict[str, TrackedFile]:
    """A non-empty ``{id: TrackedFile}`` registry with unique dsts.

    dst uniqueness matters because two tracked_files deploying to the
    same live path is a config-authoring error the real engine would
    reject; keeping the generated registry clean avoids spurious
    falsifying examples that test the harness against invalid input.
    """
    ids = draw(st.lists(_IDENT, min_size=1, max_size=4, unique=True))
    registry: dict[str, TrackedFile] = {}
    seen_dsts: set[str] = set()
    for tf_id in ids:
        dst = draw(dst_templates().filter(lambda d: d not in seen_dsts))
        seen_dsts.add(dst)
        registry[tf_id] = TrackedFile(
            src=draw(rel_src_paths()),
            dst=dst,
            disposition=draw(dispositions()),
        )
    return registry


@st.composite
def profiles(draw: st.DrawFn, tracked_file_ids: list[str]) -> dict[str, Profile]:
    """A ``{name: Profile}`` map whose ``tracked_files`` ref only known ids.

    Profiles never declare an ``extends`` here (the scaffold keeps the
    chain flat); a later task adds inheritance-chain generation for the
    ``resolve_profile`` edge cases.
    """
    names = draw(st.lists(_IDENT, min_size=1, max_size=3, unique=True))
    out: dict[str, Profile] = {}
    for name in names:
        refs = draw(
            st.lists(
                st.sampled_from(tracked_file_ids),
                max_size=len(tracked_file_ids),
                unique=True,
            )
        )
        out[name] = Profile(tracked_files=refs)
    return out


@st.composite
def configs(draw: st.DrawFn) -> Config:
    """A fully-valid :class:`Config` the state machine can reconcile.

    Guarantees: >= 1 tracked_file, >= 1 profile, every profile's
    ``tracked_files`` references a declared id. schema_version is left at
    the model default so :func:`setforge.config.load_config`'s guard does
    not fire.
    """
    registry = draw(tracked_files())
    profile_map = draw(profiles(list(registry)))
    return Config(tracked_files=registry, profiles=profile_map)


def verbs() -> st.SearchStrategy[str]:
    """The four state-changing verbs the scaffold drives."""
    return st.sampled_from(["install", "sync", "revert", "migrate"])


def minimal_config() -> Config:
    """A deterministic minimal config for non-Hypothesis unit tests.

    Not a strategy — a plain fixed value the meta-tests use when they
    need a known config rather than a generated one.
    """
    return Config(
        tracked_files={
            "claude_md": TrackedFile(src=Path("tracked/claude/CLAUDE.md"), dst="~/c")
        },
        profiles={"default": Profile(tracked_files=["claude_md"])},
    )
