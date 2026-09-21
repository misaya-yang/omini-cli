"""Media metadata.

Two paths, one answer:

  1. `ffprobe` when it is installed — authoritative, and the only source for
     stream-level detail.
  2. `media/mp4_probe.py` otherwise — pure Python, good enough to answer "is
     this really a 3-second 640x360 H.264+AAC MP4", which is what the review
     pipeline and the smoke test actually need.

`MediaInfo.probed_with` records which path ran, so a report never implies more
precision than it has.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from omni_homevlog.config import get_settings
from omni_homevlog.media.mp4_probe import probe_mp4
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.schemas import MediaInfo

logger = get_logger("ffprobe")


def ffprobe_available(binary: str | None = None) -> bool:
    name = binary or get_settings().omni_ffprobe_bin
    return shutil.which(name) is not None


def ffmpeg_available(binary: str | None = None) -> bool:
    name = binary or get_settings().omni_ffmpeg_bin
    return shutil.which(name) is not None


def _run_ffprobe(path: Path, binary: str, timeout_s: float = 60.0) -> dict[str, Any] | None:
    cmd = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ffprobe failed", extra={"extra_fields": {"error": str(exc)}})
        return None
    if proc.returncode != 0:
        logger.debug(
            "ffprobe non-zero exit",
            extra={"extra_fields": {"code": proc.returncode, "stderr": proc.stderr[:300]}},
        )
        return None
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _from_ffprobe(payload: dict[str, Any], *, size_bytes: int | None) -> MediaInfo:
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration: float | None = None
    for candidate in (fmt.get("duration"), (video or {}).get("duration")):
        if candidate is not None:
            try:
                duration = float(candidate)
                break
            except (TypeError, ValueError):
                continue

    width = height = None
    if video:
        try:
            width = int(video["width"]) if video.get("width") else None
            height = int(video["height"]) if video.get("height") else None
        except (KeyError, TypeError, ValueError):
            pass

    return MediaInfo(
        container=(fmt.get("format_name") or "").split(",")[0] or None,
        duration_s=duration,
        width=width,
        height=height,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        has_audio=audio is not None,
        size_bytes=size_bytes,
        probed_with="ffprobe",
    )


def inspect_media(path: str | Path, *, prefer_ffprobe: bool = True) -> MediaInfo:
    """Describe a media file as well as the available tooling allows."""
    p = Path(path)
    if not p.is_file():
        return MediaInfo(probed_with="none", size_bytes=0)

    size = p.stat().st_size
    binary = get_settings().omni_ffprobe_bin

    if prefer_ffprobe and ffprobe_available(binary):
        payload = _run_ffprobe(p, binary)
        if payload is not None:
            info = _from_ffprobe(payload, size_bytes=size)
            info.c2pa_present, info.c2pa_detail = detect_c2pa(p)
            return info
        logger.info("ffprobe produced no parsable output; falling back to the pure-Python probe")

    fallback = probe_mp4(p)
    return MediaInfo(
        container=fallback.container,
        duration_s=fallback.duration_s,
        width=fallback.width,
        height=fallback.height,
        video_codec=fallback.video_codec,
        audio_codec=fallback.audio_codec,
        has_audio=fallback.has_audio,
        size_bytes=size,
        probed_with="mp4_probe",
        c2pa_present=fallback.c2pa_present,
        c2pa_detail=fallback.c2pa_detail,
        # Carried through so a caller can tell "unreadable" from "readable but
        # zero-length". Without this the pure-Python path would report a corrupt
        # file as a valid one with unknown duration.
        error=fallback.error,
    )


def detect_c2pa(path: str | Path) -> tuple[bool | None, list[str]]:
    """Is a C2PA manifest attached?

    Detection only. §22 and §24.13 forbid stripping content credentials; nothing
    in this codebase removes or rewrites them. A transcode through ffmpeg *may*
    drop them, which is why `RenderArtifact.derived` exists and why the manifest
    flags derived files explicitly.
    """
    p = Path(path)
    if not p.is_file():
        return None, ["file missing"]
    if p.suffix.lower() in {".mp4", ".mov", ".m4v"}:
        info = probe_mp4(p)
        return info.c2pa_present, info.c2pa_detail
    return None, [f"no C2PA detector for extension {p.suffix!r}"]


def assert_expected_media(
    info: MediaInfo,
    *,
    expected_duration_s: float | None = None,
    expected_width: int | None = None,
    expected_height: int | None = None,
    duration_tolerance_s: float = 0.6,
) -> tuple[bool, list[str]]:
    """Compare a probe against what we asked the model to produce.

    The handoff's acceptance criterion is that the *output* is verified, not the
    HTTP status. This is that check. Unknown values (`None`) are not failures —
    they are reported as unverified, because claiming a pass we did not measure
    would be worse than admitting we could not measure it.
    """
    problems: list[str] = []

    if info.duration_s is None:
        problems.append("duration could not be determined")
    elif (
        expected_duration_s is not None
        and abs(info.duration_s - expected_duration_s) > duration_tolerance_s
    ):
        problems.append(
            f"duration {info.duration_s:.2f}s differs from requested "
            f"{expected_duration_s}s by more than {duration_tolerance_s}s"
        )

    if expected_width is not None and info.width is not None and info.width != expected_width:
        problems.append(f"width {info.width} != expected {expected_width}")
    if expected_height is not None and info.height is not None and info.height != expected_height:
        problems.append(f"height {info.height} != expected {expected_height}")
    if expected_width is not None and info.width is None:
        problems.append("width could not be determined")
    if expected_height is not None and info.height is None:
        problems.append("height could not be determined")

    return (not problems), problems


def resolution_to_dimensions(resolution: str, aspect_ratio: str) -> tuple[int | None, int | None]:
    """Expected pixel dimensions for a resolution label + aspect ratio.

    Vertical short edge is 360/720/1080/2160; the long edge follows the ratio.
    """
    short_edge = {"360p": 360, "720p": 720, "1080p": 1080, "4k": 2160}.get(resolution)
    if short_edge is None:
        return None, None
    if aspect_ratio == "16:9":
        return int(round(short_edge * 16 / 9 / 2) * 2), short_edge
    if aspect_ratio == "9:16":
        return short_edge, int(round(short_edge * 16 / 9 / 2) * 2)
    return None, None
