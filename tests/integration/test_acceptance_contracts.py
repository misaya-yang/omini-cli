"""Regression contracts for production readiness; no remote calls."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from omni_homevlog.budget import CallKind
from omni_homevlog.cli import app
from omni_homevlog.errors import BudgetExhaustedError, OmniVlogError
from omni_homevlog.pipeline.context import JobContext, budget_from_manifest
from omni_homevlog.pipeline.finalize import export
from omni_homevlog.pipeline.orchestrator import run_job
from omni_homevlog.schemas import JobState


def test_reserved_budget_survives_crash_before_ledger(build_job):
    ctx, _, _ = build_job()
    ctx.authorize(CallKind.SEED, segment_index=0, attempt_index=0, video_seconds=3)
    assert not ctx.manifest.interactions
    restored = budget_from_manifest(ctx.store.load(), ctx.spec)
    assert restored.calls_made == 1
    assert restored.video_seconds_requested == 3


def test_job_dollar_ceiling_is_pinned(build_job, spec, monkeypatch):
    ctx, _, _ = build_job(
        spec_override=spec.model_copy(update={"max_estimated_cost_usd": Decimal("1.25")})
    )
    monkeypatch.setenv("OMNI_MAX_ESTIMATED_COST_USD", "999")
    restored = JobContext.load(ctx.job_id)
    assert restored.budget.max_estimated_cost_usd == Decimal("1.25")
    with pytest.raises(BudgetExhaustedError, match="pricing is unknown"):
        ctx.authorize(CallKind.SEED, segment_index=0, attempt_index=0, video_seconds=3)


def test_failed_old_review_does_not_veto_repaired_chain(build_job):
    ctx, _, _ = build_job()
    run_job(ctx)
    bad = dict(ctx.manifest.quality_reports[0])
    bad["interaction_id"] = "superseded-attempt"
    bad["report"] = {**bad["report"], "identity_score": 0, "severe_defects": ["wrong face"]}
    ctx.manifest = ctx.store.mutate(quality_reports=[bad, *ctx.manifest.quality_reports])
    from omni_homevlog.pipeline.review import review_final

    final = review_final(ctx)
    assert final["segment_count"] == 3
    assert final["decision"]["decision"] != "HUMAN_REVIEW"


def test_export_refusal_leaves_existing_destination_untouched(build_job, tmp_path):
    ctx, _, _ = build_job()
    target = tmp_path / "export.mp4"
    target.write_bytes(b"keep-me")
    with pytest.raises(OmniVlogError, match="COMPLETE"):
        export(ctx, output=target)
    assert target.read_bytes() == b"keep-me"
    run_job(ctx)
    ctx.manifest = ctx.store.mutate(final_derived=True)
    with pytest.raises(OmniVlogError, match="Derived"):
        export(ctx, output=target, allow_derived=False)
    assert target.read_bytes() == b"keep-me"


def test_cli_status_approve_resume_export(build_job, spec, monkeypatch, tmp_path):
    ctx, provider, _ = build_job(spec_override=spec.model_copy(update={"human_gates": ["final"]}))
    run_job(ctx)
    assert ctx.manifest.state == JobState.FINAL_REVIEW
    calls = len(provider.calls)
    monkeypatch.setattr("omni_homevlog.cli._load_context", lambda *a, **k: ctx)
    runner = CliRunner()
    result = runner.invoke(app, ["status", ctx.job_id, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["chain_seconds"] == 30
    result = runner.invoke(app, ["approve", ctx.job_id, "--stage", "final"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["resume", ctx.job_id])
    assert result.exit_code == 0, result.output
    assert ctx.store.load().state == JobState.COMPLETE
    target = tmp_path / "film.mp4"
    result = runner.invoke(app, ["export", ctx.job_id, "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == Path(ctx.manifest.final_path).read_bytes()
    assert json.loads(target.with_suffix(".manifest.json").read_text())["state"] == "COMPLETE"
    assert len(provider.calls) == calls


def test_short_cumulative_output_cannot_complete(build_job):
    ctx, _, _ = build_job()
    run_job(ctx)
    from omni_homevlog.pipeline.extend import verify_chain_continuity

    ctx.manifest.segments[-1].media.duration_s = 10
    ok, problems = verify_chain_continuity(ctx)
    assert not ok
    assert any("expected 30s" in p for p in problems)


def test_unknown_dispatch_blocks_run_not_only_resume(build_job):
    ctx, provider, _ = build_job()
    from omni_homevlog.pipeline.render_seed import record_dispatch_pending

    record_dispatch_pending(ctx, segment_index=0, attempt_index=0, task="text_to_video")
    report = run_job(ctx)
    assert "pending" in report.stopped_because
    assert provider.calls == []


def test_recovery_preserves_parent_id_and_downloaded_bytes(build_job):
    ctx, provider, _ = build_job()
    run_job(ctx)
    artifact = ctx.manifest.segments[-1]
    record = ctx.manifest.interactions[-1].model_copy(
        update={"interaction_id": "pending-crash", "outcome_known": False}
    )
    ctx.manifest = ctx.store.mutate(
        state=JobState.NEEDS_HUMAN,
        interactions=[*ctx.manifest.interactions[:-1], record],
        segments=ctx.manifest.segments[:-1],
    )

    async def query(interaction_id):
        return artifact.model_copy(update={"parent_interaction_id": None})

    provider.get_interaction = query
    from omni_homevlog.pipeline.resume import resolve_unknown_interaction

    recovered = resolve_unknown_interaction(ctx, record, interaction_id=artifact.interaction_id)
    assert recovered.parent_interaction_id == record.parent_interaction_id
    assert recovered.media.duration_s == 30
    assert all(r.outcome_known for r in ctx.store.load().interactions)
    assert ctx.store.load().interactions[-1].interaction_id == artifact.interaction_id


def test_critic_attaches_video_bytes_and_marks_missing_video(build_job, tmp_path):
    from tests.conftest import build_minimal_mp4, make_report

    from omni_homevlog.agents.critic import Critic, CriticInputs
    from omni_homevlog.agents.llm import LLMResponse
    from omni_homevlog.media.extract_frames import ExtractedFrame, FrameSet

    video = tmp_path / "video.mp4"
    video.write_bytes(build_minimal_mp4(duration_s=3))
    client = Mock()
    client.generate.return_value = LLMResponse(text=make_report().model_dump_json(), model="test")
    critic = Critic(client=client)
    frames = FrameSet(
        complete=True,
        frames=[
            ExtractedFrame(
                path=tmp_path / f"missing-{i}.jpg", at_s=i, fraction=i / 3, label=f"frame-{i}"
            )
            for i in range(3)
        ],
    )
    inputs = CriticInputs(
        segment=None, bible=None, is_extension=False, video_path=video, frame_set=frames
    )
    critic.review(inputs)
    media = client.generate.call_args.kwargs["images"]
    assert any(m.mime_type == "video/mp4" and m.data_b64 for m in media)
    video.unlink()
    result = critic.review(inputs)
    assert result.report.critic_degraded
    assert not any(m.mime_type == "video/mp4" for m in client.generate.call_args.kwargs["images"])


def test_concept_mode_never_spends_on_extensions(build_job, spec, bible):
    from tests.integration.conftest import FakeDirector

    from omni_homevlog.agents.director import default_plan_for_spec

    concept = spec.model_copy(update={"mode": "concept", "concept_duration_s": 4})
    plan = default_plan_for_spec(concept, bible)
    ctx, provider, _ = build_job(spec_override=concept, director=FakeDirector(plan))
    result = run_job(ctx)
    assert result.final_state == JobState.COMPLETE
    assert len(provider.calls) == 1
    assert ctx.manifest.segments[0].media.duration_s == 4


def test_cli_retry_runs_selected_edit_then_reviews(build_job, monkeypatch):
    ctx, provider, _ = build_job()
    run_job(ctx)
    # A real stopped chain with its final segment needing repair.
    ctx.manifest = ctx.store.mutate(state=JobState.NEEDS_HUMAN, final_path=None)
    monkeypatch.setattr("omni_homevlog.cli._load_context", lambda *a, **k: ctx)
    result = CliRunner().invoke(
        app,
        [
            "retry",
            ctx.job_id,
            "--stage",
            "segment-2",
            "--mode",
            "edit",
            "--edit-prompt",
            "remove the timestamp overlay",
        ],
    )
    assert result.exit_code == 0, result.output
    assert provider.calls[-1]["kind"] == "edit"
    assert ctx.store.load().state == JobState.COMPLETE


def test_text_model_call_limit_survives_reconstruction(tmp_path):
    from omni_homevlog.agents.llm import TextModelClient

    ledger = tmp_path / "llm.json"
    first = TextModelClient(model="test", provider="gemini_api", ledger_path=ledger, max_calls=1)
    first._reserve_call()
    second = TextModelClient(model="test", provider="gemini_api", ledger_path=ledger, max_calls=1)
    with pytest.raises(BudgetExhaustedError):
        second._reserve_call()
    assert json.loads(ledger.read_text())[0]["status"] == "dispatched"


def test_each_segment_gate_is_approvable_and_does_not_repeat_seed(build_job, spec):
    from omni_homevlog.pipeline.resume import approve_gate
    ctx, provider, _ = build_job(spec_override=spec.model_copy(update={"human_gates": ["each-segment"]}))
    run_job(ctx)
    assert ctx.store.load().state == JobState.NEEDS_HUMAN and provider.calls == []
    for index in range(3):
        approve_gate(ctx, stage=f"segment-{index}")
        run_job(ctx)
        assert len(provider.calls) == index + 1
    assert ctx.store.load().state == JobState.COMPLETE


def test_high_resolution_gate_precedes_paid_seed(build_job, spec):
    from omni_homevlog.pipeline.resume import approve_gate
    ctx, provider, _ = build_job(spec_override=spec.model_copy(update={"resolution": "1080p", "target_duration_s":10, "human_gates":["high-res"]}))
    run_job(ctx)
    assert ctx.store.load().state == JobState.NEEDS_HUMAN
    assert provider.calls == []
    with pytest.raises(OmniVlogError):
        approve_gate(ctx, stage="final")
    approve_gate(ctx, stage="high-res")
    run_job(ctx)
    assert len(provider.calls) == 1
