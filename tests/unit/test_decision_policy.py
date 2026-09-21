"""§21.2 / §11: the deterministic decision policy.

This is the component §24.8 names directly: a Critic's free-text suggestion must
never become a paid call. So the tests here focus on two things:

  1. the hard-reject conditions can never produce an automatic accept
  2. a degraded Critic can never authorise spend
  3. the policy ignores the Critic's own `verdict` when it contradicts the scores
"""

from __future__ import annotations

import pytest
from tests.conftest import make_report

from omni_homevlog.agents.decision_policy import (
    Decision,
    decide_final,
    decide_segment,
)
from omni_homevlog.budget import Budget
from omni_homevlog.config import QualityThresholds

THRESHOLDS = QualityThresholds()


def budget(**kwargs) -> Budget:
    defaults = {
        "max_total_calls": 10,
        "max_video_seconds_requested": 120,
        "max_seed_attempts": 2,
        "max_edit_attempts_per_segment": 1,
        "max_regenerations_per_segment": 1,
    }
    defaults.update(kwargs)
    return Budget(**defaults)


def decide(report, *, is_extension=False, budget_=None, **kwargs):
    return decide_segment(
        report=report,
        segment=kwargs.pop("segment", None),
        budget=budget_ if budget_ is not None else budget(),
        segment_index=kwargs.pop("segment_index", 0),
        attempt_index=kwargs.pop("attempt_index", 0),
        is_extension=is_extension,
        thresholds=THRESHOLDS,
        **kwargs,
    )


# ── the clean case ─────────────────────────────────────────────────────────


def test_a_passing_report_is_accepted() -> None:
    result = decide(make_report())
    assert result.decision is Decision.ACCEPT
    assert result.critic_verdict_agrees is True


def test_a_passing_extension_is_extended() -> None:
    result = decide(make_report(), is_extension=True)
    assert result.decision is Decision.EXTEND


def test_accept_requires_every_score_to_clear_its_threshold() -> None:
    for field, threshold in (
        ("identity_score", THRESHOLDS.accept_identity),
        ("anatomy_score", THRESHOLDS.accept_anatomy),
        ("motion_score", THRESHOLDS.accept_motion),
        ("camera_realism_score", THRESHOLDS.accept_camera_realism),
        ("spatial_continuity_score", THRESHOLDS.accept_spatial_continuity),
        ("reference_fidelity_score", THRESHOLDS.accept_reference_fidelity),
    ):
        result = decide(make_report(**{field: threshold - 0.10}))
        assert result.decision is not Decision.ACCEPT, f"{field} below threshold was accepted"
        assert result.decision is not Decision.EXTEND


# ── hard rejects (§11.3) ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("flag", "description"),
    [
        ("timestamp_detected", "a timestamp"),
        ("ui_detected", "player UI"),
        ("text_overlay_detected", "on-screen text"),
        ("montage_detected", "a montage"),
    ],
)
def test_hard_reject_flags_never_accept(flag: str, description: str) -> None:
    """A perfect score card cannot rescue a burned-in artefact."""
    report = make_report(**{flag: True})
    result = decide(report)
    assert result.decision is not Decision.ACCEPT
    assert result.decision is not Decision.EXTEND
    assert result.blocking, f"{description} produced no blocking reason"


def test_severe_defects_block_accept() -> None:
    report = make_report(severe_defects=["the subject became a different person"])
    result = decide(report)
    assert result.decision is Decision.REGENERATE  # a fresh render is the right remedy
    assert "the subject became a different person" in result.blocking


def test_hard_reject_with_no_budget_goes_to_a_human() -> None:
    report = make_report(timestamp_detected=True, identity_score=0.10)
    result = decide(report, budget_=budget(max_total_calls=1, max_regenerations_per_segment=0))
    assert result.decision is Decision.HUMAN_REVIEW


def test_timestamp_with_a_good_shot_prefers_regeneration_over_an_edit() -> None:
    """A timestamp is a hard reject, so §11.3 forbids the automatic path entirely."""
    report = make_report(timestamp_detected=True)
    result = decide(report)
    assert result.decision in (Decision.REGENERATE, Decision.HUMAN_REVIEW)
    assert result.decision is not Decision.ACCEPT


# ── degraded critic ────────────────────────────────────────────────────────


def test_a_degraded_review_cannot_authorise_spend() -> None:
    """The Critic saw too little to judge; its scores are not evidence."""
    report = make_report(critic_degraded=True, notes=["review degraded: only 1 frame"])
    result = decide(report)
    assert result.decision is Decision.HUMAN_REVIEW
    assert any("degraded" in reason for reason in result.reasons)


def test_a_degraded_review_is_not_overridden_by_a_good_verdict() -> None:
    report = make_report(critic_degraded=True, verdict="accept")
    result = decide(report)
    assert result.decision is Decision.HUMAN_REVIEW


# ── regenerate vs edit vs human (§11.4) ────────────────────────────────────


def test_a_face_swap_triggers_regeneration() -> None:
    report = make_report(identity_score=0.30, verdict="regenerate")
    result = decide(report)
    assert result.decision is Decision.REGENERATE
    assert any("below" in reason for reason in result.reasons)


def test_severe_anatomy_failure_triggers_regeneration() -> None:
    report = make_report(anatomy_score=0.30)
    result = decide(report)
    assert result.decision is Decision.REGENERATE


def test_a_spatial_reset_triggers_regeneration() -> None:
    report = make_report(spatial_continuity_score=0.30)
    result = decide(report)
    assert result.decision is Decision.REGENERATE


def test_a_single_local_defect_on_an_otherwise_good_shot_is_edited() -> None:
    report = make_report(
        anatomy_score=0.70,  # below the accept floor, above the safe-edit floor
        editable_defects=["one hand has six fingers"],
        suggested_edit_prompt="correct the hand so it has five natural fingers",
        verdict="edit",
    )
    result = decide(report)
    assert result.decision is Decision.EDIT
    assert result.suggested_edit


def test_an_edit_is_refused_when_the_identity_is_only_marginal() -> None:
    """Editing risks re-rendering the face; a marginal identity is not worth it."""
    report = make_report(
        identity_score=0.60,
        anatomy_score=0.70,
        editable_defects=["one hand has six fingers"],
        suggested_edit_prompt="fix the hand",
        verdict="edit",
    )
    result = decide(report)
    assert result.decision is not Decision.EDIT


def test_multiple_editable_defects_are_not_edited() -> None:
    """§9.1: an edit changes exactly one thing."""
    report = make_report(
        anatomy_score=0.70,
        editable_defects=["six fingers", "a floating cup"],
        suggested_edit_prompt="fix the hand and the cup",
        verdict="edit",
    )
    result = decide(report)
    assert result.decision is not Decision.EDIT


def test_no_edit_capability_falls_through_to_regeneration() -> None:
    report = make_report(
        anatomy_score=0.70,
        editable_defects=["one hand has six fingers"],
        suggested_edit_prompt="fix the hand",
        verdict="edit",
    )
    result = decide(report, has_capability_to_edit=False)
    assert result.decision is Decision.REGENERATE


# ── instability ────────────────────────────────────────────────────────────


def test_scores_clustered_at_the_threshold_go_to_a_human() -> None:
    """Within the noise band, another paid call is as likely to hurt as help."""
    report = make_report(identity_score=THRESHOLDS.accept_identity + 0.01)
    result = decide(report)
    assert result.decision is Decision.HUMAN_REVIEW
    assert any("threshol" in reason for reason in result.reasons)


def test_a_comfortably_passing_report_is_not_flagged_unstable() -> None:
    report = make_report(identity_score=THRESHOLDS.accept_identity + 0.10)
    assert decide(report).decision is Decision.ACCEPT


# ── budget interaction ─────────────────────────────────────────────────────


def test_budget_refusal_downgrades_to_human_review_not_to_accept() -> None:
    report = make_report(identity_score=0.20)
    result = decide(
        report,
        budget_=budget(max_total_calls=1, max_regenerations_per_segment=0),
    )
    assert result.decision is Decision.HUMAN_REVIEW
    assert any("budget" in reason for reason in result.reasons)


def test_a_good_extension_without_extend_budget_is_accepted_not_extended() -> None:
    result = decide(
        make_report(),
        is_extension=True,
        budget_=budget(max_total_calls=100, max_video_seconds_requested=5),
    )
    assert result.decision is Decision.ACCEPT


def test_an_extension_with_no_extend_capability_is_accepted() -> None:
    result = decide(make_report(), is_extension=True, has_capability_to_extend=False)
    assert result.decision is Decision.ACCEPT


def test_an_unusable_anchor_sends_a_good_segment_to_a_human() -> None:
    """The segment is fine but cannot be continued from (see §18)."""
    result = decide(make_report(anchor_usable=False), is_extension=True)
    assert result.decision is Decision.HUMAN_REVIEW
    assert any("anchor" in reason for reason in result.reasons)


# ── the critic's verdict is advisory ───────────────────────────────────────


def test_a_critic_saying_accept_cannot_override_failing_scores() -> None:
    report = make_report(identity_score=0.20, verdict="accept")
    result = decide(report)
    assert result.decision is not Decision.ACCEPT
    assert result.critic_verdict_agrees is False
    assert any("Critic's own verdict" in reason for reason in result.reasons)


def test_a_critic_saying_regenerate_does_not_force_a_paid_call() -> None:
    """A passing report plus a regenerate verdict must not spend money."""
    report = make_report(verdict="regenerate")
    result = decide(report)
    assert result.decision is Decision.ACCEPT
    assert result.critic_verdict_agrees is False


def test_disagreement_is_recorded_rather_than_corrected() -> None:
    result = decide(make_report(verdict="human_review"))
    assert result.decision is Decision.ACCEPT
    assert result.critic_verdict_agrees is False


# ── determinism ────────────────────────────────────────────────────────────


def test_the_same_report_always_produces_the_same_decision() -> None:
    report = make_report(anatomy_score=0.70)
    results = {decide(report, budget_=budget(), segment_index=i).decision for i in range(5)}
    assert len(results) == 1


def test_thresholds_used_are_recorded_for_later_explanation() -> None:
    result = decide(make_report())
    assert result.thresholds_used["accept_identity"] == THRESHOLDS.accept_identity
    assert "calibrated_at" in result.thresholds_used


def test_explain_is_readable() -> None:
    result = decide(make_report(timestamp_detected=True))
    text = result.explain()
    assert str(result.decision) in text
    assert text.strip()


# ── final decision ─────────────────────────────────────────────────────────


def test_final_decision_gates_on_human_approval_by_default() -> None:
    result = decide_final(
        reports=[make_report() for _ in range(3)],
        budget=budget(),
        thresholds=THRESHOLDS,
        require_human_gate=True,
    )
    assert result.decision is Decision.HUMAN_REVIEW
    assert any("gate" in reason for reason in result.reasons)


def test_final_decision_accepts_when_the_gate_is_disabled() -> None:
    result = decide_final(
        reports=[make_report() for _ in range(3)],
        budget=budget(),
        thresholds=THRESHOLDS,
        require_human_gate=False,
    )
    assert result.decision is Decision.ACCEPT


def test_final_decision_refuses_with_no_reviews() -> None:
    result = decide_final(
        reports=[], budget=budget(), thresholds=THRESHOLDS, require_human_gate=False
    )
    assert result.decision is Decision.HUMAN_REVIEW


def test_final_decision_refuses_on_any_hard_reject() -> None:
    result = decide_final(
        reports=[make_report(), make_report(timestamp_detected=True)],
        budget=budget(),
        thresholds=THRESHOLDS,
        require_human_gate=False,
    )
    assert result.decision is Decision.HUMAN_REVIEW


def test_final_decision_refuses_on_a_degraded_review() -> None:
    result = decide_final(
        reports=[make_report(), make_report(critic_degraded=True)],
        budget=budget(),
        thresholds=THRESHOLDS,
        require_human_gate=False,
    )
    assert result.decision is Decision.HUMAN_REVIEW


def test_final_decision_refuses_when_an_anchor_is_unusable() -> None:
    result = decide_final(
        reports=[make_report(), make_report(anchor_usable=False)],
        budget=budget(),
        thresholds=THRESHOLDS,
        require_human_gate=False,
    )
    assert result.decision is Decision.HUMAN_REVIEW
