"""Keyframe extraction for the Critic (§11.1).

The plan asks the Critic to receive first / 25% / 50% / 75% / last frames. That
needs ffmpeg. When ffmpeg is absent we do **not** silently hand the Critic fewer
frames and let it score anyway — a Critic that has seen one frame and reports a
`motion_score` is worse than one that says it could not look.

So extraction returns a `FrameSet` that records its own completeness, and
`agents/critic.py` checks it: an incomplete frame set forces the report to be
marked degraded, which the decision policy then refuses to auto-accept.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from omni_homevlog.config import get_settings
from omni_homevlog.errors import MediaToolMissingError
from omni_homevlog.media.ffprobe import ffmpeg_available, inspect_media
from omni_homevlog.observability.logging import get_logger

logger = get_logger("frames")

#: Fractions of the clip the Critic should see.
DEFAULT_FRACTIONS: tuple[float, ...] = (0.0, 0.25, 0.50, 0.75, 0.98)


@dataclass(slots=True)
class ExtractedFrame:
    path: Path
    at_s: float
    fraction: float
    label: str


@dataclass(slots=True)
class FrameSet:
    frames: list[ExtractedFrame] = field(default_factory=list)
    complete: bool = False
    duration_s: float | None = None
    extraction_tool: str | None = None
    #: Populated when we could not get the frames we wanted.
    degradation_reason: str | None = None

    def labels(self) -> list[str]:
        return [f.label for f in self.frames]

    def paths(self) -> list[Path]:
        return [f.path for f in self.frames]

    def tail_frames(self, count: int = 2) -> list[ExtractedFrame]:
        """The final frames, used as the continuity anchor for extensions."""
        return self.frames[-count:] if self.frames else []

    def as_dict(self) -> dict[str, object]:
        return {
            "count": len(self.frames),
            "complete": self.complete,
            "duration_s": self.duration_s,
            "extraction_tool": self.extraction_tool,
            "degradation_reason": self.degradation_reason,
            "frames": [
                {"label": f.label, "at_s": round(f.at_s, 3), "path": str(f.path)}
                for f in self.frames
            ],
        }


def _run(cmd: list[str], *, timeout_s: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)


def extract_frames(
    video_path: str | Path,
    out_dir: str | Path,
    *,
    fractions: tuple[float, ...] = DEFAULT_FRACTIONS,
    width: int | None = None,
    require_complete: bool = False,
) -> FrameSet:
    """Pull frames at `fractions` of the clip.

    `require_complete=True` raises instead of degrading, for callers that cannot
    proceed with a partial set.
    """
    video = Path(video_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    info = inspect_media(video)
    result = FrameSet(duration_s=info.duration_s)

    if not video.is_file():
        result.degradation_reason = f"video not found: {video}"
        if require_complete:
            raise MediaToolMissingError(result.degradation_reason)
        return result

    if info.duration_s is None:
        result.degradation_reason = (
            "could not determine duration, so keyframe timestamps are unknown"
        )
        if require_complete:
            raise MediaToolMissingError(result.degradation_reason)
        return result

    binary = get_settings().omni_ffmpeg_bin
    if not ffmpeg_available(binary):
        result.degradation_reason = (
            f"ffmpeg ({binary!r}) not found on PATH; cannot extract keyframes. "
            "Install it (`brew install ffmpeg`) for frame-level review."
        )
        if require_complete:
            raise MediaToolMissingError(result.degradation_reason)
        return result

    result.extraction_tool = "ffmpeg"
    duration = info.duration_s
    # Stay a hair inside the file: seeking to exactly `duration` often returns
    # nothing because that timestamp is past the last decodable frame.
    safe_duration = max(0.0, duration - 0.05)

    for fraction in fractions:
        label = f"f{fraction:.2f}".replace(".", "")
        at_s = round(safe_duration * fraction, 3)
        target = out / f"{label}.jpg"
        scale = f"scale={width}:-2" if width else "scale=iw:ih"
        cmd = [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{at_s:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            scale,
            "-q:v",
            "3",
            "-y",
            str(target),
        ]
        try:
            proc = _run(cmd)
        except subprocess.TimeoutExpired:
            logger.warning(
                "ffmpeg frame extraction timed out",
                extra={"extra_fields": {"at_s": at_s, "video": str(video)}},
            )
            continue
        if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            logger.debug(
                "Frame extraction missed",
                extra={
                    "extra_fields": {
                        "at_s": at_s,
                        "code": proc.returncode,
                        "stderr": proc.stderr[:200],
                    }
                },
            )
            continue
        result.frames.append(ExtractedFrame(path=target, at_s=at_s, fraction=fraction, label=label))

    result.complete = len(result.frames) == len(fractions)
    if not result.complete and not result.degradation_reason:
        result.degradation_reason = f"extracted {len(result.frames)} of {len(fractions)} keyframes"
        if require_complete:
            raise MediaToolMissingError(result.degradation_reason)

    return result


def extract_tail_frames(
    video_path: str | Path,
    out_dir: str | Path,
    *,
    count: int = 2,
    window_s: float = 1.5,
) -> FrameSet:
    """Frames from the last `window_s` seconds — the extension continuity anchor.

    §11.1 asks for the previous segment's final 1-2 seconds when reviewing an
    extension. Sampling inside a fixed window is more useful than the last frame
    alone, because a single frame cannot show motion direction.
    """
    video = Path(video_path)
    info = inspect_media(video)
    if info.duration_s is None or info.duration_s <= 0:
        # `<= 0` as well as `is None`: a fragmented MP4 legitimately reports a zero
        # duration, and dividing by it raised a ZeroDivisionError that nothing on
        # the review path caught, so every resume crashed the same way.
        return FrameSet(
            duration_s=info.duration_s,
            degradation_reason=("duration is unknown or zero; cannot locate the tail window"),
        )

    start = max(0.0, info.duration_s - window_s)
    span = max(0.001, info.duration_s - start)
    fractions = tuple(
        start / info.duration_s + (span / info.duration_s) * (i / max(1, count - 1))
        for i in range(count)
    )
    return extract_frames(video, out_dir, fractions=fractions)


def make_contact_sheet(
    frames: FrameSet,
    out_path: str | Path,
    *,
    columns: int | None = None,
) -> Path | None:
    """One image combining the keyframes, for models that prefer a single input.

    NOTE: this sheet is for the *Critic*, never for the video model. §24.2 and
    §6 forbid feeding a collage to Omni as a reference; it would teach the model
    to render panel borders. The Critic is a different model doing a different
    job, and reading a contact sheet is exactly what it is for.

    Returns None when the frames or ffmpeg are unavailable.
    """
    if not frames.frames:
        return None
    binary = get_settings().omni_ffmpeg_bin
    if not ffmpeg_available(binary):
        return None

    cols = columns or len(frames.frames)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Build a filter graph that tiles the frames: [0][1]...[n]xstack or a simple
    # hstack for a single row, which covers our five-frame default.
    inputs: list[str] = []
    for frame in frames.frames:
        inputs += ["-i", str(frame.path)]

    if len(frames.frames) == 1:
        shutil.copyfile(frames.frames[0].path, target)
        return target

    if cols == len(frames.frames):
        layout = "".join(f"[{i}]" for i in range(len(frames.frames)))
        filter_complex = f"{layout}hstack=inputs={len(frames.frames)}"
    else:
        rows = (len(frames.frames) + cols - 1) // cols
        layout = "".join(f"[{i}]" for i in range(len(frames.frames)))
        filter_complex = (
            f"{layout}xstack=inputs={len(frames.frames)}:layout="
            + "|".join(f"{(i % cols)}*iw{(i // cols)}*ih" for i in range(len(frames.frames)))
            if rows
            else ""
        )

    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        *inputs,
        "-filter_complex",
        filter_complex,
        "-frames:v",
        "1",
        "-y",
        str(target),
    ]
    try:
        proc = _run(cmd)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not target.is_file():
        logger.debug(
            "contact sheet build failed",
            extra={"extra_fields": {"stderr": proc.stderr[:300]}},
        )
        return None
    return target
