---
name: challenge
description: Run adversarial agents to stress-test a load-bearing decision (architecture, design choice, technology pick, anything irreversible or expensive to undo). Activates when the user says "challenge this", "/challenge", "adversarial review", "stress test this", or asks for a critique of a proposal. NOT for routine implementation choices, bug fixes, or anything well-covered by an existing accepted plan.
---

# Challenge — adversarial review

Use to stress-test load-bearing decisions before they're committed. Output is a synthesized critique with attributable objections, not a vote.

## When to use

- The user says: "challenge this", "/challenge", "adversarial review", "stress test this", "second opinion".
- A load-bearing decision is on the table: architecture, technology pick, public API shape, data model, anything expensive to reverse.

## When NOT to use

- Routine implementation choices already inside an accepted plan.
- Linting / formatting / naming arguments.
- Bug fixes where the root cause is known.
- The user can ask for `challenge`; do not volunteer it for non-load-bearing work.

## How to run

1. **State the proposal back in one sentence.** The user and the agents must agree on what's being reviewed before any agent dispatches. Disagreement here means stop and clarify.
2. **Form your own opinion BEFORE dispatching.** Adversarial agents are stress-tests, not substitutes for your reasoning. Write down your own top concern as an internal anchor.
3. **Dispatch 2-4 parallel agents** (`general-purpose` subagent type, single message with multiple Agent tool calls). Each agent gets:
   - The proposal in full, including relevant code paths and constraints. Self-contained — they don't see this conversation.
   - An *independent angle*: feasibility, hidden coupling, future-flexibility cost, alternative approach, operational/security/perf risk. Pick angles that don't overlap.
   - A length cap (300-500 words) and a directive to be specific (cite line numbers, name the failure mode), not generic.
4. **Synthesize.** Produce:
   - The 2-3 strongest objections, attributed to which agent raised each.
   - The strongest alternative approach surfaced (if any).
   - A recommendation: proceed as-is / adjust X / reconsider entirely.
5. **Hand back to the user.** They make the call.

## Anti-patterns

- Running >4 agents — diminishing returns, high token cost.
- Letting agents write the code instead of critiquing the plan.
- Burying disagreement — surface conflicts between agents, don't average them out.
- Removing the original critic's specificity ("agent A said X" beats "some concerns were raised").
- Dispatching after the decision is already implemented (this is a planning-phase tool).
