"""Transcoding and concatenation — used sparingly, and always labelled.

Two hard rules from the plan shape this module:

  §24.3 — the 30-second deliverable must come from the provider's **native**
  extension chain, never from `ffmpeg concat` of three independently generated
  clips. `concat_native_chain` exists only for the case where the provider
  already produced one continuous chain split across files, and it refuses to
  run unless the caller confirms that is what happened.

  §22 / §24.13 — a transcode can drop C2PA content credentials. Any file this
  module writes is marked `derived=True` in the manifest, and we record the
  C2PA state before and after so a lost manifest is visible rather than silent.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from omni_homevlog.config import get_settings
from omni_homevlog.errors import MediaToolMissingError, OmniVlogError
from omni_homevlog.media.ffprobe import detect_c2pa, ffmpeg_available, inspect_media
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.schemas import MediaInfo

logger = get_logger("transcode")


class ConcatenationRefusedError(OmniVlogError):
    """Raised when a caller asks us to fake a continuous chain by splicing."""

    code = "concatenation_refused"


@dataclass(slots=True)
class TranscodeResult:
    output_path: Path
    media: MediaInfo
    derived: bool = True
    c2pa_before: bool | None = None
    c2pa_after: bool | None = None
    notes: list[str] | None = None


def _require_ffmpeg() -> str:
    binary = get_settings().omni_ffmpeg_bin
    if not ffmpeg_available(binary):
        raise MediaToolMissingError(
            f"ffmpeg ({binary!r}) not found on PATH. Install it "
            "(`brew install ffmpeg`) or skip the transcoding step."
        )
    return binary


def _run(cmd: list[str], *, timeout_s: float = 1800.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)


def _concat_quote(path: Path) -> str:
    """Escape a path for the ffmpeg concat demuxer.

    The demuxer's grammar is `file '<path>'`, where a literal single quote is
    written by closing the string, emitting an escaped quote, and reopening it:
    `'` becomes `'\\''`.
    """
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'"


def _concat_list_file(paths: list[Path]) -> str:
    return "\n".join(_concat_quote(p) for p in paths) + "\n"


def remux(src: str | Path, dst: str | Path) -> TranscodeResult:
    """Copy streams into a new container without re-encoding.

    Lossless and fast. Preferred over a re-encode whenever the only goal is a
    tidy container, because it is the least likely transform to disturb the
    content credentials.
    """
    binary = _require_ffmpeg()
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)

    c2pa_before, _ = detect_c2pa(source)
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        str(target),
    ]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise OmniVlogError(
            f"ffmpeg remux failed ({proc.returncode}): {proc.stderr[:400]}",
            detail={"src": str(source)},
        )
    c2pa_after, _ = detect_c2pa(target)
    notes = []
    if c2pa_before and not c2pa_after:
        notes.append(
            "C2PA was present in the source and is not detectable in the remux. "
            "The derived file is flagged in the manifest; do not present it as the "
            "original credential-bearing output."
        )
    return TranscodeResult(
        output_path=target,
        media=inspect_media(target),
        derived=True,
        c2pa_before=c2pa_before,
        c2pa_after=c2pa_after,
        notes=notes,
    )


def concat_native_chain(
    parts: list[str | Path],
    dst: str | Path,
    *,
    confirmed_single_chain: bool,
) -> TranscodeResult:
    """Join files that are already ONE provider-native chain.

    §24.3 forbids building the deliverable by stitching three independent
    generations together. The `confirmed_single_chain` flag is a deliberate
    speed bump: the caller must assert that these parts came from a single
    stateful extension chain, not from three separate `create` calls.

    If you are tempted to pass `True` to get a 30-second file out of three
    unrelated renders: that is precisely the failure this guard exists to stop.
    """
    if not confirmed_single_chain:
        raise ConcatenationRefusedError(
            "Refusing to concatenate. The plan (§24.3) forbids producing a "
            "30-second deliverable by splicing independently generated clips. "
            "Use the provider's native extend chain. If these parts really are "
            "one native chain, pass confirmed_single_chain=True and say so in "
            "the job notes.",
            detail={"parts": [str(p) for p in parts]},
        )
    if len(parts) < 2:
        raise OmniVlogError("concat_native_chain needs at least two parts.")

    binary = _require_ffmpeg()
    targets = [Path(p) for p in parts]
    for target in targets:
        if not target.is_file():
            raise OmniVlogError(f"Missing concat input: {target}")

    output = Path(dst)
    output.parent.mkdir(parents=True, exist_ok=True)

    c2pa_before, _ = detect_c2pa(targets[0])

    list_path = output.with_suffix(".concat.txt")
    list_path.write_text(_concat_list_file(targets), encoding="utf-8")
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        str(output),
    ]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise OmniVlogError(
            f"ffmpeg concat failed ({proc.returncode}): {proc.stderr[:400]}",
            detail={"parts": [str(t) for t in targets]},
        )
    list_path.unlink(missing_ok=True)

    c2pa_after, _ = detect_c2pa(output)
    return TranscodeResult(
        output_path=output,
        media=inspect_media(output),
        derived=True,
        c2pa_before=c2pa_before,
        c2pa_after=c2pa_after,
        notes=["Concatenated from a confirmed single native chain."],
    )


def strip_audio(src: str | Path, dst: str | Path) -> TranscodeResult:
    """Remove the audio track, for silent deliverables."""
    binary = _require_ffmpeg()
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)

    c2pa_before, _ = detect_c2pa(source)
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-c:v",
        "copy",
        "-an",
        "-movflags",
        "+faststart",
        "-y",
        str(target),
    ]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise OmniVlogError(f"ffmpeg audio strip failed: {proc.stderr[:400]}")
    c2pa_after, _ = detect_c2pa(target)
    return TranscodeResult(
        output_path=target,
        media=inspect_media(target),
        derived=True,
        c2pa_before=c2pa_before,
        c2pa_after=c2pa_after,
    )


def copy_verbatim(src: str | Path, dst: str | Path) -> TranscodeResult:
    """Byte-for-byte copy. The only path that preserves content credentials exactly."""
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)

    c2pa_before, before_detail = detect_c2pa(source)
    c2pa_after, _ = detect_c2pa(target)
    return TranscodeResult(
        output_path=target,
        media=inspect_media(target),
        derived=False,
        c2pa_before=c2pa_before,
        c2pa_after=c2pa_after,
        notes=[f"verbatim copy; c2pa detail: {before_detail}"],
    )
