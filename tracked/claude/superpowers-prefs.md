# Superpowers compatibility

The Superpowers framework is installed alongside the custom Socratic output
style. My preferences override Superpowers' defaults wherever they conflict.
This file is imported from CLAUDE.md so the directives ride the
`<system-reminder>` "OVERRIDE default behavior" wrapper, which Superpowers
is designed to defer to.

## Path overrides

- Save specs to `~/.claude/projects/{cwd-slug}/specs/<YYYY-MM-DD>-<topic>.md`,
  not the in-repo `docs/superpowers/specs/` default.
- Save plans to `~/.claude/projects/{cwd-slug}/plans/<YYYY-MM-DD>-<feature>.md`,
  not the in-repo default.
- `{cwd-slug}` matches the auto-memory convention — the current working
  directory with `/` replaced by `-` (e.g. `-home-raul-dotfiles`).

## Soften the hard-gates

- Do not auto-create git worktrees. I'll branch myself when I want a branch.
- Do not enforce TDD by default. Use it only when I explicitly ask for it.
- Do not dispatch implementation to subagents by default.
- Skip the full brainstorm → spec → plan workflow for single-file or
  sub-100-line mechanical changes. For those, the Socratic output style
  stance is sufficient on its own.

## Stance precedence

The Socratic output style is the primary working stance. Superpowers skills
provide artifact templates and structured workflows; they do not replace
the stance. When a Superpowers skill's instructions conflict with the
Socratic stance (e.g. a `<HARD-GATE>` competing with the "skip questions
for trivial work" escape hatch), prefer the Socratic stance unless I
explicitly invoke the formal Superpowers flow.

## Break-character signals

If I say "just do it," "skip the questions," "spec this properly," or I
explicitly invoke a Superpowers skill by name, drop the Socratic stance
for that task and follow the skill's flow as designed. Resume the Socratic
stance for the next task unless I extend the override.
