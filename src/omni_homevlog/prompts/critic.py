"""Critic prompt (§11).

The Critic is a *different model from Omni* — §4.1 is explicit, because Omni has
no structured output and is a poor judge of its own output. This prompt drives a
JSON-capable vision model.

§11.2: the Critic "必须只返回可验证 JSON，不输出散文" — JSON only, no prose. The
prompt is written so that every requested field is something the model can
actually observe in the supplied material, and so the model is told what to do
when it *cannot* observe something. That last part matters more than it looks:
a vision model asked for a `motion_score` will invent one if it was only given
still frames, and an invented score is worse than an admitted gap.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from omni_homevlog.errors import InvalidRequestError
from omni_homevlog.schemas import ContinuityBible, CritiqueReport, SegmentPlan

SYSTEM_ROLE = (
    "You are a strict video quality inspector for a short home-vlog production. "
    "You return only JSON. You never write prose, never explain, and never invent "
    "an observation you cannot make from the material you were given."
)

#: The report schema, described inline. Omni cannot consume this as a structured
#: output schema, but the Critic model is a normal JSON-capable model and this
#: text is what it follows.
REPORT_SCHEMA_DESCRIPTION = """
Return exactly this JSON object and nothing else:

{
  "identity_score": 0.0-1.0,
  "anatomy_score": 0.0-1.0,
  "motion_score": 0.0-1.0,
  "spatial_continuity_score": 0.0-1.0,
  "camera_realism_score": 0.0-1.0,
  "reference_fidelity_score": 0.0-1.0,
  "audio_continuity_score": 0.0-1.0,
  "continuity_with_previous_score": 0.0-1.0,
  "text_overlay_detected": true|false,
  "timestamp_detected": true|false,
  "ui_detected": true|false,
  "montage_detected": true|false,
  "anchor_usable": true|false,
  "severe_defects": ["..."],
  "editable_defects": ["..."],
  "suggested_edit_prompt": "string or null",
  "notes": ["..."],
  "verdict": "accept" | "edit" | "regenerate" | "human_review"
}
""".strip()

SCORING_GUIDANCE = """
Scoring rules:
- Scores are judgements, not measurements. Be consistent, not precise to three decimals.
- identity_score: same person as the reference images — face shape, features, skin tone.
  Penalise heavily any change of apparent person. This is the score that matters most.
- anatomy_score: hands, fingers, limbs, teeth, eyes, and where the body meets objects.
  Multi-fingered hands, fused limbs, or a body intersecting furniture score below 0.4.
- motion_score: does movement read as continuous and physically plausible?
  Rate this ONLY if you were given multiple frames or the video itself. If you were
  given a single still frame, set it to 0.0 and say why in notes.
- spatial_continuity_score: is this the same room as before, with the same geometry,
  furniture positions, and sight lines? A reset or rearranged space scores low.
- camera_realism_score: does it read as a phone held by a person — sway, breathing
  autofocus, natural exposure shifts — rather than a locked-off or synthetic camera?
- reference_fidelity_score: how faithfully do hair, outfit, accessories, and room
  match the supplied reference images?
- continuity_with_previous_score: how well does this segment's first moment follow
  the previous segment's last moment? Rate 1.0 when there is no previous segment.
- audio_continuity_score: is the room tone and ambience consistent with the previous
  segment? Rate 1.0 when there is no previous segment or no audio track.
- anchor_usable: is the final moment a state the next segment can continue from
  naturally? A hard stop, a cut to black, or a settled static pose is not usable.
""".strip()

HARD_REJECT_GUIDANCE = """
Report these as severe_defects — any one of them prevents an "accept" verdict:
- a timestamp or clock burned into the picture
- subtitles, captions, shot numbers, a logo, a watermark, or player interface controls
- the subject becoming a visibly different person
- a person appearing who was not planned
- severe limb or hand defects: extra fingers, fused hands, detached limbs, bodies
  passing through furniture
- the space resetting between the previous segment and this one
- the output being a collage, split screen, slideshow, or multiple panels
- an ending that cannot be continued naturally
- the provider refusing or truncating the content
""".strip()

#: The honesty clause. Without this, a model given one frame still reports a motion
#: score, and the decision policy then acts on a fabricated number.
HONESTY_CLAUSE = """
If you cannot observe something from the supplied material, say so in `notes` and
score it 0.0 rather than guessing. An admitted gap is useful. An invented score is
actively harmful, because downstream code acts on it.
"""


def render_critic_prompt(
    *,
    segment: SegmentPlan | None,
    bible: ContinuityBible | None,
    is_extension: bool,
    has_previous_frames: bool,
    has_video: bool,
    has_audio: bool,
    frame_labels: list[str],
    local_media_summary: str | None = None,
    segment_prompt: str | None = None,
) -> str:
    """Build the Critic instruction for one review.

    The `has_*` flags are passed in rather than inferred so the prompt can tell the
    truth about what was attached. Telling a model "you are seeing five keyframes"
    when only two were extracted is how you get confident nonsense.
    """
    material: list[str] = []
    if frame_labels:
        material.append(
            f"- {len(frame_labels)} still keyframes, sampled across the clip, at: "
            f"{', '.join(frame_labels)}"
        )
    if has_video:
        material.append("- the full video clip")
    if has_previous_frames:
        material.append("- frames from the final moments of the previous segment")
    if has_audio:
        material.append("- the video's audio track")

    # Be straight about what is missing. The previous wording told the model it had
    # the full clip and the audio track even though it only ever received stills,
    # which is how a vision model ends up reporting a confident `motion_score` for
    # footage it never watched.
    if not has_video:
        material.append(
            "- NOTE: these are STILL FRAMES, not the video. You cannot see motion, "
            "timing, or camera shake play out, and you cannot hear anything."
        )
    if not has_audio:
        material.append(
            "- NOTE: no audio was provided. Report `audio_continuity_score` as 1.0 "
            "and say in `notes` that audio was not assessable. Do not penalise a "
            "track you were not given."
        )
    if not has_video and len(frame_labels) <= 1:
        material.append(
            "- NOTE: you have at most one still frame, so you cannot judge motion at "
            "all. Set `motion_score` to 0.0 and say why in `notes`."
        )

    blocks: list[str] = [SYSTEM_ROLE, ""]

    blocks.append("Material you are being given:")
    blocks.extend(material or ["- (no media attached; report what you cannot assess)"])
    blocks.append("")

    if local_media_summary:
        blocks.append(
            "Container metadata measured locally (authoritative for duration and "
            f"dimensions): {local_media_summary}"
        )
        blocks.append("")

    if segment is not None:
        blocks.append("What this segment was supposed to be:")
        blocks.append(f"  start state: {segment.start_state}")
        blocks.append(f"  action: {segment.action}")
        blocks.append(f"  camera: {segment.camera_behavior}")
        blocks.append(f"  expected end state: {segment.end_state}")
        blocks.append("")

    if bible is not None:
        blocks.append("Continuity constraints that must hold:")
        blocks.append(f"  subject: {bible.subject_identity}")
        if bible.immutable_face_traits:
            blocks.append(f"  face: {', '.join(bible.immutable_face_traits)}")
        if bible.hair:
            blocks.append(f"  hair: {', '.join(bible.hair)}")
        if bible.outfit:
            blocks.append(f"  outfit: {', '.join(bible.outfit)}")
        if bible.accessories:
            blocks.append(f"  accessories: {', '.join(bible.accessories)}")
        if bible.environment_topology:
            blocks.append(f"  space: {', '.join(bible.environment_topology)}")
        if bible.lighting:
            blocks.append(f"  lighting: {', '.join(bible.lighting)}")
        blocks.append("")

    if is_extension:
        blocks.append(
            "This is an EXTENSION. Compare its opening directly against the previous "
            "segment's final frames. The most common defects are a scene reset, a "
            "restart of the story, a pose that does not follow from the previous "
            "ending, and a change of clothing, hair, or lighting."
        )
        blocks.append("")

    if segment_prompt:
        blocks.append("The prompt that produced this segment was:")
        blocks.append(segment_prompt)
        blocks.append("")

    blocks.append(SCORING_GUIDANCE)
    blocks.append("")
    blocks.append(HARD_REJECT_GUIDANCE)
    blocks.append("")
    blocks.append(HONESTY_CLAUSE)
    blocks.append("")
    blocks.append(
        '`suggested_edit_prompt`: only when the verdict is "edit". It must describe '
        "exactly one local correction and nothing else. Set it to null otherwise."
    )
    blocks.append("")
    blocks.append(REPORT_SCHEMA_DESCRIPTION)

    return "\n".join(blocks)


def parse_critic_response(text: str, *, critic_model: str) -> CritiqueReport:
    """Parse the Critic's reply into a `CritiqueReport`.

    Tolerates a fenced code block and leading prose, because models add both. A
    response we cannot parse raises rather than defaulting to a passing report —
    §21.2's exit criteria require that a Critic failure is never mistaken for a pass.
    """
    import re

    cleaned = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()

    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]

    # Both failure modes below are wrapped as `InvalidRequestError` on purpose. The
    # caller in `agents/critic.py` catches that type to fall back to
    # `_unparseable_report` — the all-zero, degraded, HUMAN_REVIEW report that
    # RECOVERY.md documents. Raising the raw `JSONDecodeError` or pydantic
    # `ValidationError` escaped that guard, so an unparseable reply propagated out of
    # the orchestrator (which catches only `OmniVlogError`) and reached the user as a
    # traceback, leaving the job stranded in SEED_REVIEW with every re-run crashing
    # the same way.
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(
            f"The Critic's reply was not valid JSON: {exc}",
            detail={"preview": cleaned[:400]},
        ) from exc

    if not isinstance(payload, dict):
        raise InvalidRequestError(
            "The Critic's reply was valid JSON but not an object.",
            detail={"type": type(payload).__name__},
        )

    payload.setdefault("verdict", "human_review")
    payload["critic_model"] = critic_model

    try:
        return CritiqueReport.model_validate(payload)
    except ValidationError as exc:
        # `extra="forbid"` on CritiqueReport means one unexpected key is enough,
        # and a reply truncated at max_output_tokens is the other common case.
        raise InvalidRequestError(
            f"The Critic's report did not match the expected schema: {exc.error_count()} "
            f"problem(s), first: {exc.errors()[0].get('loc')} "
            f"{exc.errors()[0].get('msg')}",
            detail={"preview": cleaned[:400]},
        ) from exc


#: The most common way a Critic result is wrong: a `verdict` that contradicts the
#: scores or the flags. The decision policy re-derives the decision anyway, but a
#: contradictory report is worth recording so the disagreement is visible.
def describe_contradictions(report: CritiqueReport) -> list[str]:
    issues: list[str] = []
    if report.verdict == "accept":
        if report.severe_defects:
            issues.append(
                f"verdict is accept but severe_defects is non-empty: {report.severe_defects}"
            )
        if report.timestamp_detected or report.ui_detected or report.text_overlay_detected:
            issues.append("verdict is accept but a text/UI/timestamp artefact was detected")
        if report.montage_detected:
            issues.append("verdict is accept but montage_detected is true")
    if report.verdict == "edit" and not report.suggested_edit_prompt:
        issues.append("verdict is edit but no suggested_edit_prompt was supplied")
    if report.verdict == "edit" and report.editable_defects == []:
        issues.append("verdict is edit but editable_defects is empty")
    return issues
