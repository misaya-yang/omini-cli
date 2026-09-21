"""Shared test fixtures.

The guiding rule for this suite: **no test may reach the network or spend money.**
Every provider call is either mocked or replaced. `tests/live/` is the only place
that touches a real API, and it is skipped unless `RUN_LIVE_VIDEO_TESTS=1`.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from omni_homevlog.config import reset_settings_cache
from omni_homevlog.schemas import (
    ContinuityBible,
    CritiqueReport,
    JobState,
    Manifest,
    ProjectSpec,
    ReferenceAsset,
    SegmentPlan,
)


@pytest.fixture(autouse=True)
def _clean_settings_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Isolate every test from the developer's real environment and data dir."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("RUN_LIVE_VIDEO_TESTS", "0")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def spec() -> ProjectSpec:
    return ProjectSpec(
        title="test job",
        brief="30-second intimate boyfriend-POV home vlog",
        target_duration_s=30,
        aspect_ratio="9:16",
        resolution="360p",
        provider="vertex",
        project="test-project",
        model="gemini-omni-1.1-flash-preview",
    )


@pytest.fixture
def bible() -> ContinuityBible:
    return ContinuityBible(
        subject_identity="a clearly adult woman, consistent across every segment",
        immutable_face_traits=["stable facial structure"],
        hair=["long dark hair"],
        outfit=["casual home outfit"],
        environment_topology=["apartment with a dining area and a bedroom"],
        lighting=["warm indoor lighting"],
        camera_grammar=["handheld phone at close distance"],
    )


@pytest.fixture
def segments() -> list[SegmentPlan]:
    return [
        SegmentPlan(
            index=0,
            intended_duration_s=10,
            start_state="seated at the dining table with a mug",
            action="she lifts the mug and takes a small sip",
            camera_behavior="close handheld, slight sway, no cut",
            environment="the dining area",
            emotional_beat="sleepy and affectionate",
            audio_intent="quiet room tone",
            end_state="she lowers the mug and begins to stand",
            continuation_anchor="beginning to stand, mug in one hand",
        ),
        SegmentPlan(
            index=1,
            intended_duration_s=10,
            start_state="beginning to stand, mug in one hand",
            action="she walks toward the bedroom while the camera follows",
            camera_behavior="one continuous follow shot",
            environment="the corridor to the bedroom",
            emotional_beat="shy and playful",
            audio_intent="footsteps and room tone",
            end_state="she reaches the bedroom doorway and reaches a hand back",
            continuation_anchor="one hand extended back toward the camera at the doorway",
        ),
        SegmentPlan(
            index=2,
            intended_duration_s=10,
            start_state="one hand extended back toward the camera at the doorway",
            action="she sits on the edge of the bed and leans closer",
            camera_behavior="move closer slowly, handheld",
            environment="the softly lit bedroom",
            emotional_beat="close and affectionate",
            audio_intent="soft bedding sounds",
            end_state="she covers the lens with her palm",
            continuation_anchor="her palm covering the lens entirely",
        ),
    ]


@pytest.fixture
def reference_asset(tmp_path: Path) -> ReferenceAsset:
    path = tmp_path / "face.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
    return ReferenceAsset(
        id="ref00_identity_closeup",
        path_or_uri=str(path),
        role="identity_closeup",
        sha256="a" * 64,
        provenance="synthetic",
        approved=True,
        mime_type="image/png",
    )


@pytest.fixture
def manifest(spec: ProjectSpec, bible: ContinuityBible, segments: list[SegmentPlan]) -> Manifest:
    return Manifest(
        job_id="job-20260921-120000-abc123",
        provider="vertex",
        project="test-project",
        model="gemini-omni-1.1-flash-preview",
        state=JobState.PLAN_READY,
        spec=spec,
        continuity_bible=bible,
        segment_plan=segments,
    )


# ── report factories ───────────────────────────────────────────────────────


def make_report(**overrides: Any) -> CritiqueReport:
    """A passing report. Override any field to construct a specific case."""
    payload: dict[str, Any] = {
        "identity_score": 0.95,
        "anatomy_score": 0.92,
        "motion_score": 0.90,
        "spatial_continuity_score": 0.93,
        "camera_realism_score": 0.91,
        "reference_fidelity_score": 0.88,
        "text_overlay_detected": False,
        "timestamp_detected": False,
        "ui_detected": False,
        "montage_detected": False,
        "severe_defects": [],
        "editable_defects": [],
        "suggested_edit_prompt": None,
        "verdict": "accept",
    }
    payload.update(overrides)
    return CritiqueReport.model_validate(payload)


@pytest.fixture
def passing_report() -> CritiqueReport:
    return make_report()


# ── synthetic media ────────────────────────────────────────────────────────


def build_minimal_mp4(
    *,
    duration_s: float = 3.0,
    width: int = 640,
    height: int = 360,
    timescale: int = 1000,
    with_audio: bool = True,
    with_c2pa: bool = False,
) -> bytes:
    """Build a small but structurally valid MP4.

    Real enough that `media/mp4_probe.py` reads its duration, dimensions, codecs,
    audio presence, and C2PA box from it. Written by hand rather than with a
    library so the tests do not depend on ffmpeg being installed.
    """

    def box(box_type: bytes, payload: bytes = b"") -> bytes:
        return struct.pack(">I", 8 + len(payload)) + box_type + payload

    def full_box(box_type: bytes, version: int, flags: int, payload: bytes = b"") -> bytes:
        return box(box_type, struct.pack(">B", version) + flags.to_bytes(3, "big") + payload)

    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")

    def trak(handler: bytes, codec: bytes) -> bytes:
        duration = int(duration_s * timescale)
        tkhd = full_box(
            b"tkhd",
            0,
            7,
            struct.pack(">IIIII", 0, 0, 0, 0, duration)
            + b"\x00" * 52
            + struct.pack(">II", int(width * 65536), int(height * 65536)),
        )
        hdlr = full_box(b"hdlr", 0, 0, struct.pack(">I", 0) + handler + b"\x00" * 12)
        # mdhd: version 0 -> creation, modification, timescale, duration
        mdhd = full_box(
            b"mdhd", 0, 0, struct.pack(">IIII", 0, 0, timescale, duration) + b"\x55\xc4\x00\x00"
        )
        stsd = full_box(b"stsd", 0, 0, struct.pack(">I", 1) + box(codec, b"\x00" * 8))
        stbl = box(b"stbl", stsd)
        minf = box(b"minf", stbl)
        mdia = box(b"mdia", mdhd + hdlr + minf)
        return box(b"trak", tkhd + mdia)

    tracks = trak(b"vide", b"avc1")
    if with_audio:
        tracks += trak(b"soun", b"mp4a")

    mvhd = full_box(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, timescale, int(duration_s * timescale))
        + struct.pack(">IHH", 0x00010000, 0x0100, 0)
        + b"\x00" * 8
        + struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        + b"\x00" * 24
        + struct.pack(">I", 2),
    )
    moov = box(b"moov", mvhd + tracks)

    parts = [ftyp]
    if with_c2pa:
        parts.append(box(b"jumb", b"c2pa manifest placeholder" + b"\x00" * 64))
    parts.append(moov)
    # A minimal mdat so the file is not obviously synthetic by size alone.
    parts.append(box(b"mdat", b"\x00" * 2048))
    return b"".join(parts)


@pytest.fixture
def sample_mp4(tmp_path: Path) -> Path:
    path = tmp_path / "sample.mp4"
    path.write_bytes(build_minimal_mp4())
    return path


@pytest.fixture
def sample_mp4_with_c2pa(tmp_path: Path) -> Path:
    path = tmp_path / "sample_c2pa.mp4"
    path.write_bytes(build_minimal_mp4(with_c2pa=True))
    return path


# ── provider response fixtures ─────────────────────────────────────────────


def sse_body(events: list[dict[str, Any]]) -> str:
    """Encode events the way the verified Vertex endpoint does."""
    lines = [f"data: {json.dumps(event)}" for event in events]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def completed_interaction_event(
    *,
    interaction_id: str = "int-abc123",
    video_uri: str | None = None,
    video_b64: str | None = None,
    model: str = "gemini-omni-1.1-flash-preview",
    usage: dict[str, Any] | None = None,
    status: str = "completed",
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A verified-shape `interaction.completed` SSE event."""
    content: dict[str, Any] = {"type": "video"}
    if video_uri:
        content["uri"] = video_uri
        content["mime_type"] = "video/mp4"
    if video_b64:
        content["data"] = video_b64
        content["mime_type"] = "video/mp4"

    interaction: dict[str, Any] = {
        "id": interaction_id,
        "status": status,
        "model": model,
        "created": "2026-09-21T12:00:00Z",
        "steps": [{"type": "model_output", "content": [content]}],
        "usage": usage or {"text_input_tokens": 45, "video_output_tokens": 5793},
    }
    if errors:
        interaction["errors"] = errors
    return {"event_type": "interaction.completed", "interaction": interaction}
