"""Meta-tests for the stateful-test harness scaffold (RFC 0001 §6, E1).

These tests exercise the HARNESS ITSELF, not setforge behavior. They are
the red-green gate for the scaffold: if they pass, a future per-component
task can register a real invariant and trust that

- the state machine drives install / sync / revert / migrate over
  Hypothesis-generated inputs without error,
- the invariant helper CATCHES a deliberately-violated invariant after
  the step that breaks it, and
- the fixtures isolate the filesystem and intercept the subprocess
  boundary.

The real INV-1..10 catalog lands in task E2 against these same helpers.
"""

from __future__ import annotations

import subprocess

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import rule, run_state_machine_as_test

from setforge.config import Config
from tests.harness import strategies as hstrat
from tests.harness.invariants import (
    InvariantStateMachine,
    InvariantViolation,
    invariant,
)
from tests.harness.model import StubReconcileModel

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@given(hstrat.configs())
def test_config_strategy_builds_valid_configs(config: Config) -> None:
    """Every generated config validates as a real ``Config`` model."""
    assert isinstance(config, Config)
    assert config.profiles, "a generated config always has >= 1 profile"
    # Every profile's tracked_files reference a declared tracked_file id.
    declared = set(config.tracked_files)
    for profile in config.profiles.values():
        assert set(profile.tracked_files) <= declared


@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@given(hstrat.tracked_file_contents())
def test_content_strategy_is_text(content: str) -> None:
    """The file-content strategy yields round-trippable unicode text."""
    assert isinstance(content, str)
    assert content == content.encode("utf-8").decode("utf-8")


@settings(max_examples=25)
@given(st.data())
def test_verb_strategy_only_emits_known_verbs(data: st.DataObject) -> None:
    """The command strategy only ever emits the four scaffolded verbs."""
    verb = data.draw(hstrat.verbs())
    assert verb in {"install", "sync", "revert", "migrate"}


# ---------------------------------------------------------------------------
# StubReconcileModel seam
# ---------------------------------------------------------------------------


def test_stub_model_drives_four_verbs(tmp_path) -> None:
    """The seam exposes install / sync / revert / migrate and mutates state."""
    model = StubReconcileModel.create(tmp_path)
    model.set_config(hstrat.minimal_config())
    model.install()
    assert model.store_index(), "install records a store index"
    model.sync()
    model.migrate()
    model.revert()
    # Every verb appended a transition snapshot; revert popped exactly one.
    assert model.transition_count() >= 1


def test_stub_model_store_layout_on_disk(tmp_path) -> None:
    """install materializes the RFC §9.3 base/ + local/ + index/ store dirs."""
    model = StubReconcileModel.create(tmp_path)
    model.set_config(hstrat.minimal_config())
    model.install()
    assert (model.state_dir / "base").is_dir()
    assert (model.state_dir / "local").is_dir()
    assert (model.state_dir / "index").is_dir()


# ---------------------------------------------------------------------------
# Invariant helper API
# ---------------------------------------------------------------------------


def test_invariant_decorator_registers_on_subclass() -> None:
    """The @invariant decorator is discoverable on the state-machine subclass."""

    class _M(InvariantStateMachine):
        model_factory = staticmethod(StubReconcileModel.create)

        @invariant()
        def _always_true(self) -> None:
            return None

    names = {inv.__name__ for inv in _M.registered_invariants()}
    assert "_always_true" in names


def test_invariant_helper_passes_when_holding(tmp_path) -> None:
    """assert_invariants is a no-op when every registered invariant holds."""

    class _M(InvariantStateMachine):
        model_factory = staticmethod(StubReconcileModel.create)

        @invariant()
        def _holds(self) -> None:
            assert self.model.transition_count() >= 0

    machine = _M(tmp_path)
    machine.model.set_config(hstrat.minimal_config())
    machine.model.install()
    machine.assert_invariants()  # must not raise


def test_invariant_helper_catches_violation(tmp_path) -> None:
    """A deliberately-violated toy invariant is CAUGHT as InvariantViolation.

    This is the load-bearing meta-test: it proves the helper actually
    fails the test when an invariant is broken, so a future real
    INV-1..10 cannot silently no-op.
    """

    class _Broken(InvariantStateMachine):
        model_factory = staticmethod(StubReconcileModel.create)

        @invariant()
        def _impossible(self) -> None:
            # Deliberately false: transition_count is never negative.
            assert self.model.transition_count() < 0, "toy violation"

    machine = _Broken(tmp_path)
    machine.model.set_config(hstrat.minimal_config())
    machine.model.install()
    with pytest.raises(InvariantViolation) as exc:
        machine.assert_invariants()
    assert "_impossible" in str(exc.value)


# ---------------------------------------------------------------------------
# Full RuleBasedStateMachine run over generated sequences
# ---------------------------------------------------------------------------


class _ScaffoldMachine(InvariantStateMachine):
    """The shipped skeleton: drives all four verbs, one passing invariant.

    A future task adds INV-1..10 as more @invariant methods; the @rule
    bodies stay as-is (the seam already supports the verbs).
    """

    model_factory = staticmethod(StubReconcileModel.create)

    @rule(config=hstrat.configs())
    def reconfigure(self, config: Config) -> None:
        self.model.set_config(config)

    @rule()
    def install(self) -> None:
        self.model.install()

    @rule()
    def sync(self) -> None:
        self.model.sync()

    @rule()
    def migrate(self) -> None:
        self.model.migrate()

    @rule()
    def revert(self) -> None:
        self.model.revert()

    @invariant()
    def _store_index_is_a_dict(self) -> None:
        # A real INV-10 (store index ↔ on-disk consistent) replaces this.
        assert isinstance(self.model.store_index(), dict)


def test_state_machine_runs_over_generated_sequences() -> None:
    """The state machine drives generated install/sync/revert/migrate runs."""
    run_state_machine_as_test(
        _ScaffoldMachine,
        settings=settings(
            max_examples=25,
            stateful_step_count=12,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )


def test_state_machine_violation_is_surfaced() -> None:
    """A broken invariant inside a state-machine run fails the run.

    Proves the @invariant hook is wired into the stateful driver, not
    just the standalone assert_invariants() path.
    """

    class _BrokenMachine(_ScaffoldMachine):
        @invariant()
        def _impossible(self) -> None:
            assert self.model.transition_count() < 0, "toy violation in machine"

    with pytest.raises(InvariantViolation):
        run_state_machine_as_test(
            _BrokenMachine,
            settings=settings(
                max_examples=25,
                stateful_step_count=12,
                suppress_health_check=[
                    HealthCheck.too_slow,
                    HealthCheck.filter_too_much,
                ],
            ),
        )


# ---------------------------------------------------------------------------
# Fixtures: fs isolation + subprocess-boundary mock
# ---------------------------------------------------------------------------


def test_isolated_state_home_fixture_isolates_fs(isolated_state_home) -> None:
    """The fixture redirects HOME + SETFORGE_STATE_DIR under tmp_path."""
    import os
    from pathlib import Path

    home = isolated_state_home.home
    state = isolated_state_home.state_dir
    assert Path.home() == home
    assert os.environ["SETFORGE_STATE_DIR"] == str(state)
    # Nothing leaks to the real dev-host home.
    assert str(home).startswith(str(isolated_state_home.root))


def test_mock_subprocess_intercepts_known_binary(mock_cli_subprocess) -> None:
    """pytest-subprocess intercepts the claude/git/code boundary."""
    mock_cli_subprocess.register(["claude", "plugin", "list", "--json"], stdout="[]")
    result = subprocess.run(
        ["claude", "plugin", "list", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "[]"


def test_mock_subprocess_blocks_unregistered_calls(mock_cli_subprocess) -> None:
    """An unregistered real-binary call raises rather than escaping the sandbox."""
    with pytest.raises(Exception):  # noqa: B017,PT011 (process-mock raises its own type)
        subprocess.run(["some-unregistered-binary", "--go"], check=True)
