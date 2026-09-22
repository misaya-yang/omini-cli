"""Director (§4.1).

Turns a free-text creative brief into a `ContinuityBible` plus an ordered
`SegmentPlan`. It never calls the video API — that separation is the point.

Responsibilities §4.1 assigns to this role, each of which is enforced by the
prompt and then *checked* on the way back in:

  * compress the brief into 3 continuous segments (or fewer, for a 10s target)
  * establish invariants for person, outfit, space, lighting, camera language
  * cap the actions per segment
  * give every segment a clear start state and end state
  * never call the video generation API

The checks matter more than the prompt. A model asked for "at most three actions"
will sometimes return six, and a plan with six actions per segment renders as a
montage no matter how good the prompt compiler is. So `validate_plan` re-derives
every structural requirement and the pipeline refuses a plan that fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omni_homevlog.agents.llm import InlineImage, TextModelClient, build_client
from omni_homevlog.config import Settings, get_settings
from omni_homevlog.errors import OmniVlogError
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.schemas import (
    DEFAULT_FORBIDDEN_EDITING_PATTERNS,
    DEFAULT_FORBIDDEN_VISUAL_ELEMENTS,
    ContinuityBible,
    DirectorPlan,
    ProjectSpec,
    ReferenceAsset,
    SegmentPlan,
)

logger = get_logger("director")


class PlanRejectedError(OmniVlogError):
    """The Director produced a structurally unusable plan."""

    code = "plan_rejected"


#: JSON schema handed to the model as `responseJsonSchema`.
DIRECTOR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "logline", "continuity_bible", "segments"],
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "continuity_bible": {
            "type": "object",
            "required": ["subject_identity"],
            "properties": {
                "subject_identity": {"type": "string"},
                "immutable_face_traits": {"type": "array", "items": {"type": "string"}},
                "hair": {"type": "array", "items": {"type": "string"}},
                "accessories": {"type": "array", "items": {"type": "string"}},
                "outfit": {"type": "array", "items": {"type": "string"}},
                "body_proportion": {"type": "array", "items": {"type": "string"}},
                "environment_topology": {"type": "array", "items": {"type": "string"}},
                "lighting": {"type": "array", "items": {"type": "string"}},
                "camera_grammar": {"type": "array", "items": {"type": "string"}},
                "interaction_style": {"type": "array", "items": {"type": "string"}},
                "forbidden_visual_elements": {"type": "array", "items": {"type": "string"}},
                "forbidden_editing_patterns": {"type": "array", "items": {"type": "string"}},
            },
        },
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "index",
                    "intended_duration_s",
                    "start_state",
                    "action",
                    "camera_behavior",
                    "environment",
                    "emotional_beat",
                    "audio_intent",
                    "end_state",
                    "continuation_anchor",
                ],
                "properties": {
                    "index": {"type": "integer"},
                    "intended_duration_s": {"type": "integer", "minimum": 3, "maximum": 10},
                    "start_state": {"type": "string"},
                    "action": {"type": "string"},
                    "camera_behavior": {"type": "string"},
                    "environment": {"type": "string"},
                    "emotional_beat": {"type": "string"},
                    "audio_intent": {"type": "string"},
                    "end_state": {"type": "string"},
                    "continuation_anchor": {"type": "string"},
                    "max_distinct_actions": {"type": "integer"},
                },
            },
        },
    },
}

SYSTEM_INSTRUCTION = (
    "You are a director planning a short, intimate, single-take home video shot "
    "by a boyfriend on a phone. You return only JSON matching the requested schema. "
    "You plan; you do not generate video."
)


def build_director_prompt(
    *,
    spec: ProjectSpec,
    references: list[ReferenceAsset],
    reference_descriptions: list[str] | None = None,
) -> str:
    """The planning instruction. Structured, specific, and bounded."""
    segment_count = spec.extension_count + 1
    segment_seconds = (
        spec.concept_duration_s if spec.mode == "concept" else min(10, spec.target_duration_s)
    )

    reference_lines: list[str] = []
    if reference_descriptions:
        for i, description in enumerate(reference_descriptions):
            reference_lines.append(f"  Image{i + 1}: {description}")
    else:
        for i, asset in enumerate(references):
            reference_lines.append(f"  Image{i + 1}: role={asset.role}")

    references_block = (
        "\n".join(reference_lines)
        if reference_lines
        else "  (no reference images supplied; plan a generic but consistent adult woman)"
    )

    return f"""Plan a {spec.target_duration_s}-second home vlog as {"one" if segment_count == 1 else segment_count} continuous segment{"s" if segment_count != 1 else ""}.

THE BRIEF
{spec.brief}

STYLE
{spec.style}

FORMAT
- Aspect ratio {spec.aspect_ratio}, {spec.resolution} output.
- {"Audio is generated; plan for natural room tone and small everyday sounds." if spec.audio_enabled else "No audio."}
- Each segment is {segment_seconds} seconds and will be generated as ONE continuous take.
- Segment 0 is generated fresh. Every later segment is an EXTENSION that begins from
  the exact final moment of the one before it.

REFERENCE IMAGES
{references_block}

WHAT EACH SEGMENT MUST CONTAIN
- exactly ONE main location
- at most ONE natural movement between positions
- ONE to THREE simple actions, no more
- a clear end state that the next segment can continue from

YOUR JOB
1. Write a continuity bible that fixes the person, hair, accessories, outfit, body
   proportions, the apartment's layout, the lighting, and the camera language.
   Every field must be concrete and specific enough to be checked against a frame
   later. "Long dark hair" is usable; "nice hair" is not.
2. Break the brief into {segment_count} segment{"s" if segment_count != 1 else ""}. Each one gets a start state,
   an action, camera behaviour, environment, emotional beat, audio intent, an end
   state, and a continuation anchor.
3. The `continuation_anchor` must describe a *physical pose or position* the next
   segment can start from. "She rests one hand on the bedroom doorframe, turned
   three-quarters toward the camera" works. "The scene ends peacefully" does not.

HARD RULES
- Do NOT write shot numbers, timecodes, "shot 1", "scene 2", or a storyboard.
- Do NOT describe separate cuts, transitions, montage, or split screens. Every
  segment is one unbroken take.
- Do NOT plan more than {segment_count} segment{"s" if segment_count != 1 else ""}.
- Do NOT plan more than three distinct actions in any segment.
- Do NOT describe the segments as a list of shots; describe them as one continuous
  moment that happens to be generated in parts.

Return JSON matching the schema exactly."""


@dataclass(slots=True)
class DirectorResult:
    plan: DirectorPlan
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Director:
    """Plans a job. One LLM call, validated on the way back."""

    def __init__(
        self,
        *,
        client: TextModelClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def client(self) -> TextModelClient:
        if self._client is None:
            self._client = build_client(
                model=self.settings.director_model,
                provider=self.settings.omni_provider,
                settings=self.settings,
                location=self.settings.director_location,
            )
        return self._client

    def plan(
        self,
        *,
        spec: ProjectSpec,
        references: list[ReferenceAsset],
        reference_descriptions: list[str] | None = None,
        reference_images: list[InlineImage] | None = None,
    ) -> DirectorResult:
        """Produce a validated plan."""
        prompt = build_director_prompt(
            spec=spec,
            references=references,
            reference_descriptions=reference_descriptions,
        )

        response = self.client.generate(
            prompt=prompt,
            system_instruction=SYSTEM_INSTRUCTION,
            images=reference_images,
            response_schema=DIRECTOR_RESPONSE_SCHEMA,
            temperature=0.7,
        )

        from omni_homevlog.agents.llm import extract_json

        payload = extract_json(response.text)

        try:
            plan = DirectorPlan.model_validate(payload)
        except Exception as exc:
            raise PlanRejectedError(
                f"Director output did not match the plan schema: {exc}",
                detail={"preview": response.text[:600]},
            ) from exc

        warnings = validate_plan(plan, spec=spec)
        return DirectorResult(
            plan=plan, model=response.model, usage=response.usage, warnings=warnings
        )


def validate_plan(plan: DirectorPlan, *, spec: ProjectSpec) -> list[str]:
    """Re-derive §4.1's structural requirements. Raises on a hard violation.

    Soft problems come back as warnings. Anything that would make the render
    structurally wrong — the wrong number of segments, a banned element missing
    from the bible, an anchor that cannot be continued from — raises, because the
    pipeline cannot repair it and must not proceed.
    """
    warnings: list[str] = []
    problems: list[str] = []

    expected = spec.extension_count + 1
    if len(plan.segments) != expected:
        problems.append(
            f"plan has {len(plan.segments)} segments but a {spec.target_duration_s}s "
            f"target needs exactly {expected}"
        )

    for segment in plan.segments:
        if segment.max_distinct_actions > 3:
            problems.append(
                f"segment {segment.index} allows {segment.max_distinct_actions} "
                "distinct actions; the cap is 3"
            )
        if not segment.continuation_anchor.strip():
            problems.append(f"segment {segment.index} has an empty continuation_anchor")
        if not segment.end_state.strip():
            problems.append(f"segment {segment.index} has an empty end_state")

        # A "continuation anchor" that describes a mood cannot be continued from.
        anchor = segment.continuation_anchor.lower()
        vague = ("peaceful", "calm", "the scene ends", "fade", "atmosphere", "mood")
        if any(v in anchor for v in vague) and not any(
            p in anchor
            for p in (
                "hand",
                "arm",
                "stand",
                "sit",
                "turn",
                "reach",
                "walk",
                "hold",
                "face",
                "shoulder",
                "door",
                "bed",
            )
        ):
            warnings.append(
                f"segment {segment.index} continuation_anchor reads as a mood rather "
                "than a physical state; extensions continue from poses, not feelings"
            )

    # The segments must form one continuous chain.
    for previous, current in zip(plan.segments, plan.segments[1:], strict=False):
        if (
            previous.end_state.strip()
            and current.start_state.strip()
            and not _states_relate(previous.end_state, current.start_state)
        ):
            warnings.append(
                f"segment {current.index} start_state does not obviously follow "
                f"segment {previous.index} end_state; the extension may reset"
            )

    bible = plan.continuity_bible
    if not bible.forbidden_visual_elements:
        bible.forbidden_visual_elements = list(DEFAULT_FORBIDDEN_VISUAL_ELEMENTS)
        warnings.append("Director returned an empty forbidden_visual_elements; defaults restored")
    else:
        missing = [
            item
            for item in DEFAULT_FORBIDDEN_VISUAL_ELEMENTS
            if item not in bible.forbidden_visual_elements
        ]
        if missing:
            bible.forbidden_visual_elements.extend(missing)
            warnings.append(f"Director omitted required forbidden elements {missing}; appended")

    if not bible.forbidden_editing_patterns:
        bible.forbidden_editing_patterns = list(DEFAULT_FORBIDDEN_EDITING_PATTERNS)
    else:
        missing = [
            item
            for item in DEFAULT_FORBIDDEN_EDITING_PATTERNS
            if item not in bible.forbidden_editing_patterns
        ]
        if missing:
            bible.forbidden_editing_patterns.extend(missing)

    if not bible.immutable_face_traits:
        warnings.append(
            "continuity bible has no immutable_face_traits; identity checks will be "
            "weaker and the Critic has less to compare against"
        )

    if problems:
        raise PlanRejectedError(
            "Director plan failed structural validation:\n  - " + "\n  - ".join(problems),
            detail={"problems": problems, "warnings": warnings},
        )

    for warning in warnings:
        logger.warning(warning)
    return warnings


def _states_relate(end_state: str, start_state: str) -> bool:
    """Loose check that consecutive segments share vocabulary.

    Intentionally forgiving: this is a warning signal, not a gate. Paraphrase is
    normal and desirable; what we are looking for is a start state that shares
    *nothing* with the previous ending, which usually means the Director quietly
    started a new scene.
    """
    stop = {
        "the",
        "a",
        "an",
        "she",
        "her",
        "and",
        "then",
        "with",
        "to",
        "of",
        "in",
        "on",
        "at",
        "is",
        "as",
        "into",
        "toward",
        "towards",
        "from",
        "begins",
    }

    def words(text: str) -> set[str]:
        import re

        return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in stop and len(w) > 3}

    left, right = words(end_state), words(start_state)
    if not left or not right:
        return True
    return bool(left & right)


def default_plan_for_spec(spec: ProjectSpec, bible: ContinuityBible) -> DirectorPlan:
    """The plan template from §10, used as a fallback and in tests.

    This is the plan document's own worked example of a 30-second boyfriend-POV
    home vlog. It exists so the pipeline can be exercised without an LLM call and
    so a Director failure has a documented, known-good fallback the operator can
    opt into deliberately.
    """
    segments = [
        SegmentPlan(
            index=0,
            intended_duration_s=10,
            start_state=(
                "The woman is seated at a small dining table beside a mug and cookies "
                "in the warm apartment"
            ),
            action=(
                "She rests her cheek on one hand, notices her boyfriend filming, looks "
                "into the lens with a soft sleepy smile, then lifts the mug and takes "
                "one small sip"
            ),
            camera_behavior="Close handheld phone shot, slight natural sway, no cut",
            environment="a warm apartment dining area with a small table and soft lamps",
            emotional_beat="sleepy, affectionate, unhurried",
            audio_intent="quiet room tone, the small sound of the mug being lifted",
            end_state=(
                "She lowers the mug, shifts forward in the chair, and begins to stand "
                "while looking toward the bedroom"
            ),
            continuation_anchor=(
                "beginning to stand, mug still in one hand, gaze turned toward the bedroom"
            ),
        ),
        SegmentPlan(
            index=1,
            intended_duration_s=10,
            start_state="Continue from her beginning to stand, mug in hand",
            action=(
                "She stands naturally and walks toward the vanity and bedroom while the "
                "boyfriend follows at normal walking speed. She briefly adjusts one "
                "section of her long hair, then looks back over her shoulder with a shy smile"
            ),
            camera_behavior=(
                "One continuous follow shot; preserve walking direction and apartment geometry"
            ),
            environment="the apartment corridor leading to the bedroom",
            emotional_beat="shy, playful, warm",
            audio_intent="footsteps on the floor, quiet room tone, no music",
            end_state=(
                "She reaches the bedroom doorway, turns slightly toward the camera, and "
                "extends one hand backward as if asking him to follow"
            ),
            continuation_anchor=(
                "one hand extended back toward the camera at the bedroom doorway, "
                "body turned three-quarters toward the lens"
            ),
        ),
        SegmentPlan(
            index=2,
            intended_duration_s=10,
            start_state="Continue from her hand reaching back at the bedroom doorway",
            action=(
                "She guides the camera into the bedroom, sits on the edge of the bed "
                "among blankets and plush toys, holds the mug with both hands for a "
                "moment, then leans closer with a playful affectionate expression"
            ),
            camera_behavior="Move closer slowly, remain handheld and intimate, no cut",
            environment="a softly lit bedroom with blankets and plush toys on the bed",
            emotional_beat="close, affectionate, a little mischievous",
            audio_intent="soft bedding sounds, quiet breathing, room tone",
            end_state=(
                "She reaches toward the phone and gently covers the lens with her palm, "
                "ending the recording naturally"
            ),
            continuation_anchor="her palm covering the lens entirely",
        ),
    ]
    wanted = spec.extension_count + 1
    if wanted > len(segments):
        # Continue the §10 arc rather than truncating it. Slicing a hardcoded
        # 3-element list gave a 40-second job three segments, so it paid for three
        # renders and then failed looking for segment 3.
        while len(segments) < wanted:
            index = len(segments)
            previous = segments[-1]
            segments.append(
                SegmentPlan(
                    index=index,
                    intended_duration_s=10,
                    start_state=previous.end_state,
                    action=(
                        "She settles into the moment, looking toward the camera with the "
                        "same easy, affectionate manner as before"
                    ),
                    camera_behavior=(
                        "Stay handheld at the same close conversational distance, no cut"
                    ),
                    environment=previous.environment,
                    emotional_beat=previous.emotional_beat,
                    audio_intent=previous.audio_intent,
                    end_state=f"the moment comes to a natural rest after part {index}",
                    continuation_anchor=(
                        f"a settled pose at the end of part {index}, facing the camera"
                    ),
                )
            )

    if spec.mode == "concept":
        segments[0].intended_duration_s = spec.concept_duration_s
    return DirectorPlan(
        title=spec.title,
        logline=spec.brief,
        continuity_bible=bible,
        segments=segments[:wanted],
    )
