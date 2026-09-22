"""Core data models (Pydantic v2).

Field names follow the plan document §7 so that the JSON artifacts on disk
(`project_spec.json`, `continuity_bible.json`, `manifest.json`, ...) match the
contract that the plan and any downstream tooling expect.

Everything here is a *contract*, not a convenience: the Director model must emit
these shapes, the Critic must emit `CritiqueReport`, and the decision policy
reads `CritiqueReport` fields by name.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ────────────────────────────────────────────────────────────────────────────
# Scalar aliases
# ─────────────────────────────────────────────────────────────────────────────

Resolution = Literal["360p", "720p", "1080p", "4k"]
AspectRatio = Literal["9:16", "16:9"]
ProviderName = Literal["vertex", "gemini_api"]
TargetDuration = Literal[10, 20, 30, 40]
RunMode = Literal["concept", "production"]

#: Omni `generation_config.video_config.task` values (verified on Vertex).
VideoTask = Literal["text_to_video", "image_to_video", "reference_to_video", "edit", "extend"]

ReferenceRole = Literal[
    "identity_closeup",
    "identity_body",
    "outfit",
    "environment",
    "first_frame",
    "last_frame",
    "motion_reference",
]

Provenance = Literal["synthetic", "owned", "licensed", "unknown"]

#: Points where the pipeline stops and waits for a human decision (§7.1).
Gate = Literal["high-res", "final", "each-segment"]


def _default_gates() -> list[Gate]:
    """Default gate set.

    A named function rather than a lambda so the list element type is the `Gate`
    literal rather than a bare `str`, which the pydantic Field type wants.
    """
    return ["high-res"]


#: Roles that feed the *person's* appearance. These are the ones where a collage,
#: a timestamp, or a second distinct face is a hard blocker (§6.2).
IDENTITY_ROLES: frozenset[str] = frozenset({"identity_closeup", "identity_body", "outfit"})


def utc_now_iso() -> str:
    """Timestamp helper. Always UTC, always second precision, always sortable."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# §5 Task state machine
# ─────────────────────────────────────────────────────────────────────────────


class JobState(StrEnum):
    """States from plan §5.

    `EXTENSION_{n}_*` is a family: the plan names EXTENSION_1 and EXTENSION_2 for
    the default 30s chain; a 40s chain adds EXTENSION_3. The state machine builds
    transitions for however many extensions the project actually needs.
    """

    CREATED = "CREATED"
    REFERENCES_VALIDATED = "REFERENCES_VALIDATED"
    PLAN_READY = "PLAN_READY"

    SEED_RENDERING = "SEED_RENDERING"
    SEED_REVIEW = "SEED_REVIEW"
    SEED_ACCEPTED = "SEED_ACCEPTED"

    EXTENSION_1_RENDERING = "EXTENSION_1_RENDERING"
    EXTENSION_1_REVIEW = "EXTENSION_1_REVIEW"
    EXTENSION_1_ACCEPTED = "EXTENSION_1_ACCEPTED"

    EXTENSION_2_RENDERING = "EXTENSION_2_RENDERING"
    EXTENSION_2_REVIEW = "EXTENSION_2_REVIEW"
    EXTENSION_2_ACCEPTED = "EXTENSION_2_ACCEPTED"

    EXTENSION_3_RENDERING = "EXTENSION_3_RENDERING"
    EXTENSION_3_REVIEW = "EXTENSION_3_REVIEW"
    EXTENSION_3_ACCEPTED = "EXTENSION_3_ACCEPTED"

    FINAL_REVIEW = "FINAL_REVIEW"
    COMPLETE = "COMPLETE"

    # ── Exception states (§5) ───────────────────────────────────────────────
    NEEDS_HUMAN = "NEEDS_HUMAN"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


#: States from which no automatic transition is allowed.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETE, JobState.FAILED_FINAL, JobState.POLICY_BLOCKED}
)

#: States that hand control to a human until `omni-vlog approve` / `retry` runs.
HUMAN_GATE_STATES: frozenset[JobState] = frozenset(
    {JobState.NEEDS_HUMAN, JobState.BUDGET_EXHAUSTED, JobState.PROVIDER_UNAVAILABLE}
)

EXTENSION_STATES: dict[int, JobState] = {
    1: JobState.EXTENSION_1_ACCEPTED,
    2: JobState.EXTENSION_2_ACCEPTED,
    3: JobState.EXTENSION_3_ACCEPTED,
}


def extension_rendering_state(i: int) -> JobState:
    return JobState(f"EXTENSION_{i}_RENDERING")


def extension_review_state(i: int) -> JobState:
    return JobState(f"EXTENSION_{i}_REVIEW")


def extension_accepted_state(i: int) -> JobState:
    return JobState(f"EXTENSION_{i}_ACCEPTED")


# ─────────────────────────────────────────────────────────────────────────────
# §7.1 ProjectSpec
# ─────────────────────────────────────────────────────────────────────────────


class ProjectSpec(BaseModel):
    """The user's creative brief, normalised and frozen at job creation.

    `provider`, `project`, and `model` are *pinned* for the life of the job. §8.3
    and §24.4 forbid silently switching project mid-chain, so the spec carries the
    binding rather than reading it from the environment on each call.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    brief: str
    target_duration_s: TargetDuration = 30
    aspect_ratio: AspectRatio = "9:16"
    resolution: Resolution = "720p"
    audio_enabled: bool = True
    style: str = "intimate handheld boyfriend-POV home vlog, natural and unposed"
    provider: ProviderName
    max_total_calls: int = Field(default=8, ge=1)
    #: Ceiling on the *estimated* spend for this job. None means no ceiling.
    #: `omni-vlog create --max-cost` presented this and then dropped it, so the
    #: flag appeared to work while the budget ignored it entirely.
    max_estimated_cost_usd: Decimal | None = None

    max_llm_calls: int = Field(default=24, ge=1)
    background: bool = False

    # ── Extensions to the plan's §7.1 shape (all optional / defaulted) ───────
    mode: RunMode = "production"
    #: Pinned provider binding. See `providers/factory.py`.
    project: str | None = None
    model: str | None = None
    location: str | None = None
    #: GCS prefix for `delivery: "uri"`. None ⇒ inline base64 delivery.
    gcs_uri: str | None = None
    #: Concept mode renders a short probe instead of the full chain.
    concept_duration_s: int = Field(default=4, ge=3, le=6)
    #: Human gates. `high-res` gates the higher-resolution pass, `final` gates
    #: export, `each-segment` gates every segment.
    human_gates: list[Gate] = Field(default_factory=_default_gates)

    @field_validator("gcs_uri")
    @classmethod
    def _validate_gcs_uri(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.startswith("gs://"):
            raise ValueError(f"gcs_uri must start with 'gs://', got {v!r}")
        return v if v.endswith("/") else v + "/"

    @property
    def extension_count(self) -> int:
        """How many 10s extensions follow the seed segment."""
        return 0 if self.mode == "concept" else max(0, self.target_duration_s // 10 - 1)


# ─────────────────────────────────────────────────────────────────────────────
# §7.2 ContinuityBible
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_FORBIDDEN_VISUAL_ELEMENTS: list[str] = [
    "on-screen text",
    "timestamps",
    "shot numbers",
    "subtitles",
    "captions",
    "logos",
    "watermarks",
    "player UI",
    "storyboard panels",
    "split screen",
    "collage",
]

DEFAULT_FORBIDDEN_EDITING_PATTERNS: list[str] = [
    "rapid montage",
    "hard jump cuts",
    "identity drift",
    "outfit changes",
    "extra people",
]


class ContinuityBible(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_identity: str
    immutable_face_traits: list[str] = Field(default_factory=list)
    hair: list[str] = Field(default_factory=list)
    accessories: list[str] = Field(default_factory=list)
    outfit: list[str] = Field(default_factory=list)
    body_proportion: list[str] = Field(default_factory=list)
    environment_topology: list[str] = Field(default_factory=list)
    lighting: list[str] = Field(default_factory=list)
    camera_grammar: list[str] = Field(default_factory=list)
    interaction_style: list[str] = Field(default_factory=list)
    forbidden_visual_elements: list[str] = Field(
        default_factory=lambda: list(DEFAULT_FORBIDDEN_VISUAL_ELEMENTS)
    )
    forbidden_editing_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_FORBIDDEN_EDITING_PATTERNS)
    )

    def forbidden_phrase(self) -> str:
        """Single English clause listing every banned element.

        The Prompt Compiler inlines this into the main prompt because Omni is not
        driven by a separate negative-prompt parameter (§9.1).
        """
        items = [*self.forbidden_visual_elements, *self.forbidden_editing_patterns]
        return ", ".join(dict.fromkeys(items))


# ─────────────────────────────────────────────────────────────────────────────
# §7.3 SegmentPlan
# ─────────────────────────────────────────────────────────────────────────────


class SegmentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    intended_duration_s: int
    start_state: str
    action: str
    camera_behavior: str
    environment: str
    emotional_beat: str
    audio_intent: str
    end_state: str
    continuation_anchor: str
    max_distinct_actions: int = 3

    @field_validator("intended_duration_s")
    @classmethod
    def _duration_in_range(cls, v: int) -> int:
        # Handoff: generated duration is an integer 3s..10s.
        if not 3 <= v <= 10:
            raise ValueError(f"intended_duration_s must be 3..10, got {v}")
        return v

    @field_validator("index")
    @classmethod
    def _index_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("index must be >= 0")
        return v


class DirectorPlan(BaseModel):
    """The Director's full structured output: bible + ordered segments."""

    model_config = ConfigDict(extra="forbid")

    title: str
    logline: str
    continuity_bible: ContinuityBible
    segments: list[SegmentPlan]

    @field_validator("segments")
    @classmethod
    def _check_segments(cls, v: list[SegmentPlan]) -> list[SegmentPlan]:
        if not v:
            raise ValueError("Director must return at least one segment")
        for expected, seg in enumerate(v):
            if seg.index != expected:
                raise ValueError(
                    f"segments must be 0-indexed and contiguous; "
                    f"expected index {expected}, got {seg.index}"
                )
        return v


# ─────────────────────────────────────────────────────────────────────────────
# §7.4 CritiqueReport
# ────────────────────────────────────────────────────────────────────────────


class CritiqueReport(BaseModel):
    """Critic output. Scores are 0..1 and are *heuristic*, never ground truth (§11.5)."""

    model_config = ConfigDict(extra="forbid")

    identity_score: float = Field(ge=0.0, le=1.0)
    anatomy_score: float = Field(ge=0.0, le=1.0)
    motion_score: float = Field(ge=0.0, le=1.0)
    spatial_continuity_score: float = Field(ge=0.0, le=1.0)
    camera_realism_score: float = Field(ge=0.0, le=1.0)
    reference_fidelity_score: float = Field(ge=0.0, le=1.0)

    text_overlay_detected: bool
    timestamp_detected: bool
    ui_detected: bool
    montage_detected: bool

    severe_defects: list[str] = Field(default_factory=list)
    editable_defects: list[str] = Field(default_factory=list)
    suggested_edit_prompt: str | None = None

    # ── Extensions to the plan's §7.4 shape ─────────────────────────────────
    #: Extensions feed the next segment; a bad anchor poisons the whole chain.
    anchor_usable: bool = True
    audio_continuity_score: float = Field(default=1.0, ge=0.0, le=1.0)
    continuity_with_previous_score: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)

    verdict: Literal["accept", "edit", "regenerate", "human_review"]

    #: Provenance of this report — a heuristic judgement, not a measurement.
    critic_model: str = "unknown"
    critic_degraded: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# §7.5 ProviderCapabilities
# ─────────────────────────────────────────────────────────────────────────────


class ProviderCapabilities(BaseModel):
    """Per-surface capability record produced by `omni-vlog doctor`.

    §2.3: the two surfaces are NOT isomorphic. Nothing in the codebase may treat
    a capability as a global fact; it must read it from here.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    project: str | None
    model: str
    location: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    measured_generation_s: float | None = None
    measured_chain_s: float | None = None
    t2v: bool = False
    i2v: bool = False
    reference_to_video: bool = False
    first_last_frame: bool = False
    edit: bool = False
    extend: bool = False
    stateful_previous_interaction_id: bool = False
    stateful_steps_replay: bool = False
    max_generation_s: int = 10
    max_upload_edit_s: int | None = None
    max_upload_extend_s: int | None = None
    max_total_chain_s: int | None = None
    supported_resolutions: list[str] = Field(
        default_factory=lambda: ["360p", "720p", "1080p", "4k"]
    )
    uri_delivery: bool = False
    gcs_delivery: bool = False
    native_audio: bool = False
    async_polling: bool = False
    remote_retrieval: bool = False
    probed_at: str = Field(default_factory=utc_now_iso)
    notes: list[str] = Field(default_factory=list)

    def can_chain(self, seconds: int) -> tuple[bool, str]:
        """Can this surface natively chain to `seconds` total? (§8.3 strategy D)"""
        if self.max_total_chain_s is None:
            return False, "max_total_chain_s unknown — not probed"
        if seconds > self.max_total_chain_s:
            return False, (
                f"requested {seconds}s exceeds probed native chain limit {self.max_total_chain_s}s"
            )
        return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# §6.3 ReferenceAsset
# ─────────────────────────────────────────────────────────────────────────────


class ReferenceAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    path_or_uri: str
    role: ReferenceRole
    sha256: str
    provenance: Provenance
    approved: bool = False

    # ─ Sanitizer annotations (§6.2) ────────────────────────────────────────
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    staged_uri: str | None = None
    perceptual_hash: str | None = None
    sanitizer: SanitizerReport | None = None


class SanitizerReport(BaseModel):
    """Output of the reference sanitizer's vision pass (§6.2)."""

    model_config = ConfigDict(extra="forbid")

    has_text_overlay: bool = False
    has_timestamp: bool = False
    has_ui_controls: bool = False
    is_collage: bool = False
    multiple_distinct_people: bool = False
    is_clean_identity_reference: bool = True
    subject_is_adult: bool | None = None
    warnings: list[str] = Field(default_factory=list)

    #: Set when the pass could not run (no model access, unsupported file).
    #: A failed sanitizer must never silently read as "clean".
    degraded: bool = False
    degradation_reason: str | None = None


class ReferenceRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    reasons: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Render artifacts and interaction ledger
# ─────────────────────────────────────────────────────────────────────────────


class MediaInfo(BaseModel):
    """What we could determine about a media file. Absent ≠ zero: None means unknown.

    `error` is the probe's own verdict that the file is not a usable container.
    It is a separate field from the measurements because "we could not read this"
    and "we read it and it is 0 seconds long" are different facts, and only the
    first means the render should be thrown away.
    """

    model_config = ConfigDict(extra="forbid")

    container: str | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    has_audio: bool | None = None
    size_bytes: int | None = None
    probed_with: str | None = None
    c2pa_present: bool | None = None
    c2pa_detail: list[str] = Field(default_factory=list)
    #: Set when the file could not be parsed as a usable video container.
    error: str | None = None

    @property
    def is_usable(self) -> bool:
        """No parse error and a real, non-zero duration. What the pipeline gates on.

        `duration_s > 0` matters: a fragmented MP4 carries an `mvhd` whose duration
        field is 0 by specification, so a zero-length or empty render probes as
        `duration_s=0.0` with no error. Treating that as usable let an empty file be
        accepted as a segment artifact.
        """
        return self.error is None and self.duration_s is not None and self.duration_s > 0


class RenderArtifact(BaseModel):
    """One rendered video, plus how we got it.

    A `RenderArtifact` is never "the final film" — it is one node in the lineage.
    `parent_interaction_id` is what makes the chain auditable (§13.2).
    """

    model_config = ConfigDict(extra="forbid")

    interaction_id: str
    parent_interaction_id: str | None = None
    task: VideoTask
    model: str
    provider: str
    project: str | None = None
    #: Which planned segment this render belongs to. Explicit rather than derived
    #: from the on-disk path, because a repair replaces a segment's artifact and
    #: the chain has to know which one it is replacing.
    segment_index: int = -1

    status: Literal["completed", "failed", "cancelled", "incomplete", "in_progress", "unknown"]

    #: Where the bytes are. Exactly one of these is populated for a completed render.
    local_path: str | None = None
    gcs_uri: str | None = None

    prompt_sha256: str
    prompt: str

    aspect_ratio: AspectRatio | None = None
    resolution: Resolution | None = None
    requested_duration_s: int | None = None

    media: MediaInfo | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    completed_at: str | None = None
    latency_s: float | None = None
    estimated_cost_usd: Decimal | None = None

    #: Relative path under the job directory, e.g. "renders/segment_00/attempt_00_raw.mp4".
    artifact_relpath: str | None = None
    steps_path: str | None = None

    #: True when this artifact's bytes are a local ffmpeg transform of a provider
    #: output rather than the provider's own bytes (§22).
    derived: bool = False

    error_code: str | None = None
    error_message: str | None = None

    def is_usable_video(self) -> bool:
        return self.status == "completed" and bool(self.local_path or self.gcs_uri)


class InteractionRecord(BaseModel):
    """One row of the interaction ledger (§23 observability fields)."""

    model_config = ConfigDict(extra="forbid")

    interaction_id: str
    parent_interaction_id: str | None = None
    job_id: str
    segment_index: int
    attempt_index: int
    provider: str
    project: str | None
    model: str
    task: VideoTask | str
    #: Why the call was made — seed / extend / edit / regenerate. `task` records
    #: what the provider actually ran, which for a regenerated segment is just
    #: `extend` or `text_to_video` again. Without this, rebuilding the budget from
    #: a manifest could never reconstruct the regenerate and edit counters, so
    #: every crash-and-resume handed the job another round of paid repairs.
    call_kind: str | None = None
    resolution: str | None = None
    duration: str | None = None
    request_started_at: str
    request_completed_at: str | None = None
    latency_s: float | None = None
    status: str
    error_code: str | None = None
    error_message: str | None = None
    output_uri: str | None = None
    estimated_cost_usd: Decimal | None = None
    #: `unknown` when a request was dispatched but we never saw its result. §5.1:
    #: such a request must be resolved by querying, never by re-issuing.
    outcome_known: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Manifest (§13.2)
# ─────────────────────────────────────────────────────────────────────────────


class BudgetEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str = Field(default_factory=utc_now_iso)
    kind: Literal["authorize", "deny", "spend", "refund_unknown"]
    call_kind: str
    segment_index: int
    attempt_index: int
    video_seconds: int = 0
    estimated_cost_usd: Decimal = Decimal("0")
    reason: str | None = None


class Manifest(BaseModel):
    """The job's complete, replayable lineage (§13.2)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    job_id: str
    provider: str
    project: str | None
    model: str
    state: JobState
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    spec: ProjectSpec | None = None
    references: list[ReferenceAsset] = Field(default_factory=list)
    continuity_bible: ContinuityBible | None = None
    segment_plan: list[SegmentPlan] = Field(default_factory=list)

    interactions: list[InteractionRecord] = Field(default_factory=list)
    quality_reports: list[dict[str, Any]] = Field(default_factory=list)
    budget_events: list[BudgetEvent] = Field(default_factory=list)
    #: Running budget snapshot (§13.2 includes `budget` in the manifest example).
    #: Kept alongside `budget_events` because the events are the audit trail and
    #: this is the current position, and a reader usually wants the latter.
    budget: dict[str, Any] = Field(default_factory=dict)

    #: The **accepted chain**: at most one artifact per planned segment, ordered by
    #: segment index. Attempts that were superseded by a repair are not here; they
    #: live in `interactions` (the full ledger) and in the `artifacts` table, and
    #: their files stay in `renders/segment_NN/`. Keeping only the surviving
    #: artifact per segment is what makes `verify_chain_continuity` a meaningful
    #: check rather than a list of every render ever attempted.
    segments: list[RenderArtifact] = Field(default_factory=list)
    final_path: str | None = None
    final_derived: bool = False
    final_source_path: str | None = None
    final_source_sha256: str | None = None
    final_sha256: str | None = None

    c2pa_present: bool | None = None
    synthid_expected: bool | None = None

    #: Set when a chain was resumed against a different project than it started on.
    #: §5.1: only ever true with explicit human consent.
    degraded_cross_project_resume: bool = False

    capability_snapshot: dict[str, Any] | None = None
    state_history: list[dict[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def segment_artifacts(self) -> list[RenderArtifact]:
        """The usable chain, ordered by segment index."""
        return sorted(
            (a for a in self.segments if a.is_usable_video()),
            key=lambda a: a.segment_index,
        )

    def last_usable_artifact(self) -> RenderArtifact | None:
        usable = self.segment_artifacts()
        return usable[-1] if usable else None

    def artifact_for_segment(self, segment_index: int) -> RenderArtifact | None:
        usable = [a for a in self.segment_artifacts() if a.segment_index == segment_index]
        return usable[-1] if usable else None

    def upsert_segment(self, artifact: RenderArtifact) -> list[RenderArtifact]:
        """Place `artifact` as the current render for its segment.

        A repair replaces the segment's artifact rather than appending a second
        one. The superseded attempt keeps its place in `interactions` and on disk,
        so the history is not lost, but the chain stays one-artifact-per-segment.
        """
        index = artifact.segment_index
        if index < 0:
            # Unknown index: append rather than silently overwriting segment 0.
            return [*self.segments, artifact]

        kept = [a for a in self.segments if a.segment_index != index]
        return sorted([*kept, artifact], key=lambda a: a.segment_index)


ReferenceAsset.model_rebuild()
