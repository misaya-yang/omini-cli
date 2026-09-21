"""Regressions for defects found in code review.

Each test here corresponds to a specific bug that shipped, passed the whole suite,
and would have cost money or stranded a job in production. They exist so the same
defect cannot come back quietly.

Every test names the symptom, not just the fix, because the reason a case is
tested is usually more valuable than the assertion.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from tests.conftest import make_report

from omni_homevlog.agents.decision_policy import Decision, decide_segment
from omni_homevlog.budget import Budget, CallKind
from omni_homevlog.errors import InvalidRequestError, ServerError
from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.prompts.critic import parse_critic_response
from omni_homevlog.providers.transport import BaseTransport
from omni_homevlog.schemas import InteractionRecord, MediaInfo

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── `.env.example` must not break every command ────────────────────────────
#
# README's install step is `cp .env.example .env`. The template shipped with an
# empty `OMNI_MAX_ESTIMATED_COST_USD=`, which pydantic-settings parsed as a
# Decimal, so every command died with a raw ValidationError before doing anything.


def test_an_empty_env_value_falls_through_to_the_default() -> None:
    from omni_homevlog.config import Settings

    settings = Settings(omni_max_estimated_cost_usd=None)
    assert settings.omni_max_estimated_cost_usd is None


def test_the_shipped_env_template_loads_cleanly(tmp_path) -> None:
    """The exact file a new user copies must be usable as-is."""
    from omni_homevlog.config import Settings

    template = REPO_ROOT / ".env.example"
    assert template.is_file()

    settings = Settings(_env_file=template)
    assert settings.omni_provider in ("vertex", "gemini_api")


# ── 5xx must actually retry ────────────────────────────────────────────────
#
# The classification sat outside the try/except that drove the retry loop, so the
# loop was unreachable: a single transient 503 failed a paid render on the first
# attempt while the error still advertised `retryable=True`.


class FlakyTransport(BaseTransport):
    """Fails the first `failures` calls with 503, then succeeds."""

    def __init__(self, *, failures: int = 2) -> None:
        super().__init__(max_server_retries=2)
        self.failures = failures
        self.attempts = 0

    def create_url(self) -> str:
        return "http://example.invalid"

    def get_url(self, interaction_id: str) -> str:
        return f"http://example.invalid/{interaction_id}"

    def _auth_headers(self) -> dict[str, str]:
        return {}

    def describe(self) -> str:
        return "flaky"

    def _post(self, url: str, payload: dict, headers: dict):
        self.attempts += 1
        failing = self.attempts <= self.failures

        class Response:
            status_code = 503 if failing else 200
            text = (
                '{"error":{"message":"unavailable","code":"unavailable"}}'
                if failing
                else '{"id":"abc","status":"completed","steps":[]}'
            )

        return Response()

    def _get(self, url: str, headers: dict):  # pragma: no cover - unused here
        raise NotImplementedError


def test_a_transient_5xx_is_retried_and_then_succeeds() -> None:
    transport = FlakyTransport(failures=2)
    result = transport.create_interaction({"model": "m"})

    assert transport.attempts == 3, "the retry loop did not run"
    assert result.envelope.interaction_id == "abc"


def test_a_persistent_5xx_exhausts_the_budget_then_raises() -> None:
    transport = FlakyTransport(failures=99)

    with pytest.raises(ServerError):
        transport.create_interaction({"model": "m"})

    assert transport.attempts == 3, "should be 1 initial + max_server_retries"


def test_the_raised_5xx_error_is_still_marked_retryable() -> None:
    """The caller decides whether to try again; the flag must not lie."""
    transport = FlakyTransport(failures=99)
    with pytest.raises(ServerError) as excinfo:
        transport.create_interaction({"model": "m"})
    assert excinfo.value.retryable is True


# ── continuity scores must gate the chain ──────────────────────────────────
#
# Both continuity scores were omitted from the thresholds check, so an extension
# whose take reset the room and the outfit cleared every gate and was accepted.


def _budget() -> Budget:
    return Budget(max_total_calls=10, max_video_seconds_requested=120)


def test_a_spatial_reset_on_an_extension_is_not_accepted() -> None:
    report = make_report(continuity_with_previous_score=0.05)
    result = decide_segment(
        report=report,
        segment=None,
        budget=_budget(),
        segment_index=1,
        attempt_index=0,
        is_extension=True,
    )
    assert result.decision not in (Decision.ACCEPT, Decision.EXTEND)


def test_an_audio_discontinuity_is_not_accepted() -> None:
    report = make_report(audio_continuity_score=0.2)
    result = decide_segment(
        report=report,
        segment=None,
        budget=_budget(),
        segment_index=1,
        attempt_index=0,
        is_extension=True,
    )
    assert result.decision not in (Decision.ACCEPT, Decision.EXTEND)


def test_a_seed_is_not_judged_on_continuity() -> None:
    """A seed has no previous segment, so the score carries no information."""
    result = decide_segment(
        report=make_report(continuity_with_previous_score=0.0, audio_continuity_score=0.0),
        segment=None,
        budget=_budget(),
        segment_index=0,
        attempt_index=0,
        is_extension=False,
    )
    assert result.decision is Decision.ACCEPT


# ── a Critic reply we cannot parse must degrade, not crash ─────────────────
#
# `parse_critic_response` raised JSONDecodeError / pydantic ValidationError, but
# the caller guarded on InvalidRequestError. The degraded-report path was
# unreachable, so a chatty reply reached the user as a traceback and left the job
# stranded in SEED_REVIEW with every re-run crashing identically.


@pytest.mark.parametrize(
    "reply",
    [
        "I cannot analyze this image.",
        '{"identity_score": 0.9}',
        '{"identity_score": 0.9, "anatomy',
        "[1, 2, 3]",
        "",
        "```json\nnot json at all\n```",
    ],
    ids=["prose", "missing-fields", "truncated", "array", "empty", "fenced-garbage"],
)
def test_an_unparseable_critic_reply_raises_invalid_request(reply: str) -> None:
    with pytest.raises(InvalidRequestError):
        parse_critic_response(reply, critic_model="test")


def test_an_extra_key_in_the_reply_degrades_rather_than_crashing() -> None:
    """`extra="forbid"` means one unexpected key is enough to fail validation."""
    reply = json.dumps(
        {
            "identity_score": 0.9,
            "anatomy_score": 0.9,
            "motion_score": 0.9,
            "spatial_continuity_score": 0.9,
            "camera_realism_score": 0.9,
            "reference_fidelity_score": 0.9,
            "text_overlay_detected": False,
            "timestamp_detected": False,
            "ui_detected": False,
            "montage_detected": False,
            "surprise_field": 1,
            "verdict": "accept",
        }
    )
    with pytest.raises(InvalidRequestError):
        parse_critic_response(reply, critic_model="test")


# ─ the Critic must not be told it has media it does not have ──────────────
#
# The prompt claimed "the full video clip" and "the video's audio track" on every
# review, but only base64 stills are ever attached. That suppressed the prompt's
# own anti-fabrication note, so motion and audio scores were invented.


def test_the_critic_prompt_is_honest_about_stills() -> None:
    from omni_homevlog.prompts.critic import render_critic_prompt

    prompt = render_critic_prompt(
        segment=None,
        bible=None,
        is_extension=True,
        has_previous_frames=True,
        has_video=False,
        has_audio=False,
        frame_labels=["f000", "f025", "f050"],
    )
    lowered = prompt.lower()
    assert "still frames, not the video" in lowered
    assert "the full video clip" not in lowered
    assert "no audio was provided" in lowered


def test_the_critic_agent_never_claims_video_or_audio() -> None:
    """The flags describe what the model can perceive, not what exists on disk."""
    source = (REPO_ROOT / "src/omni_homevlog/agents/critic.py").read_text()
    assert "has_video=False" in source
    assert "has_audio=False" in source
    assert "has_video=inputs.has_video()" not in source


# ── a Critic's prose must not reach a paid call unvalidated ────────────────
#
# §24.8. The orchestrator handed `suggested_edit_prompt` straight to `provider.edit`
# when no canned defect hint matched, so a reply containing a timecode or a cut
# instruction was dispatched verbatim.


def _compiler():
    from omni_homevlog.prompts.compiler import PromptCompiler
    from omni_homevlog.schemas import ContinuityBible

    return PromptCompiler(bible=ContinuityBible(subject_identity="a woman"))


def test_a_suggestion_with_a_timecode_is_refused() -> None:
    compiled = _compiler().compile_edit_from_suggestion(
        suggestion="remove the flicker at 00:07 and cut to a closer framing",
        segment_index=0,
    )
    assert compiled is None


def test_a_suggestion_asking_for_a_restage_is_refused() -> None:
    compiled = _compiler().compile_edit_from_suggestion(
        suggestion="restage the scene in a different room", segment_index=0
    )
    assert compiled is None


def test_a_plain_local_suggestion_is_accepted_and_wrapped() -> None:
    compiled = _compiler().compile_edit_from_suggestion(
        suggestion="remove the timestamp overlay from the corner of the frame",
        segment_index=0,
    )
    assert compiled is not None
    assert "Preserve the entire video exactly as it is" in compiled.text


def test_an_empty_suggestion_is_refused() -> None:
    assert _compiler().compile_edit_from_suggestion(suggestion="   ", segment_index=0) is None


# ── a zero-duration file is not a render ───────────────────────────────────
#
# A fragmented MP4 carries an mvhd whose duration is 0 by specification, so an
# empty render probed as duration 0.0 with no error and was accepted as the
# segment artifact; a later tail-frame extraction then divided by it.


def test_a_zero_duration_probe_is_not_usable() -> None:
    assert not MediaInfo(duration_s=0.0, error=None).is_usable
    assert MediaInfo(duration_s=3.0).is_usable


def test_tail_frame_extraction_survives_a_zero_duration(tmp_path) -> None:
    from omni_homevlog.media.extract_frames import extract_tail_frames

    path = tmp_path / "empty.mp4"
    from tests.conftest import build_minimal_mp4

    path.write_bytes(build_minimal_mp4(duration_s=0.0))

    frames = extract_tail_frames(path, tmp_path / "out")
    assert frames.degradation_reason is not None


def test_a_tiny_non_video_file_is_reported_as_unusable(tmp_path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a container" * 100)
    info = inspect_media(junk, prefer_ffprobe=False)
    assert not info.is_usable
    assert info.error


# ── the ledger must reconstruct repair ceilings ────────────────────────────
#
# `task` says what the provider ran, not why we called it, so a regeneration
# counted as a seed or an extension. Every per-segment repair ceiling then read
# zero after a resume, and each restart handed the job another paid repair.


def _record(call_kind: str | None, task: str, segment_index: int = 1) -> InteractionRecord:
    return InteractionRecord(
        interaction_id=f"int-{call_kind}-{task}",
        job_id="job",
        segment_index=segment_index,
        attempt_index=0,
        provider="vertex",
        project="p",
        model="m",
        task=task,
        call_kind=call_kind,
        request_started_at="2026-09-21T00:00:00Z",
        status="completed",
    )


def test_a_regeneration_is_reconstructed_as_a_regeneration() -> None:
    from omni_homevlog.pipeline.context import _call_kind_of

    assert _call_kind_of(_record("regenerate", "extend")) is CallKind.REGENERATE
    assert _call_kind_of(_record("edit", "edit")) is CallKind.EDIT
    assert _call_kind_of(_record("seed", "reference_to_video", 0)) is CallKind.SEED


def test_a_row_without_a_call_kind_falls_back_to_the_task() -> None:
    """Rows written before the field existed must still be readable."""
    from omni_homevlog.pipeline.context import _call_kind_of

    assert _call_kind_of(_record(None, "text_to_video", 0)) is CallKind.SEED
    assert _call_kind_of(_record(None, "extend")) is CallKind.EXTEND


def test_rebuilding_a_budget_restores_the_repair_ceiling() -> None:
    """The regression: one seed plus one regeneration used to count as two seeds."""
    from omni_homevlog.pipeline.context import budget_from_manifest
    from omni_homevlog.schemas import Manifest, ProjectSpec

    spec = ProjectSpec(title="t", brief="b", provider="vertex", project="p", model="m")
    manifest = Manifest(
        job_id="job",
        provider="vertex",
        project="p",
        model="m",
        state="SEED_ACCEPTED",
        spec=spec,
        interactions=[
            _record("seed", "reference_to_video", 0),
            _record("regenerate", "text_to_video", 0),
        ],
    )

    budget = budget_from_manifest(manifest, spec)
    assert budget.count(CallKind.REGENERATE, 0) == 1
    assert budget.count(CallKind.SEED, 0) == 1


# ── the continuation rule stays in force ───────────────────────────────────


def test_a_vertex_continuation_omits_generation_config() -> None:
    from omni_homevlog.providers.base import RenderRequest
    from omni_homevlog.providers.vertex_enterprise import VertexEnterpriseProvider

    provider = VertexEnterpriseProvider(project="p", session=object())
    payload = provider.build_payload(
        RenderRequest(
            task="extend",
            prompt="continue",
            segment_index=1,
            attempt_index=0,
            aspect_ratio="16:9",
            resolution="360p",
            duration_s=3,
            parent_interaction_id="int-seed",
        )
    )
    assert "generation_config" not in payload
    assert payload["previous_interaction_id"] == "int-seed"
    assert payload["response_format"][0]["type"] == "video"


def test_a_vertex_chain_does_not_require_a_bucket() -> None:
    """Strategy A carries the continuation, so no video input is needed.

    Requiring one made every 20/30/40-second job fail on its first extension under
    the shipped default configuration.
    """
    from omni_homevlog.providers.vertex_enterprise import VertexEnterpriseProvider

    provider = VertexEnterpriseProvider(project="p", session=object(), gcs=None, gcs_prefix=None)
    assert provider.needs_video_input() is False


# ── a budget reservation for an edit must not be free ──────────────────────


def _tight_seconds_budget() -> Budget:
    """Allows 5 video-seconds, so a 10-second reservation must be refused."""
    return Budget(
        max_total_calls=10,
        max_video_seconds_requested=5,
        max_edit_attempts_per_segment=1,
        max_regenerations_per_segment=1,
    )


def test_the_edit_preflight_asks_about_the_real_duration() -> None:
    """Behavioural, not textual.

    Reservations used to pass `video_seconds=0`, so a check against a budget with
    almost no seconds left returned "ok" and an edit was authorised that the real
    call could not afford. A textual assertion would have matched the comment
    explaining the fix, so this drives the policy instead.
    """
    report = make_report(
        anatomy_score=0.70,
        editable_defects=["one hand has six fingers"],
        suggested_edit_prompt="correct the hand so it has five natural fingers",
        verdict="edit",
    )
    result = decide_segment(
        report=report,
        segment=None,
        budget=_tight_seconds_budget(),
        segment_index=0,
        attempt_index=0,
        is_extension=False,
    )
    assert result.decision is not Decision.EDIT, (
        "an edit was authorised by a budget that cannot afford the render"
    )


def test_an_edit_reserves_the_seconds_it_will_render() -> None:
    """`run_edit` must hand the budget a real duration, not zero."""

    budget = _tight_seconds_budget()
    allowed, why = budget.check(CallKind.EDIT, segment_index=0, video_seconds=10)
    assert not allowed, why

    # And with the zero the old code passed, the same budget says yes:
    allowed_with_zero, _ = budget.check(CallKind.EDIT, segment_index=0, video_seconds=0)
    assert allowed_with_zero, "the premise of this test no longer holds"


def test_the_cost_estimator_answers_zero_for_zero_seconds() -> None:
    """Zero is the estimator's own correct answer, not a reason to skip it."""
    from omni_homevlog.costing import estimate_video_seconds_cost

    assert estimate_video_seconds_cost(model="m", video_seconds=0) == Decimal("0")
    assert estimate_video_seconds_cost(model="m", video_seconds=10) >= Decimal("0")


# ── a repair must not be able to extend a segment onto itself ──────────────


def test_the_finalizer_is_reachable_after_a_critic_rejection() -> None:
    """A chain whose head was edited must still verify.

    `verify_chain_continuity` used to reject any chain starting with an edit, so a
    locally repaired seed completed on disk yet the job could never reach COMPLETE.
    """
    source = (REPO_ROOT / "src/omni_homevlog/pipeline/extend.py").read_text()
    assert "the chain starts with an edit, which cannot be a seed" not in source
    assert "names no parent" in source


# ── every provider must satisfy the protocol the probe relies on ───────────


def test_the_provider_protocol_declares_build_payload() -> None:
    """`probe_generation` builds its request through the provider.

    A provider satisfying the protocol but lacking `build_payload` would probe one
    request shape and run another, which is worse than not probing.
    """
    from omni_homevlog.providers.base import VideoProvider

    assert hasattr(VideoProvider, "build_payload")
    source = (REPO_ROOT / "src/omni_homevlog/providers/base.py").read_text()
    protocol_block = source.split("class VideoProvider(Protocol)", 1)[1].split("\nclass ", 1)[0]
    assert re.search(r"def build_payload", protocol_block)
