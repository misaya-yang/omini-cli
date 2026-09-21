"""Decision policy (§11.3–§11.5).

This is the component that decides whether money gets spent. It is pure,
deterministic Python, and §24.8 is explicit about why: "不得把 Critic 自由文本直接执行为
花钱调用" — a Critic's free-text suggestion must never be executed as a paid call.

So the Critic's own `verdict` is **advisory only**. This module re-derives the
decision from the report's scores and flags against configured thresholds, and
that derived decision is what the pipeline acts on. Where the two disagree, the
disagreement is recorded in the manifest — a Critic that keeps saying "accept" on
defective footage is a signal about the Critic, and it should be visible rather
than silently overridden.

Order of evaluation matters and follows §11.3/§11.4:

  1. hard rejects (§11.3) — no automatic accept, full stop
  2. score floors — regenerate when the shot is fundamentally wrong
  3. accept, when everything clears
  4. edit, only when the shot is otherwise good and the defect is single and local
  5. human review for every remaining case, including instability, exhaustion, and
     a degraded Critic

Anything that could spend money is gated on the budget, and the *reason string* is
returned alongside the decision so `omni-vlog status` can explain itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from omni_homevlog.budget import Budget, CallKind
from omni_homevlog.config import QualityThresholds, load_thresholds
from omni_homevlog.schemas import CritiqueReport, SegmentPlan

#: Seconds an edit re-renders. Used to size the pre-flight reservation check.
EDIT_RENDER_SECONDS = 10

#: §11.3 hard-reject conditions. Each corresponds to a bullet in the plan.
HARD_REJECT_SIGNALS: tuple[tuple[str, str], ...] = (
    ("timestamp_detected", "a timestamp was burned into the picture"),
    ("ui_detected", "player interface or storyboard borders are visible"),
    ("text_overlay_detected", "on-screen text, captions, or shot numbers are visible"),
    ("montage_detected", "the output reads as a montage, slideshow, or split screen"),
)


class Decision(StrEnum):
    ACCEPT = "ACCEPT"
    EDIT = "EDIT"
    REGENERATE = "REGENERATE"
    EXTEND = "EXTEND"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(slots=True)
class DecisionResult:
    decision: Decision
    reasons: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    suggested_edit: str | None = None
    critic_verdict_agrees: bool | None = None
    #: True when the only reason for HUMAN_REVIEW is that a configured gate wants
    #: an approval, and nothing is actually wrong. The two cases need different
    #: handling: a gate means "wait where you are and let them approve", whereas a
    #: quality failure means "stop and bring someone in".
    gate_only: bool = False
    #: Thresholds used, recorded so a past decision stays explainable after the
    #: config file is retuned.
    thresholds_used: dict[str, float | str] = field(default_factory=dict)

    @property
    def spends_money(self) -> bool:
        return self.decision in (Decision.EDIT, Decision.REGENERATE, Decision.EXTEND)

    def explain(self) -> str:
        head = f"{self.decision}"
        if self.reasons:
            head += ": " + "; ".join(self.reasons)
        if self.blocking:
            head += " | blocked by: " + "; ".join(self.blocking)
        return head


def _hard_reject_reasons(report: CritiqueReport) -> list[str]:
    reasons: list[str] = []
    for attr, description in HARD_REJECT_SIGNALS:
        if getattr(report, attr, False):
            reasons.append(description)
    if report.severe_defects:
        reasons.extend(report.severe_defects)
    return reasons


def _score_failures(
    report: CritiqueReport, t: QualityThresholds, *, is_extension: bool = False
) -> list[str]:
    """Every score below its threshold.

    `continuity_with_previous_score` is checked only for extensions. A seed has no
    previous segment, so the Critic reports 1.0 for it by instruction; checking it
    on a seed would be meaningless rather than harmless, because it would mask a
    genuine 0.0 from a Critic that ignored the instruction.

    Both continuity scores were previously omitted entirely. That is the failure
    §18 exists to prevent: an extension whose take resets the room and the outfit
    scores 0.05 on continuity, clears the other six, and would have been accepted.
    The thresholds were in `quality_thresholds.yaml` the whole time, simply never
    consulted.
    """
    failures: list[str] = []
    checks: list[tuple[str, float, float]] = [
        ("identity_score", report.identity_score, t.accept_identity),
        ("anatomy_score", report.anatomy_score, t.accept_anatomy),
        ("motion_score", report.motion_score, t.accept_motion),
        ("camera_realism_score", report.camera_realism_score, t.accept_camera_realism),
        ("spatial_continuity_score", report.spatial_continuity_score, t.accept_spatial_continuity),
        ("reference_fidelity_score", report.reference_fidelity_score, t.accept_reference_fidelity),
    ]
    if is_extension:
        checks.append(
            (
                "continuity_with_previous_score",
                report.continuity_with_previous_score,
                t.accept_continuity_with_previous,
            )
        )
        checks.append(
            ("audio_continuity_score", report.audio_continuity_score, t.accept_audio_continuity)
        )

    for name, value, threshold in checks:
        if value < threshold:
            failures.append(f"{name} {value:.2f} < {threshold:.2f}")
    return failures


def _is_unstable(
    report: CritiqueReport, t: QualityThresholds, *, is_extension: bool = False
) -> list[str]:
    """Scores sitting just under a threshold — §11.4 sends these to a human.

    The reasoning is that a score inside the margin band is within the noise of a
    vision model's judgement, so spending another call on it is as likely to make
    things worse as better.

    The band is **symmetric** around the threshold. A one-sided band (only just
    below) would be unreachable: the caller checks instability only after the score
    floors pass, and a score below the threshold has already failed those. So a
    band that only looked downward would never fire. Symmetric is also what §11.4
    describes: "identity 分数临界" is about being borderline, not about which side
    of the line the number landed on.
    """
    unstable: list[str] = []
    checks = [
        ("identity_score", report.identity_score, t.accept_identity),
        ("anatomy_score", report.anatomy_score, t.accept_anatomy),
        ("motion_score", report.motion_score, t.accept_motion),
        ("camera_realism_score", report.camera_realism_score, t.accept_camera_realism),
        ("spatial_continuity_score", report.spatial_continuity_score, t.accept_spatial_continuity),
    ]
    if is_extension:
        checks.append(
            (
                "continuity_with_previous_score",
                report.continuity_with_previous_score,
                t.accept_continuity_with_previous,
            )
        )
    for name, value, threshold in checks:
        if abs(value - threshold) <= t.unstable_margin:
            unstable.append(
                f"{name} {value:.2f} is within {t.unstable_margin} of the {threshold:.2f} threshold"
            )
    return unstable


def _needs_regeneration(report: CritiqueReport, t: QualityThresholds) -> list[str]:
    """§11.4: the failures a local edit cannot fix."""
    reasons: list[str] = []
    if report.identity_score < t.regenerate_identity_below:
        reasons.append(
            f"identity {report.identity_score:.2f} is below "
            f"{t.regenerate_identity_below:.2f} — a different person or a drifted face"
        )
    if report.anatomy_score < t.regenerate_anatomy_below:
        reasons.append(
            f"anatomy {report.anatomy_score:.2f} is below "
            f"{t.regenerate_anatomy_below:.2f} — large-scale limb defects"
        )
    if report.spatial_continuity_score < t.regenerate_spatial_below:
        reasons.append(
            f"spatial continuity {report.spatial_continuity_score:.2f} is below "
            f"{t.regenerate_spatial_below:.2f} — the space reset"
        )
    if report.montage_detected:
        reasons.append("the output became a montage")
    return reasons


def _edit_is_safe(report: CritiqueReport, t: QualityThresholds) -> tuple[bool, list[str]]:
    """§11.4: an edit is only offered when everything we are NOT changing is good.

    Editing a shot whose identity is already mediocre risks the model re-rendering
    the face while fixing the hand, turning a marginal clip into an unusable one.
    """
    blockers: list[str] = []
    if report.identity_score < t.min_safe_edit_identity:
        blockers.append(
            f"identity {report.identity_score:.2f} is below the "
            f"{t.min_safe_edit_identity:.2f} floor for a safe edit"
        )
    if report.anatomy_score < t.min_safe_edit_anatomy:
        blockers.append(
            f"anatomy {report.anatomy_score:.2f} is below the "
            f"{t.min_safe_edit_anatomy:.2f} floor for a safe edit"
        )
    if not report.suggested_edit_prompt:
        blockers.append("the Critic did not supply a specific edit instruction")
    if len(report.editable_defects) > 1:
        blockers.append(
            f"the Critic listed {len(report.editable_defects)} editable defects; "
            "§9.1 requires an edit to change exactly one thing"
        )
    return (not blockers), blockers


def decide_segment(
    *,
    report: CritiqueReport,
    segment: SegmentPlan | None,
    budget: Budget,
    segment_index: int,
    attempt_index: int,
    is_extension: bool,
    thresholds: QualityThresholds | None = None,
    has_capability_to_edit: bool = True,
    has_capability_to_extend: bool = True,
    anchor_confirmed: bool = True,
) -> DecisionResult:
    """Decide what to do about one reviewed segment.

    Pure function of its arguments plus the budget state — no I/O, no model calls,
    no randomness. That is what makes it testable and what makes §24.8 satisfiable.
    """
    t = thresholds or load_thresholds()
    result = DecisionResult(
        decision=Decision.HUMAN_REVIEW,
        thresholds_used=t.to_dict(),
    )
    result.critic_verdict_agrees = None

    # The planned anchor is what the *next* segment will continue from. Recording
    # it in the reasons makes a rejected anchor diagnosable without opening the
    # plan file.
    if segment is not None and segment.continuation_anchor:
        result.reasons.append(f"planned anchor: {segment.continuation_anchor!r}")
    if attempt_index > 0:
        result.reasons.append(f"attempt {attempt_index} for this segment")

    # ── 1. Hard rejects (§11.3) ───────────────────────────────────────────
    hard = _hard_reject_reasons(report)
    if hard:
        result.blocking = hard
        if report.critic_degraded:
            # A degraded Critic reporting a hard reject tells us almost nothing.
            result.decision = Decision.HUMAN_REVIEW
            result.reasons = [
                "a hard-reject condition was reported by a review that was itself "
                "degraded, so neither the defect nor the scores can be relied on",
                *hard,
            ]
            _note_critic_agreement(result, report)
            return result

        allowed, why = budget.check(
            CallKind.REGENERATE, segment_index=segment_index, video_seconds=10
        )
        if allowed:
            # §11.4: these are exactly the regenerate cases. A burned-in timestamp
            # or a montage cannot be kept, so a targeted edit is the wrong remedy.
            result.decision = Decision.REGENERATE
            result.reasons = [
                "hard-reject condition present, so the shot must be redone rather than repaired",
                *hard,
            ]
        else:
            result.decision = Decision.HUMAN_REVIEW
            result.reasons = [
                "hard-reject condition present, so no automatic accept is possible",
                f"budget refused regeneration: {why}",
                *hard,
            ]
        _note_critic_agreement(result, report)
        return result

    # ── 2. Degraded Critic ────────────────────────────────────────────────
    if report.critic_degraded:
        result.decision = Decision.HUMAN_REVIEW
        result.reasons.append(
            "the review itself was degraded, so its scores cannot authorise spend"
        )
        result.blocking = list(report.notes[-3:])
        _note_critic_agreement(result, report)
        return result

    # ── 3. Instability (§11.4 "identity 分数临界") ────────────────────────
    # Checked before any remedy, because a borderline score is not evidence enough
    # to spend on. This is deliberately ahead of the regenerate branch: going to a
    # human costs nothing, and a re-render on a marginal score is a coin flip.
    unstable = _is_unstable(report, t, is_extension=is_extension)
    if unstable:
        result.decision = Decision.HUMAN_REVIEW
        result.reasons = [
            "scores sit within the noise band of their thresholds, so another paid "
            "call is as likely to make things worse as better",
            *unstable,
        ]
        _note_critic_agreement(result, report)
        return result

    # ── 4. Regenerate-worthy failures ─────────────────────────────────────
    regen_reasons = _needs_regeneration(report, t)
    if regen_reasons:
        allowed, why = budget.check(
            CallKind.REGENERATE, segment_index=segment_index, video_seconds=10
        )
        if allowed:
            result.decision = Decision.REGENERATE
            result.reasons = regen_reasons
        else:
            result.decision = Decision.HUMAN_REVIEW
            result.reasons = [*regen_reasons, f"budget refused regeneration: {why}"]
        _note_critic_agreement(result, report)
        return result

    # ── 5. Score floors ──────────────────────────────────────────────────
    failures = _score_failures(report, t, is_extension=is_extension)

    if not failures:
        if is_extension:
            if not has_capability_to_extend:
                result.decision = Decision.ACCEPT
                result.reasons = ["segment accepted; no further extension is available"]
            elif not report.anchor_usable or not anchor_confirmed:
                result.decision = Decision.HUMAN_REVIEW
                result.reasons = [
                    "the segment is good but its continuation anchor is not usable, so "
                    "extending from it would break the next segment"
                ]
                result.blocking = [
                    f"anchor_usable={report.anchor_usable}, "
                    f"planned anchor present={anchor_confirmed}"
                ]
            else:
                allowed, why = budget.check(
                    CallKind.EXTEND, segment_index=segment_index + 1, video_seconds=10
                )
                if allowed:
                    result.decision = Decision.EXTEND
                    result.reasons = ["segment accepted; extending"]
                else:
                    result.decision = Decision.ACCEPT
                    result.reasons = [f"segment accepted, but no further extension: {why}"]
        else:
            result.decision = Decision.ACCEPT
            result.reasons = ["all scores clear their thresholds and no defect was reported"]

        _note_critic_agreement(result, report)
        return result

    # ─ 6. Failures present: edit, else regenerate, else human ────────────
    edit_ok, edit_blockers = _edit_is_safe(report, t)
    # Ask about the duration an edit would actually render, not zero: a check with
    # `video_seconds=0` passes on a budget that would refuse the real call.
    edit_allowed, edit_why = budget.check(
        CallKind.EDIT, segment_index=segment_index, video_seconds=EDIT_RENDER_SECONDS
    )

    if edit_ok and edit_allowed and has_capability_to_edit:
        result.decision = Decision.EDIT
        result.suggested_edit = report.suggested_edit_prompt
        result.reasons = [
            "the shot is otherwise sound and the defect is single and local",
            *failures,
        ]
    elif (
        failures
        and budget.check(CallKind.REGENERATE, segment_index=segment_index, video_seconds=10)[0]
    ):
        result.decision = Decision.REGENERATE
        result.reasons = [
            "the failures are not safely fixable by a local edit",
            *failures,
        ]
        result.blocking = edit_blockers
    else:
        result.decision = Decision.HUMAN_REVIEW
        result.reasons = [
            "no automatic remedy is available within budget",
            *failures,
        ]
        result.blocking = [
            *(edit_blockers or ["edits are not available"]),
            "edit unavailable: " + edit_why,
        ]

    _note_critic_agreement(result, report)
    return result


def _note_critic_agreement(result: DecisionResult, report: CritiqueReport) -> None:
    """Record whether the Critic's own verdict matched the derived decision.

    Not a correction — a record. Repeated disagreement is evidence about the
    Critic that a reader can act on.
    """
    mapping = {
        Decision.ACCEPT: "accept",
        Decision.EDIT: "edit",
        Decision.REGENERATE: "regenerate",
        Decision.HUMAN_REVIEW: "human_review",
        Decision.EXTEND: "accept",
    }
    expected = mapping.get(result.decision)
    result.critic_verdict_agrees = expected == report.verdict
    if not result.critic_verdict_agrees:
        result.reasons.append(
            f"note: the Critic's own verdict was {report.verdict!r} while the policy "
            f"derived {result.decision}"
        )


def decide_final(
    *,
    reports: list[CritiqueReport],
    budget: Budget,
    thresholds: QualityThresholds | None = None,
    require_human_gate: bool = True,
) -> DecisionResult:
    """Decide whether a completed chain may be exported.

    §11.4 and §12.3: a high-resolution or final pass is a human gate by default.
    The policy can refuse (requesting review) but never silently approves a
    higher-resolution regeneration, because a re-render is *not* the same content
    (§24.12).
    """
    t = thresholds or load_thresholds()
    result = DecisionResult(decision=Decision.HUMAN_REVIEW, thresholds_used=t.to_dict())

    if not reports:
        result.reasons = ["no segment reviews exist, so nothing has been verified"]
        return result

    if budget.remaining_calls() <= 0:
        result.reasons.append(
            "the call ceiling is exhausted, so no repair pass is possible; this is a "
            "review-and-decide situation only"
        )
    if budget.max_estimated_cost_usd is not None:
        result.reasons.append(
            f"estimated spend {budget.estimated_cost_usd} of "
            f"{budget.max_estimated_cost_usd} USD allowed"
        )

    degraded = [i for i, r in enumerate(reports) if r.critic_degraded]
    hard = [i for i, r in enumerate(reports) if _hard_reject_reasons(r)]

    if hard:
        result.decision = Decision.HUMAN_REVIEW
        result.blocking = [f"segment {i}: {_hard_reject_reasons(reports[i])}" for i in hard]
        result.reasons = ["at least one segment has a hard-reject defect"]
        return result

    if degraded:
        result.decision = Decision.HUMAN_REVIEW
        result.reasons = [f"segments {degraded} were reviewed on thin evidence"]
        result.blocking = [f"segment {i} review degraded" for i in degraded]
        return result

    anchor_failures = [i for i, r in enumerate(reports) if not r.anchor_usable]
    if anchor_failures:
        result.decision = Decision.HUMAN_REVIEW
        result.reasons = [
            f"segments {anchor_failures} do not end in a continuable state; the chain "
            "may not actually be continuous"
        ]
        return result

    if require_human_gate:
        result.decision = Decision.HUMAN_REVIEW
        result.gate_only = True
        result.reasons = [
            "every segment passed, but the final approval gate is enabled "
            "(§11.4: high-resolution and final output require a human)"
        ]
        return result

    result.decision = Decision.ACCEPT
    result.reasons = ["all segments passed review"]
    return result
