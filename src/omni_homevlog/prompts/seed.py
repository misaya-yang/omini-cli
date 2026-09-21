"""Initial-segment prompt template (§9.2).

Structure, in the order §9.1 requires — continuity first, then action, then
camera:

    1. reference header + "use these only as references" instruction
    2. the standing continuity statement (same person, stable identity)
    3. {SEGMENT_ACTION}      ← the only part that changes per segment
    4. camera behaviour
    5. the ending anchor, so the next segment has something to continue from
    6. the prohibition clause, inline (there is no separate negative prompt)
    7. the closing reminder that the images are references, not panels
"""

from __future__ import annotations

from omni_homevlog.schemas import ContinuityBible, ReferenceAsset, SegmentPlan

REFERENCE_HEADER = "{REFERENCE_TOKENS}"

REFERENCE_INSTRUCTION = (
    "Use the supplied images only as identity, hairstyle, outfit, and "
    "home-environment references. Do not reproduce any borders, layout, text, "
    "timestamps, interface elements, or collage structure from the reference media."
)

CONTINUITY_STATEMENT = (
    "Create one continuous {DURATION}-second {ORIENTATION} {ASPECT} realistic "
    "handheld smartphone shot from the boyfriend's point of view. Keep the same "
    "clearly adult woman throughout, with stable facial identity, hairstyle, "
    "accessories, outfit, body proportions, and natural skin texture."
)

CAMERA_BEHAVIOUR = (
    "The camera behaves like a real phone held at close conversational distance: "
    "subtle hand movement, natural autofocus breathing, small exposure shifts, "
    "realistic motion blur, ordinary warm home lighting, and spontaneous behavior "
    "rather than posed commercial acting."
)

CLOSING = (
    "Use the given images as references for video generation. They must not be used "
    "as literal storyboard panels."
)

#: The prohibition clause. §9.1: "禁止项直接写进主 prompt" — the forbidden terms are
#: written into the main prompt rather than relying on a separate negative-prompt
#: parameter, which Omni does not expose.
PROHIBITION_TEMPLATE = "The generated video contains no {FORBIDDEN}. "

#: Fixed identity phrasing.
#:
#: §9.1: "角色描述固定，不在每段随意换词" — the character must be described with the
#: same words in every segment. Rewording "the woman" as "the young lady" between
#: segments is a real cause of identity drift, so the template hardcodes one
#: phrasing and the continuity statement is reused verbatim across all segments.
IDENTITY_PHRASE = "the same clearly adult woman"


def render_seed_prompt(
    *,
    segment: SegmentPlan,
    bible: ContinuityBible,
    references: list[ReferenceAsset] | None = None,
    reference_tokens: str = "",
    duration_s: int = 10,
    aspect_ratio: str = "9:16",
) -> str:
    """Compile the initial-segment prompt."""
    blocks: list[str] = []

    if reference_tokens:
        blocks.append(reference_tokens)
        blocks.append(REFERENCE_INSTRUCTION)

    blocks.append(
        # `{ORIENTATION}` is derived, not hardcoded. The template used to say
        # "vertical 9:16" literally and substitute only the ratio, so a landscape
        # job asked for a "vertical 16:9" shot — a contradiction the model has to
        # resolve on its own.
        CONTINUITY_STATEMENT.replace("{DURATION}", str(duration_s))
        .replace("{ORIENTATION}", "vertical" if aspect_ratio == "9:16" else "horizontal")
        .replace("{ASPECT}", aspect_ratio)
    )

    # The action block is the only segment-specific content.
    blocks.append(_action_block(segment, bible))

    blocks.append(CAMERA_BEHAVIOUR)

    blocks.append(
        f"End with {segment.continuation_anchor.rstrip('.')}, holding that state "
        "naturally for the final moment so the scene can continue seamlessly."
    )

    blocks.append(PROHIBITION_TEMPLATE.format(FORBIDDEN=bible.forbidden_phrase()))

    blocks.append(CLOSING)

    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


def _action_block(segment: SegmentPlan, bible: ContinuityBible) -> str:
    """Start state + action + explicit environment grounding.

    Written as prose, never as a numbered list — a list is the shape that invites
    shot-by-shot interpretation (§9.5).
    """
    pieces: list[str] = []
    if segment.start_state:
        pieces.append(segment.start_state.rstrip(".") + ".")
    if segment.action:
        pieces.append(segment.action.rstrip(".") + ".")
    if segment.environment:
        pieces.append(f"The setting is {segment.environment.rstrip('.')}.")
    if segment.emotional_beat:
        pieces.append(f"The mood stays {segment.emotional_beat.rstrip('.')}.")
    return " ".join(pieces)


def render_seed_prompt_for_plan(
    *,
    plan_segment: SegmentPlan,
    bible: ContinuityBible,
    references: list[ReferenceAsset],
    duration_s: int = 10,
    aspect_ratio: str = "9:16",
) -> str:
    """Convenience wrapper that derives the reference header from the assets."""
    from omni_homevlog.providers.request_builder import reference_tokens

    return render_seed_prompt(
        segment=plan_segment,
        bible=bible,
        references=references,
        reference_tokens=reference_tokens(references),
        duration_s=duration_s,
        aspect_ratio=aspect_ratio,
    )
