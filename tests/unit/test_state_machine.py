"""§21.1: illegal state transitions.

The state machine is the component that must not be persuadable. If it can be
talked into skipping a review, the budget and the Critic are both bypassed.
"""

from __future__ import annotations

import itertools

import pytest

from omni_homevlog.errors import StateTransitionError
from omni_homevlog.schemas import TERMINAL_STATES, JobState
from omni_homevlog.state_machine import (
    UNIVERSAL_TARGETS,
    assert_transition,
    build_transitions,
    describe_path,
    is_terminal,
    legal_transitions,
)


def test_thirty_second_path_matches_the_plan_diagram() -> None:
    """§5's worked example, exactly."""
    expected = [
        "CREATED",
        "REFERENCES_VALIDATED",
        "PLAN_READY",
        "SEED_RENDERING",
        "SEED_REVIEW",
        "SEED_ACCEPTED",
        "EXTENSION_1_RENDERING",
        "EXTENSION_1_REVIEW",
        "EXTENSION_1_ACCEPTED",
        "EXTENSION_2_RENDERING",
        "EXTENSION_2_REVIEW",
        "EXTENSION_2_ACCEPTED",
        "FINAL_REVIEW",
        "COMPLETE",
    ]
    assert describe_path(2) == expected


@pytest.mark.parametrize(
    ("duration_s", "extensions"),
    [(10, 0), (20, 1), (30, 2), (40, 3)],
)
def test_happy_path_is_fully_walkable(duration_s: int, extensions: int) -> None:
    path = describe_path(extensions)
    for current, target in itertools.pairwise(path):
        # Throws if the path the machine advertises is not actually legal.
        assert_transition(JobState(current), JobState(target), extension_count=extensions)
    assert duration_s <= 40


def test_skipping_a_review_is_illegal() -> None:
    with pytest.raises(StateTransitionError):
        assert_transition(JobState.SEED_RENDERING, JobState.SEED_ACCEPTED, extension_count=2)


def test_extending_before_the_seed_is_accepted_is_illegal() -> None:
    with pytest.raises(StateTransitionError):
        assert_transition(
            JobState.PLAN_READY,
            JobState.EXTENSION_1_RENDERING,
            extension_count=2,
        )


def test_a_ten_second_job_cannot_reach_an_extension_state() -> None:
    with pytest.raises(StateTransitionError):
        assert_transition(
            JobState.SEED_ACCEPTED,
            JobState.EXTENSION_1_RENDERING,
            extension_count=0,
        )


def test_terminal_states_lead_nowhere() -> None:
    for terminal in TERMINAL_STATES:
        assert is_terminal(terminal)
        assert legal_transitions(terminal, 2) == set()
        with pytest.raises(StateTransitionError, match="terminal"):
            assert_transition(terminal, JobState.CREATED, extension_count=2)


def test_exception_states_are_reachable_from_anywhere_non_terminal() -> None:
    """A run can fail at any point, so this must not be an illegal move."""
    for state in build_transitions(2):
        if state in TERMINAL_STATES:
            continue
        for target in UNIVERSAL_TARGETS:
            assert_transition(state, target, extension_count=2)


def test_noop_transition_is_allowed_by_default() -> None:
    assert_transition(JobState.SEED_REVIEW, JobState.SEED_REVIEW, extension_count=2)


def test_noop_can_be_forbidden() -> None:
    with pytest.raises(StateTransitionError):
        assert_transition(
            JobState.SEED_REVIEW,
            JobState.SEED_REVIEW,
            extension_count=2,
            allow_noop=False,
        )


def test_review_can_send_a_segment_back_for_re_rendering() -> None:
    assert_transition(JobState.SEED_REVIEW, JobState.SEED_RENDERING, extension_count=2)
    assert_transition(
        JobState.EXTENSION_1_REVIEW,
        JobState.EXTENSION_1_RENDERING,
        extension_count=2,
    )


def test_three_extensions_chain_correctly() -> None:
    assert_transition(
        JobState.EXTENSION_2_ACCEPTED,
        JobState.EXTENSION_3_RENDERING,
        extension_count=3,
    )
    assert_transition(JobState.EXTENSION_3_ACCEPTED, JobState.FINAL_REVIEW, extension_count=3)


def test_negative_extension_count_is_refused() -> None:
    with pytest.raises(ValueError, match="extension_count"):
        build_transitions(-1)


def test_recovery_edges_are_deliberate_not_automatic() -> None:
    """NEEDS_HUMAN can re-enter work, but only through these specific edges."""
    allowed = legal_transitions(JobState.NEEDS_HUMAN, 2)
    assert JobState.SEED_RENDERING in allowed
    assert JobState.EXTENSION_1_RENDERING in allowed
    # It must not be able to jump straight to COMPLETE.
    assert JobState.COMPLETE not in allowed


def test_budget_exhausted_cannot_resume_automatically() -> None:
    allowed = legal_transitions(JobState.BUDGET_EXHAUSTED, 2)
    assert JobState.SEED_RENDERING not in allowed
    assert JobState.NEEDS_HUMAN in allowed
    assert JobState.FAILED_FINAL in allowed


def test_every_state_in_the_enum_appears_in_the_table() -> None:
    table = build_transitions(3)
    missing = [s for s in JobState if s not in table]
    assert not missing, f"states with no transitions defined: {missing}"
