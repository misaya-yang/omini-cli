"""Critic (§4.1, §11).

Reviews a rendered segment with a *different* model from Omni, and returns a
`CritiqueReport`.

Two design commitments, both of which exist because the alternative produces
confidently wrong decisions:

**1. Degradation is explicit.** If keyframes could not be extracted, or no video
was supplied, the report is marked `critic_degraded` and the decision policy
refuses to auto-accept it. A Critic that saw one frame and reports
`motion_score = 0.9` is worse than one that says "I could not assess motion" —
the first authorises a paid extension from a fabricated number.

**2. The Critic advises; it does not decide.** `report.verdict` is recorded, and
`decision_policy` re-derives the real decision from the scores and flags. The two
are compared and a disagreement is logged, because a Critic that repeatedly says
"accept" on defective footage is information worth having.

Video understanding. The plan's §11.1 asks for the reference images, the bible,
the prompt, the current video, the previous segment's last 1-2 seconds, extracted
keyframes, and ffprobe metadata. We supply what we have and state plainly what we
do not — the prompt template carries a `has_*` flag per item for exactly this.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omni_homevlog.agents.llm import InlineImage, TextModelClient, build_client
from omni_homevlog.config import Settings, get_settings
from omni_homevlog.errors import InvalidRequestError
from omni_homevlog.media.extract_frames import FrameSet, extract_frames
from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.prompts.critic import (
    describe_contradictions,
    parse_critic_response,
    render_critic_prompt,
)
from omni_homevlog.schemas import (
    ContinuityBible,
    CritiqueReport,
    ReferenceAsset,
    SegmentPlan,
)

logger = get_logger("critic")

#: Inline image payloads must stay modest. A keyframe at review resolution is
#: ~40-80 KB, so five of them plus references fits comfortably; anything larger
#: is a sign the caller passed full-resolution frames by mistake.
MAX_INLINE_IMAGE_BYTES = 900_000
REVIEW_FRAME_WIDTH = 512

#: A report with fewer observable signals than this is treated as degraded no
#: matter what the model returned.
MIN_FRAMES_FOR_FULL_REVIEW = 3


@dataclass(slots=True)
class CriticInputs:
    """Everything gathered for one review, plus what was unavailable."""

    segment: SegmentPlan | None
    bible: ContinuityBible | None
    is_extension: bool
    video_path: Path | None = None
    frame_set: FrameSet | None = None
    previous_frames: FrameSet | None = None
    reference_paths: list[Path] = field(default_factory=list)
    segment_prompt: str | None = None
    local_media_summary: str | None = None

    def has_video(self) -> bool:
        return bool(self.video_path and Path(self.video_path).is_file())

    def frame_labels(self) -> list[str]:
        return self.frame_set.labels() if self.frame_set else []

    def unavailability_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self.has_video():
            reasons.append("the video file was not available to the Critic")
        if not self.frame_set or not self.frame_set.frames:
            reasons.append(
                self.frame_set.degradation_reason
                if self.frame_set and self.frame_set.degradation_reason
                else "no keyframes could be extracted"
            )
        elif not self.frame_set.complete:
            reasons.append(
                self.frame_set.degradation_reason or "only some keyframes were extracted"
            )
        if self.is_extension and (not self.previous_frames or not self.previous_frames.frames):
            reasons.append(
                "no frames from the previous segment, so continuity_with_previous "
                "could not be assessed visually"
            )
        return reasons


@dataclass(slots=True)
class CriticResult:
    report: CritiqueReport
    inputs: CriticInputs
    contradictions: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    @property
    def degraded(self) -> bool:
        return self.report.critic_degraded


class Critic:
    """Reviews rendered segments."""

    def __init__(
        self,
        *,
        client: TextModelClient | None = None,
        settings: Settings | None = None,
        extract_frames_when_missing: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self.extract_frames_when_missing = extract_frames_when_missing

    @property
    def client(self) -> TextModelClient:
        if self._client is None:
            self._client = build_client(
                model=self.settings.critic_model,
                provider=self.settings.omni_provider,
                settings=self.settings,
                location=self.settings.critic_location,
            )
        return self._client

    # ── input assembly ─────────────────────────────────────────────────────

    def gather_inputs(
        self,
        *,
        segment: SegmentPlan | None,
        bible: ContinuityBible | None,
        video_path: str | Path | None,
        is_extension: bool,
        previous_video_path: str | Path | None = None,
        reference_assets: list[ReferenceAsset] | None = None,
        segment_prompt: str | None = None,
        frames_dir: str | Path | None = None,
    ) -> CriticInputs:
        """Collect the review material, recording precisely what is missing."""
        video = Path(video_path) if video_path else None
        inputs = CriticInputs(
            segment=segment,
            bible=bible,
            is_extension=is_extension,
            video_path=video,
            segment_prompt=segment_prompt,
        )

        if video and video.is_file():
            info = inspect_media(video)
            if info.probed_with and info.probed_with != "none":
                inputs.local_media_summary = (
                    f"{info.width}x{info.height}, {info.duration_s}s, "
                    f"video={info.video_codec}, audio={info.audio_codec or 'none'}, "
                    f"{info.size_bytes} bytes (probed with {info.probed_with})"
                )

            if self.extract_frames_when_missing:
                out_dir = Path(frames_dir) if frames_dir else video.parent / "review_frames"
                inputs.frame_set = extract_frames(video, out_dir, width=REVIEW_FRAME_WIDTH)

        if is_extension and previous_video_path:
            previous = Path(previous_video_path)
            if previous.is_file() and self.extract_frames_when_missing:
                out_dir = (
                    Path(frames_dir) / "previous"
                    if frames_dir
                    else previous.parent / "review_frames_prev"
                )
                from omni_homevlog.media.extract_frames import extract_tail_frames

                inputs.previous_frames = extract_tail_frames(previous, out_dir, count=2)

        if reference_assets:
            for asset in reference_assets:
                path = Path(asset.path_or_uri)
                if path.is_file():
                    inputs.reference_paths.append(path)

        return inputs

    # ── review ─────────────────────────────────────────────────────────────

    def review(self, inputs: CriticInputs) -> CriticResult:
        """Run one review. Never returns an unmarked pass on thin evidence."""
        reasons = inputs.unavailability_reasons()
        images = self._collect_images(inputs)

        prompt = render_critic_prompt(
            segment=inputs.segment,
            bible=inputs.bible,
            is_extension=inputs.is_extension,
            has_previous_frames=bool(inputs.previous_frames and inputs.previous_frames.frames),
            # These two describe what the model can *perceive*, not what exists on
            # disk. `_collect_images` attaches base64 keyframes and nothing else,
            # and the text client has no path for video or audio bytes — so the
            # model never sees the clip and never hears the track.
            #
            # Passing `inputs.has_video()` here told the Critic it had "the full
            # video clip" and "the video's audio track" on every single review.
            # That suppressed the prompt's own anti-fabrication note (which fires
            # only when `not has_video`), so it reported motion and audio scores it
            # had invented from stills — and the decision policy authorised paid
            # extensions on those numbers.
            has_video=False,
            has_audio=False,
            frame_labels=inputs.frame_labels(),
            local_media_summary=inputs.local_media_summary,
            segment_prompt=inputs.segment_prompt,
        )

        try:
            response = self.client.generate(
                prompt=prompt,
                system_instruction=None,  # role text is already in the prompt
                images=images,
                temperature=0.2,
                max_output_tokens=4096,
            )
            report = parse_critic_response(response.text, critic_model=response.model)
            usage = response.usage
            raw_text = response.text
        except InvalidRequestError as exc:
            # A Critic we cannot parse must not become an implicit pass (§21.2).
            logger.error(
                "Critic returned unparseable output; forcing human review",
                extra={"extra_fields": {"error": exc.message}},
            )
            report = _unparseable_report(str(exc))
            usage = {}
            raw_text = ""

        frames_seen = len(inputs.frame_labels())
        if (frames_seen < MIN_FRAMES_FOR_FULL_REVIEW or not inputs.has_video()) and not reasons:
            reasons.append(
                f"only {frames_seen} frames were available, below the "
                f"{MIN_FRAMES_FOR_FULL_REVIEW} needed for a full visual review"
            )

        if reasons:
            report.critic_degraded = True
            report.notes.extend(f"review degraded: {r}" for r in reasons)
            if report.verdict == "accept":
                # Downgrade rather than let a thin review authorise an extension.
                report.verdict = "human_review"

        contradictions = describe_contradictions(report)
        for contradiction in contradictions:
            logger.warning(
                "Critic verdict contradicts its own report",
                extra={"extra_fields": {"detail": contradiction}},
            )

        return CriticResult(
            report=report,
            inputs=inputs,
            contradictions=contradictions,
            usage=usage,
            raw_text=raw_text,
        )

    def review_video(
        self,
        *,
        segment: SegmentPlan | None,
        bible: ContinuityBible | None,
        video_path: str | Path,
        is_extension: bool,
        previous_video_path: str | Path | None = None,
        reference_assets: list[ReferenceAsset] | None = None,
        segment_prompt: str | None = None,
        frames_dir: str | Path | None = None,
    ) -> CriticResult:
        """Convenience path: gather inputs then review."""
        inputs = self.gather_inputs(
            segment=segment,
            bible=bible,
            video_path=video_path,
            is_extension=is_extension,
            previous_video_path=previous_video_path,
            reference_assets=reference_assets,
            segment_prompt=segment_prompt,
            frames_dir=frames_dir,
        )
        return self.review(inputs)

    # ── image assembly ─────────────────────────────────────────────────────

    def _collect_images(self, inputs: CriticInputs) -> list[InlineImage]:
        """Attach frames and references, staying inside the payload budget.

        Ordering is intentional: current keyframes first, then the previous
        segment's tail, then the references. The most decision-relevant material
        should be least likely to be dropped if the budget is tight.
        """
        images: list[InlineImage] = []
        budget = MAX_INLINE_IMAGE_BYTES

        def add(path: Path, label: str) -> bool:
            nonlocal budget
            try:
                stat = path.stat()
            except OSError:
                return False
            if stat.st_size > budget:
                logger.debug(
                    "Skipping image for Critic: payload budget exhausted",
                    extra={"extra_fields": {"path": str(path), "label": label}},
                )
                return False
            try:
                data = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                return False
            images.append(InlineImage(data_b64=data, mime_type=_mime_for(path)))
            budget -= stat.st_size
            return True

        if inputs.frame_set:
            for frame in inputs.frame_set.frames:
                add(frame.path, f"frame {frame.label}")

        if inputs.previous_frames:
            for frame in inputs.previous_frames.frames:
                add(frame.path, "previous tail")

        for reference in inputs.reference_paths[:3]:
            add(reference, "reference")

        return images


def _mime_for(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/jpeg")


def _unparseable_report(reason: str) -> CritiqueReport:
    """The report used when the Critic's output cannot be trusted at all.

    Every score is 0.0 and the verdict is `human_review`. This is deliberately
    pessimistic: the alternative — guessing — is how a broken Critic silently
    approves bad footage.
    """
    return CritiqueReport(
        identity_score=0.0,
        anatomy_score=0.0,
        motion_score=0.0,
        spatial_continuity_score=0.0,
        camera_realism_score=0.0,
        reference_fidelity_score=0.0,
        text_overlay_detected=False,
        timestamp_detected=False,
        ui_detected=False,
        montage_detected=False,
        severe_defects=[f"critic output could not be parsed: {reason}"],
        editable_defects=[],
        suggested_edit_prompt=None,
        verdict="human_review",
        critic_model="unavailable",
        critic_degraded=True,
    )
