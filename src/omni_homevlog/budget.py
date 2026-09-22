"""Budget guard (§12).

The plan's §24.8 is blunt: a Critic's free-text suggestion must never become a
paid call on its own. So every provider call in this codebase passes through
`Budget.authorize()` first, and that method is pure arithmetic over counters —
no model is consulted, no heuristics, no "but it looks close".

Counting rules:
  * one *call* = one `interactions.create` request, regardless of outcome
  * `max_total_calls` is a hard ceiling across the whole job
  * per-segment retries are capped separately so one bad segment cannot eat the
    entire job budget
  * a 429 or a 5xx is still a call; §12.2 requires we do not re-issue a request
    whose billing status is unclear
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, PrivateAttr

from omni_homevlog.errors import BudgetExhaustedError
from omni_homevlog.schemas import BudgetEvent


class CallKind(StrEnum):
    SEED = "seed"
    EXTEND = "extend"
    EDIT = "edit"
    REGENERATE = "regenerate"
    PROBE = "probe"


class Budget(BaseModel):
    """§12.2. Mutable counters plus the ceilings they are checked against."""

    model_config = ConfigDict(extra="forbid")

    max_total_calls: int = 8
    max_video_seconds_requested: int = 60
    max_seed_attempts: int = 2
    max_edit_attempts_per_segment: int = 1
    max_regenerations_per_segment: int = 1
    max_estimated_cost_usd: Decimal | None = None

    calls_made: int = 0
    video_seconds_requested: int = 0
    estimated_cost_usd: Decimal = Decimal("0")

    #: "kind:segment_index" -> count. Private so it stays out of the JSON dump.
    _by_kind_segment: Counter[str] = PrivateAttr(default_factory=Counter)

    #  introspection ───────────────────────────────────────────────────────

    def count(self, kind: CallKind, segment_index: int) -> int:
        return self._by_kind_segment[f"{kind}:{segment_index}"]

    def remaining_calls(self) -> int:
        return max(0, self.max_total_calls - self.calls_made)

    def remaining_cost_usd(self) -> Decimal | None:
        if self.max_estimated_cost_usd is None:
            return None
        return max(Decimal("0"), self.max_estimated_cost_usd - self.estimated_cost_usd)

    # ─ the gate ────────────────────────────────────────────────────────────

    def check(
        self,
        kind: CallKind,
        *,
        segment_index: int,
        video_seconds: int = 0,
        estimated_cost_usd: Decimal = Decimal("0"),
    ) -> tuple[bool, str]:
        """Non-raising form of `authorize`, for planning and for `status`."""
        if self.calls_made + 1 > self.max_total_calls:
            return False, (f"job call ceiling reached ({self.calls_made}/{self.max_total_calls})")
        if self.video_seconds_requested + video_seconds > self.max_video_seconds_requested:
            return False, (
                f"video-seconds ceiling reached "
                f"({self.video_seconds_requested}/{self.max_video_seconds_requested})"
            )

        if kind is CallKind.SEED and self.count(kind, segment_index) >= self.max_seed_attempts:
            return False, (
                f"seed attempt ceiling reached for this job "
                f"({self.count(kind, segment_index)}/{self.max_seed_attempts})"
            )
        if (
            kind is CallKind.EDIT
            and self.count(kind, segment_index) >= self.max_edit_attempts_per_segment
        ):
            return False, (
                f"edit ceiling reached for segment {segment_index} "
                f"({self.count(kind, segment_index)}/{self.max_edit_attempts_per_segment})"
            )
        if (
            kind is CallKind.REGENERATE
            and self.count(kind, segment_index) >= self.max_regenerations_per_segment
        ):
            return False, (
                f"regenerate ceiling reached for segment {segment_index} "
                f"({self.count(kind, segment_index)}/{self.max_regenerations_per_segment})"
            )
        if kind is CallKind.EXTEND and self.count(kind, segment_index) >= 2:
            # One planned extension plus at most one repair.
            return False, f"extend ceiling reached for segment {segment_index}"

        limit = self.max_estimated_cost_usd
        if limit is not None and self.estimated_cost_usd + estimated_cost_usd > limit:
            return False, (
                f"cost ceiling would be exceeded "
                f"({self.estimated_cost_usd} + {estimated_cost_usd} > {limit} USD)"
            )
        return True, "ok"

    def authorize(
        self,
        kind: CallKind,
        *,
        segment_index: int,
        attempt_index: int,
        video_seconds: int = 0,
        estimated_cost_usd: Decimal = Decimal("0"),
        events: list[BudgetEvent] | None = None,
    ) -> BudgetEvent:
        """Reserve one call. Raises `BudgetExhaustedError` when the ceiling binds.

        The reservation is recorded even when it is granted, so a crash between
        authorize and the HTTP request still leaves an auditable trail.
        """
        ok, reason = self.check(
            kind,
            segment_index=segment_index,
            video_seconds=video_seconds,
            estimated_cost_usd=estimated_cost_usd,
        )
        event = BudgetEvent(
            kind="authorize" if ok else "deny",
            call_kind=str(kind),
            segment_index=segment_index,
            attempt_index=attempt_index,
            video_seconds=video_seconds,
            estimated_cost_usd=estimated_cost_usd,
            reason=None if ok else reason,
        )
        if events is not None:
            events.append(event)
        if not ok:
            raise BudgetExhaustedError(
                f"Budget refused {kind} for segment {segment_index}: {reason}",
                detail={"call_kind": str(kind), "segment_index": segment_index},
            )

        self.calls_made += 1
        self.video_seconds_requested += video_seconds
        self.estimated_cost_usd += estimated_cost_usd
        self._by_kind_segment[f"{kind}:{segment_index}"] += 1
        return event

    def record_cost(self, delta: Decimal, events: list[BudgetEvent] | None = None) -> None:
        """Add realised cost discovered after the fact (usage tokens → dollars)."""
        self.estimated_cost_usd += delta
        if events is not None:
            events.append(
                BudgetEvent(
                    kind="spend",
                    call_kind="actual",
                    segment_index=-1,
                    attempt_index=-1,
                    estimated_cost_usd=delta,
                )
            )

    def note_unknown_outcome(self, events: list[BudgetEvent] | None = None) -> None:
        """A dispatched request we never observed.

        §5.1: this is NOT a licence to retry. We record it so the human sees a
        possibly-billable call, and we keep the call counted against the ceiling.
        """
        if events is not None:
            events.append(
                BudgetEvent(
                    kind="refund_unknown",
                    call_kind="unknown",
                    segment_index=-1,
                    attempt_index=-1,
                    reason=("request dispatched but outcome never observed; may still be billable"),
                )
            )

    def snapshot(self) -> dict[str, int | str | None]:
        return {
            "max_total_calls": self.max_total_calls,
            "max_seed_attempts": self.max_seed_attempts,
            "max_edit_attempts_per_segment": self.max_edit_attempts_per_segment,
            "max_regenerations_per_segment": self.max_regenerations_per_segment,
            "calls_made": self.calls_made,
            "remaining_calls": self.remaining_calls(),
            "video_seconds_requested": self.video_seconds_requested,
            "max_video_seconds_requested": self.max_video_seconds_requested,
            "estimated_cost_usd": str(self.estimated_cost_usd),
            "max_estimated_cost_usd": (
                str(self.max_estimated_cost_usd)
                if self.max_estimated_cost_usd is not None
                else None
            ),
        }


def budget_for_mode(
    *,
    mode: str,
    target_duration_s: int,
    max_total_calls: int = 8,
    max_estimated_cost_usd: Decimal | None = None,
) -> Budget:
    """Concept mode is cheap and shallow; production mode is the full chain (§12.1)."""
    if mode == "concept":
        return Budget(
            max_total_calls=min(max_total_calls, 2),
            max_video_seconds_requested=6,
            max_seed_attempts=1,
            max_edit_attempts_per_segment=0,
            max_regenerations_per_segment=1,
            max_estimated_cost_usd=max_estimated_cost_usd,
        )

    extension_count = max(0, target_duration_s // 10 - 1)
    # seed + one repair + (extend + one repair) per extension + headroom for edits
    worst_case = 2 + extension_count * 2 + 2
    return Budget(
        max_total_calls=min(max_total_calls, worst_case),
        max_video_seconds_requested=max(30, target_duration_s * 3),
        max_seed_attempts=2,
        max_edit_attempts_per_segment=1,
        max_regenerations_per_segment=1,
        max_estimated_cost_usd=max_estimated_cost_usd,
    )
