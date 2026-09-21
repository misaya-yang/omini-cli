"""Extension prompt template (§9.3).

The whole job of this template is to make the model treat the extension as *the
same take continuing*, not as a new shot that happens to start similarly.

§9.1 is direct about the failure mode: "扩展段强调'从上一帧直接继续'" and "对扩展 prompt
只描述下一段动作，不重写整个故事" — describe only the next segment's action; do not
restate the whole story. A prompt that re-describes the apartment, the character,
and the plot gives the model permission to re-establish all of it, which is
exactly the "scene reset" the plan's exit criteria forbid.
"""

from __future__ import annotations

from omni_homevlog.schemas import ContinuityBible, SegmentPlan

#: Continuity preamble. Deliberately exhaustive about *what must not change*,
#: because that list is the actual constraint; the action paragraph is short.
CONTINUITY_PREAMBLE = (
    "Continue directly from the exact final moment of the previous video. Preserve "
    "the same woman, face, hair, accessories, outfit, room layout, lighting, handheld "
    "phone position, motion direction, and audio atmosphere."
)

#: Physical-continuity instruction. This is what prevents the common failure where
#: the new segment starts from a neutral standing pose rather than the previous
#: segment's actual final pose.
FLOW_INSTRUCTION = (
    "The previous final pose must flow physically into the new movement. Do not "
    "reintroduce the character, do not restart the story, and do not change the "
    "apartment geometry."
)

PROHIBITION = (
    "No on-screen text, timestamp, subtitle, caption, logo, watermark, interface, "
    "storyboard layout, split screen, collage, rapid cuts, new person, face drift, "
    "clothing change, or sudden lighting change."
)


def render_extend_prompt(
    *,
    segment: SegmentPlan,
    bible: ContinuityBible,
    duration_s: int = 10,
) -> str:
    """Compile an extension prompt for the segment that follows the current one."""
    action = " ".join(
        part
        for part in (
            segment.action.rstrip(".") + "." if segment.action else "",
            f"The setting remains {segment.environment.rstrip('.')}."
            if segment.environment
            else "",
        )
        if part
    )

    movement = (
        f"Over the next {duration_s} seconds, {action} "
        "Keep this as one continuous natural moment with no montage and no abrupt "
        "scene reset."
    )

    anchor = (
        f"End with {segment.continuation_anchor.rstrip('.')}."
        if segment.continuation_anchor
        else ""
    )

    blocks = [
        CONTINUITY_PREAMBLE,
        movement,
        FLOW_INSTRUCTION,
        anchor,
        PROHIBITION,
    ]
    return "\n\n".join(b.strip() for b in blocks if b and b.strip())


def render_extend_prompt_with_camera(
    *,
    segment: SegmentPlan,
    bible: ContinuityBible,
    duration_s: int = 10,
) -> str:
    """Extension prompt that also carries the planned camera behaviour.

    Used when the Director specified a camera move that differs from the previous
    segment's (for example a follow-shot beginning). Kept separate from
    `render_extend_prompt` so the default stays as short as §9.1 wants.
    """
    base = render_extend_prompt(segment=segment, bible=bible, duration_s=duration_s)
    if not segment.camera_behavior:
        return base
    camera = f"The camera continues handheld and now {segment.camera_behavior.rstrip('.')}."
    return base.replace(FLOW_INSTRUCTION, f"{FLOW_INSTRUCTION}\n\n{camera}")


def continuation_note(previous_anchor: str | None) -> str:
    """Reminder appended when the previous segment's anchor is known.

    Naming the previous anchor explicitly measurably reduces "restart" behaviour,
    because the model has a concrete state to continue from rather than an
    instruction to "continue" in the abstract.
    """
    if not previous_anchor:
        return ""
    return (
        f"The previous video ended with: {previous_anchor.rstrip('.')}. "
        "Begin from exactly that state."
    )
