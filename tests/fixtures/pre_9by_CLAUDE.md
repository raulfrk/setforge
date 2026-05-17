# setforge global preferences (pre-9by fixture)

This fixture mimics a real `~/.claude/CLAUDE.md` from before the
dotfiles-9by parser tightening: every user-section marker is missing
the `host-local`/`shared` semantics keyword, and every end marker is
missing the `hash=<sha256>` segment. The strict parser must refuse to
read this; the `allow_legacy=True` migration mode must accept it.

<!-- setforge:user-section start workflow -->
- Stay focused on the contract from the bd issue.
- Tier self-review by blast radius — leaf code is spot-check; auth and
  data pipelines get line-by-line.
- Keep changes small. A 200-line diff understood beats 1500 skimmed.
<!-- setforge:user-section end workflow -->

Some surrounding prose unrelated to user-sections.

<!-- setforge:user-section start commits -->
- Subject: imperative mood, capitalized, no period.
- Body required only when the diff is not self-evident.
- One logical change per commit.
<!-- setforge:user-section end commits -->

End of fixture.
