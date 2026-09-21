"""§21.1: budget authorisation and refusal.

The plan's §12.2 rule is that every new call passes through `authorize` first, and
§24.8 is that no model output may increase the budget. So these tests are about
the arithmetic holding under pressure.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omni_homevlog.budget import Budget, CallKind, budget_for_mode
from omni_homevlog.errors import BudgetExhaustedError
from omni_homevlog.schemas import BudgetEvent


def test_authorize_grants_then_refuses_at_the_ceiling() -> None:
    budget = Budget(max_total_calls=2, max_video_seconds_requested=60)

    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0, video_seconds=10)
    budget.authorize(CallKind.EXTEND, segment_index=1, attempt_index=0, video_seconds=10)

    with pytest.raises(BudgetExhaustedError, match="call ceiling"):
        budget.authorize(CallKind.EXTEND, segment_index=2, attempt_index=0, video_seconds=10)


def test_refusal_records_a_deny_event() -> None:
    budget = Budget(max_total_calls=1)
    events: list[BudgetEvent] = []
    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0, events=events)

    with pytest.raises(BudgetExhaustedError):
        budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0, events=events)

    assert [e.kind for e in events] == ["authorize", "deny"]
    assert events[1].reason is not None


def test_video_seconds_ceiling_binds_independently_of_calls() -> None:
    budget = Budget(max_total_calls=10, max_video_seconds_requested=15, max_seed_attempts=5)
    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0, video_seconds=10)

    with pytest.raises(BudgetExhaustedError, match="video-seconds"):
        budget.authorize(CallKind.SEED, segment_index=0, attempt_index=1, video_seconds=10)


def test_edit_ceiling_is_per_segment() -> None:
    budget = Budget(max_total_calls=10, max_edit_attempts_per_segment=1)
    budget.authorize(CallKind.EDIT, segment_index=0, attempt_index=1)

    with pytest.raises(BudgetExhaustedError, match="edit ceiling reached for segment 0"):
        budget.authorize(CallKind.EDIT, segment_index=0, attempt_index=2)

    # A different segment is unaffected.
    budget.authorize(CallKind.EDIT, segment_index=1, attempt_index=1)


def test_regenerate_ceiling_is_per_segment() -> None:
    budget = Budget(max_total_calls=10, max_regenerations_per_segment=1)
    budget.authorize(CallKind.REGENERATE, segment_index=0, attempt_index=1, video_seconds=10)

    with pytest.raises(BudgetExhaustedError, match="regenerate ceiling"):
        budget.authorize(CallKind.REGENERATE, segment_index=0, attempt_index=2, video_seconds=10)


def test_seed_ceiling_is_per_job() -> None:
    budget = Budget(max_total_calls=10, max_seed_attempts=2)
    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0, video_seconds=10)
    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=1, video_seconds=10)

    with pytest.raises(BudgetExhaustedError, match="seed attempt ceiling"):
        budget.authorize(CallKind.SEED, segment_index=0, attempt_index=2, video_seconds=10)


def test_extend_ceiling_is_two_per_segment() -> None:
    """One planned extension plus at most one repair."""
    budget = Budget(max_total_calls=10, max_video_seconds_requested=100)
    budget.authorize(CallKind.EXTEND, segment_index=1, attempt_index=0, video_seconds=10)
    budget.authorize(CallKind.EXTEND, segment_index=1, attempt_index=1, video_seconds=10)

    with pytest.raises(BudgetExhaustedError, match="extend ceiling"):
        budget.authorize(CallKind.EXTEND, segment_index=1, attempt_index=2, video_seconds=10)


def test_cost_ceiling_is_checked_before_the_call() -> None:
    budget = Budget(
        max_total_calls=10,
        max_estimated_cost_usd=Decimal("1.00"),
    )
    budget.authorize(
        CallKind.SEED,
        segment_index=0,
        attempt_index=0,
        video_seconds=10,
        estimated_cost_usd=Decimal("0.80"),
    )

    with pytest.raises(BudgetExhaustedError, match="cost ceiling"):
        budget.authorize(
            CallKind.EXTEND,
            segment_index=1,
            attempt_index=0,
            video_seconds=10,
            estimated_cost_usd=Decimal("0.80"),
        )


def test_check_is_non_raising_and_matches_authorize() -> None:
    budget = Budget(max_total_calls=1)
    allowed, why = budget.check(CallKind.SEED, segment_index=0, video_seconds=10)
    assert allowed and why == "ok"

    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0, video_seconds=10)
    allowed, why = budget.check(CallKind.SEED, segment_index=0, video_seconds=10)
    assert not allowed
    assert "ceiling" in why


def test_unknown_outcome_is_recorded_and_still_counted() -> None:
    """§5.1: an unobserved request is not forgiven, and is not retried."""
    budget = Budget(max_total_calls=5)
    events: list[BudgetEvent] = []
    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0, events=events)
    budget.note_unknown_outcome(events=events)

    assert budget.calls_made == 1
    assert any(e.kind == "refund_unknown" for e in events)
    unknown = next(e for e in events if e.kind == "refund_unknown")
    assert "billable" in (unknown.reason or "")


def test_concept_mode_is_much_cheaper_than_production() -> None:
    concept = budget_for_mode(mode="concept", target_duration_s=30)
    production = budget_for_mode(mode="production", target_duration_s=30)

    assert concept.max_total_calls <= 2
    assert concept.max_edit_attempts_per_segment == 0
    assert production.max_total_calls > concept.max_total_calls


def test_production_budget_scales_with_the_chain_length() -> None:
    short = budget_for_mode(mode="production", target_duration_s=10)
    long = budget_for_mode(mode="production", target_duration_s=40)

    assert long.max_video_seconds_requested >= short.max_video_seconds_requested


def test_budget_snapshot_is_json_friendly() -> None:
    budget = Budget(max_total_calls=3)
    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0, video_seconds=10)
    snapshot = budget.snapshot()

    assert snapshot["calls_made"] == 1
    assert snapshot["remaining_calls"] == 2
    assert isinstance(snapshot["estimated_cost_usd"], str)


def test_private_counter_does_not_leak_into_the_serialised_model() -> None:
    budget = Budget(max_total_calls=3)
    budget.authorize(CallKind.SEED, segment_index=0, attempt_index=0)
    dumped = budget.model_dump()
    assert "_by_kind_segment" not in dumped
