"""Job state machine (§5).

Plain Python, on purpose. §24.1 forbids reaching for LangGraph here: the whole
point of this component is that it is trivially auditable and cannot be talked
into an illegal transition by a model.

Two things this module is strict about:

1. **Illegal transitions raise.** There is no "warn and continue" path.
2. **Exception states are reachable from almost anywhere.** A run can fail at
   any point, so `NEEDS_HUMAN` / `BUDGET_EXHAUSTED` / `PROVIDER_UNAVAILABLE` /
   `FAILED_*` are legal targets from every non-terminal state. The illegal
   transitions are the *forward* ones — skipping a review, or extending from a
   segment nobody accepted.
"""

from __future__ import annotations

from collections.abc import Iterable

from omni_homevlog.errors import StateTransitionError
from omni_homevlog.schemas import (
    TERMINAL_STATES,
    JobState,
    extension_accepted_state,
    extension_rendering_state,
    extension_review_state,
)

#: The `extension_*_state` helpers are re-exported deliberately: they are the
#: public way to name an indexed extension state, and mypy requires the
#: re-export to be explicit.
__all__ = [
    "UNIVERSAL_TARGETS",
    "assert_transition",
    "build_transitions",
    "describe_path",
    "extension_accepted_state",
    "extension_rendering_state",
    "extension_review_state",
    "is_human_gate",
    "is_terminal",
    "legal_transitions",
    "reachable_states",
]

#: States any non-terminal state may jump to. Failure does not schedule itself.
UNIVERSAL_TARGETS: frozenset[JobState] = frozenset(
    {
        JobState.NEEDS_HUMAN,
        JobState.BUDGET_EXHAUSTED,
        JobState.PROVIDER_UNAVAILABLE,
        JobState.POLICY_BLOCKED,
        JobState.FAILED_RETRYABLE,
        JobState.FAILED_FINAL,
    }
)

#: Forward edges that do not depend on how many extensions the job needs.
_BASE_FORWARD: dict[JobState, set[JobState]] = {
    JobState.CREATED: {JobState.REFERENCES_VALIDATED},
    JobState.REFERENCES_VALIDATED: {JobState.PLAN_READY},
    JobState.PLAN_READY: {JobState.SEED_RENDERING},
    JobState.SEED_RENDERING: {JobState.SEED_REVIEW},
    JobState.SEED_REVIEW: {JobState.SEED_ACCEPTED, JobState.SEED_RENDERING},
    JobState.SEED_ACCEPTED: {JobState.FINAL_REVIEW},
    JobState.FINAL_REVIEW: {JobState.COMPLETE, JobState.FAILED_FINAL},
    JobState.COMPLETE: set(),
    JobState.FAILED_FINAL: set(),
    JobState.POLICY_BLOCKED: set(),
}

#: Recovery edges: an exception state may re-enter work, but only deliberately.
#: `omni-vlog resume` / `retry` walk these; nothing walks them automatically.
_RECOVERY: dict[JobState, set[JobState]] = {
    JobState.NEEDS_HUMAN: {
        JobState.SEED_RENDERING,
        JobState.SEED_REVIEW,
        JobState.FINAL_REVIEW,
    },
    JobState.BUDGET_EXHAUSTED: {JobState.NEEDS_HUMAN, JobState.FAILED_FINAL},
    JobState.PROVIDER_UNAVAILABLE: {JobState.NEEDS_HUMAN, JobState.FAILED_FINAL},
    JobState.FAILED_RETRYABLE: {
        JobState.SEED_RENDERING,
        JobState.SEED_REVIEW,
        JobState.FINAL_REVIEW,
    },
}


def build_transitions(extension_count: int) -> dict[JobState, set[JobState]]:
    """Full transition table for a job needing `extension_count` extensions.

    `extension_count=2` reproduces the plan's 30-second diagram (§5) exactly.
    """
    if extension_count < 0:
        raise ValueError("extension_count must be >= 0")

    table: dict[JobState, set[JobState]] = {
        state: set(targets) for state, targets in _BASE_FORWARD.items()
    }
    for state, targets in _RECOVERY.items():
        table.setdefault(state, set()).update(targets)
    for state in UNIVERSAL_TARGETS:
        table.setdefault(state, set())

    if extension_count == 0:
        table[JobState.SEED_ACCEPTED].add(JobState.FINAL_REVIEW)
    else:
        # Seed feeds the first extension rather than the final review.
        table[JobState.SEED_ACCEPTED].discard(JobState.FINAL_REVIEW)
        for i in range(1, extension_count + 1):
            rendering = extension_rendering_state(i)
            review = extension_review_state(i)
            accepted = extension_accepted_state(i)

            previous_accepted = (
                JobState.SEED_ACCEPTED if i == 1 else extension_accepted_state(i - 1)
            )
            table.setdefault(previous_accepted, set()).add(rendering)

            table.setdefault(rendering, set()).update({review})
            # A failed review may re-render the same segment (bounded by budget).
            table.setdefault(review, set()).update({accepted, rendering})

            # The accepted state always exists; where it leads depends on whether
            # another extension follows it.
            accepted_targets = table.setdefault(accepted, set())
            if i == extension_count:
                accepted_targets.add(JobState.FINAL_REVIEW)

            # Recovery into an extension stage.
            for exc in (JobState.NEEDS_HUMAN, JobState.FAILED_RETRYABLE):
                table.setdefault(exc, set()).update({rendering, review})

    # Universal targets apply everywhere that is not already terminal.
    for state in list(table):
        if state in TERMINAL_STATES:
            continue
        table[state].update(UNIVERSAL_TARGETS)

    return table


def legal_transitions(state: JobState, extension_count: int) -> set[JobState]:
    return build_transitions(extension_count).get(state, set())


def assert_transition(
    current: JobState,
    target: JobState,
    *,
    extension_count: int,
    allow_noop: bool = True,
) -> None:
    """Raise `StateTransitionError` unless `current -> target` is legal."""
    if current == target and allow_noop:
        return
    if current in TERMINAL_STATES:
        raise StateTransitionError(
            f"{current} is terminal; cannot move to {target}",
            detail={"from": str(current), "to": str(target)},
        )
    allowed = legal_transitions(current, extension_count)
    if target not in allowed:
        raise StateTransitionError(
            f"Illegal transition {current} -> {target}. Allowed: "
            f"{sorted(str(s) for s in allowed) or 'none'}",
            detail={
                "from": str(current),
                "to": str(target),
                "allowed": sorted(str(s) for s in allowed),
            },
        )


def is_human_gate(state: JobState) -> bool:
    from omni_homevlog.schemas import HUMAN_GATE_STATES

    return state in HUMAN_GATE_STATES


def is_terminal(state: JobState) -> bool:
    return state in TERMINAL_STATES


def describe_path(extension_count: int) -> list[str]:
    """Human-readable happy path, used by `omni-vlog doctor --describe`."""
    path: list[JobState] = [
        JobState.CREATED,
        JobState.REFERENCES_VALIDATED,
        JobState.PLAN_READY,
        JobState.SEED_RENDERING,
        JobState.SEED_REVIEW,
        JobState.SEED_ACCEPTED,
    ]
    for i in range(1, extension_count + 1):
        path += [
            extension_rendering_state(i),
            extension_review_state(i),
            extension_accepted_state(i),
        ]
    path += [JobState.FINAL_REVIEW, JobState.COMPLETE]
    return [str(s) for s in path]


def reachable_states(extension_count: int) -> Iterable[JobState]:
    return build_transitions(extension_count).keys()
