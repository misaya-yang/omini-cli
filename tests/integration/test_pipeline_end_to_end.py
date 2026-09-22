"""End-to-end pipeline tests with a mocked provider (§21.2).

The 30-second chain is the thing this project exists to produce, so these tests
assert on the properties §18 calls out: the result is a single parent-linked
native chain, not three independent renders spliced together.
"""

from __future__ import annotations

import json

import pytest
from tests.integration.conftest import passing_report

from omni_homevlog.pipeline.extend import verify_chain_continuity
from omni_homevlog.pipeline.orchestrator import Orchestrator, run_job
from omni_homevlog.schemas import JobState
from omni_homevlog.storage.local import read_json

pytestmark = pytest.mark.usefixtures("_no_real_network")


# ── the happy path ─────────────────────────────────────────────────────────


def test_thirty_second_chain_completes(build_job) -> None:
    ctx, _provider, _ = build_job()

    report = run_job(ctx)

    # The plan's default gate is `high-res`, which gates the finalization step, so
    # the chain runs to FINAL_REVIEW and stops there.
    assert report.final_state in (JobState.FINAL_REVIEW, JobState.COMPLETE), report.summary()

    artifacts = ctx.store.load().segment_artifacts()
    assert len(artifacts) == 3, f"expected 3 segments, got {len(artifacts)}"
    assert [str(a.task) for a in artifacts] == [
        "reference_to_video",
        "extend",
        "extend",
    ]


def test_the_chain_is_parent_linked_not_three_independent_renders(build_job) -> None:
    """§18's headline exit criterion."""
    ctx, _provider, _ = build_job()
    run_job(ctx)

    artifacts = ctx.store.load().segment_artifacts()
    assert len(artifacts) == 3

    ok, problems = verify_chain_continuity(ctx)
    assert ok, problems

    assert artifacts[0].parent_interaction_id is None
    assert artifacts[1].parent_interaction_id == artifacts[0].interaction_id
    assert artifacts[2].parent_interaction_id == artifacts[1].interaction_id


def test_each_render_is_saved_on_disk(build_job) -> None:
    ctx, _provider, _ = build_job()
    run_job(ctx)

    for artifact in ctx.store.load().segment_artifacts():
        assert artifact.local_path is not None
        assert artifact.media is not None
        assert artifact.media.duration_s is not None


def test_manifest_reconstructs_the_whole_lineage(build_job) -> None:
    ctx, _provider, _ = build_job()
    run_job(ctx)

    ctx.reload()
    manifest = ctx.store.load()

    assert len(manifest.interactions) == 3
    assert all(record.outcome_known for record in manifest.interactions)
    assert len(manifest.quality_reports) == 3
    assert manifest.continuity_bible is not None
    assert len(manifest.segment_plan) == 3
    assert manifest.state_history, "no state history was recorded"

    # Every state transition is also in the database index.
    assert ctx.db.get_job(manifest.job_id) is not None


def test_prompts_are_recorded_per_attempt(build_job) -> None:
    ctx, provider, _ = build_job()
    run_job(ctx)

    prompts = [call["prompt"] for call in provider.calls if call["kind"] != "get_interaction"]
    assert len(prompts) == 3
    assert "[# References" in prompts[0], "the seed prompt lost its reference header"
    for prompt in prompts:
        assert "no on-screen text" in prompt.lower()


def test_a_ten_second_job_stops_after_one_segment(build_job, spec) -> None:
    short = spec.model_copy(update={"target_duration_s": 10, "title": "ten seconds"})
    ctx, provider, _ = build_job(spec_override=short)
    run_job(ctx)

    artifacts = ctx.store.load().segment_artifacts()
    assert len(artifacts) == 1
    assert len([c for c in provider.calls if c["kind"] != "get_interaction"]) == 1


def test_a_twenty_second_job_runs_one_extension(build_job, spec) -> None:
    twenty = spec.model_copy(update={"target_duration_s": 20, "title": "twenty"})
    ctx, _provider, _ = build_job(spec_override=twenty)
    run_job(ctx)

    artifacts = ctx.store.load().segment_artifacts()
    assert len(artifacts) == 2
    assert [str(a.task) for a in artifacts] == ["reference_to_video", "extend"]


def test_a_forty_second_job_runs_three_extensions(build_job, spec, bible) -> None:
    from omni_homevlog.schemas import DirectorPlan, SegmentPlan

    forty = spec.model_copy(update={"target_duration_s": 40, "title": "forty"})
    segments = [
        SegmentPlan(
            index=i,
            intended_duration_s=10,
            start_state=f"state {i}",
            action=f"she does the thing for part {i}",
            camera_behavior="handheld, no cut",
            environment="the apartment",
            emotional_beat="warm",
            audio_intent="room tone",
            end_state=f"end state {i}",
            continuation_anchor=f"pose {i} held toward the lens",
        )
        for i in range(4)
    ]
    plan = DirectorPlan(title="forty", logline="x", continuity_bible=bible, segments=segments)
    from tests.integration.conftest import FakeDirector

    ctx, _provider, _ = build_job(spec_override=forty, director=FakeDirector(plan))
    run_job(ctx)

    artifacts = ctx.store.load().segment_artifacts()
    assert len(artifacts) == 4
    ok, problems = verify_chain_continuity(ctx)
    assert ok, problems


# ── review and repair ──────────────────────────────────────────────────────


def test_a_failing_segment_is_regenerated_then_accepted(build_job) -> None:
    """The repair loop: bad render -> regenerate -> good render -> extend."""
    ctx, _provider, _ = build_job(
        reports=[
            passing_report(identity_score=0.25, verdict="regenerate"),
            passing_report(),
            passing_report(),
            passing_report(),
        ]
    )

    report = run_job(ctx)
    assert report.final_state not in (JobState.FAILED_FINAL, JobState.NEEDS_HUMAN), report.summary()

    manifest = ctx.store.load()
    artifacts = manifest.segment_artifacts()

    # The chain still has exactly three links: a repair *replaces* its segment's
    # artifact rather than appending a fourth.
    assert len(artifacts) == 3
    assert [a.segment_index for a in artifacts] == [0, 1, 2]

    # But the ledger remembers every call, so the superseded attempt is not lost.
    seed_renders = [
        r for r in manifest.interactions if r.segment_index == 0 and str(r.task) != "extend"
    ]
    assert len(seed_renders) >= 2, "the superseded seed render is missing from the ledger"

    ok, problems = verify_chain_continuity(ctx)
    assert ok, problems


def test_repeated_regeneration_stops_and_asks_a_human(build_job) -> None:
    """The loop bound: a segment that never clears review must not spin."""
    ctx, _provider, _ = build_job(
        reports=[passing_report(identity_score=0.20, verdict="regenerate")] * 10
    )

    report = run_job(ctx)
    assert report.final_state is JobState.NEEDS_HUMAN
    assert "regenerated" in " ".join(ctx.store.load().notes)


def test_a_critics_own_human_review_verdict_does_not_stop_a_passing_report(
    build_job,
) -> None:
    """The policy, not the Critic, decides. §24.8.

    A report whose scores all clear their thresholds is accepted even when the
    Critic wrote "human_review", because the verdict is advisory. The disagreement
    is recorded rather than obeyed, and the job runs to completion.
    """
    ctx, _provider, _ = build_job(reports=[passing_report(verdict="human_review")])
    report = run_job(ctx)

    assert report.final_state is JobState.COMPLETE
    stored = ctx.store.load().quality_reports
    assert stored[0]["decision"]["critic_verdict_agrees"] is False


def test_a_blocking_report_stops_the_run(build_job) -> None:
    """What actually stops a run is evidence the policy cannot act on.

    A degraded review is the clearest case: the Critic could not see enough to
    judge, so neither its verdict nor its scores can authorise another call.
    """
    ctx, _provider, _ = build_job(reports=[passing_report(critic_degraded=True, verdict="accept")])
    report = run_job(ctx)
    assert report.final_state is JobState.NEEDS_HUMAN


def test_a_timestamp_hard_reject_forces_regeneration(build_job) -> None:
    ctx, provider, _ = build_job(
        reports=[
            passing_report(timestamp_detected=True, verdict="regenerate"),
            passing_report(),
            passing_report(),
            passing_report(),
        ]
    )
    run_job(ctx)

    # The first render must not have become the accepted seed.
    seed_attempts = [c for c in provider.calls if c["kind"] == "reference_to_video"]
    assert len(seed_attempts) >= 2, "a timestamped seed was accepted without a re-render"


def test_a_local_defect_is_edited_rather_than_regenerated(build_job) -> None:
    ctx, provider, _ = build_job(
        reports=[
            passing_report(
                anatomy_score=0.70,
                editable_defects=["the left hand has six fingers"],
                suggested_edit_prompt="correct the left hand to five natural fingers",
                verdict="edit",
            ),
            passing_report(),
            passing_report(),
            passing_report(),
        ]
    )
    run_job(ctx)

    edits = [c for c in provider.calls if c["kind"] == "edit"]
    assert edits, "an editable defect did not produce an edit call"


def test_a_degraded_review_never_authorises_an_extension(build_job) -> None:
    ctx, provider, _ = build_job(
        reports=[passing_report(critic_degraded=True, verdict="accept")] * 5
    )
    report = run_job(ctx)

    assert report.final_state is JobState.NEEDS_HUMAN
    # Only the seed was rendered; no extension was paid for on thin evidence.
    assert len([c for c in provider.calls if c["kind"] == "extend"]) == 0


# ── budget ─────────────────────────────────────────────────────────────────


def test_budget_ceiling_stops_the_chain(build_job, spec) -> None:
    tight = spec.model_copy(update={"max_total_calls": 2, "title": "tight"})
    ctx, provider, _ = build_job(spec_override=tight)

    report = run_job(ctx)

    assert len([c for c in provider.calls if c["kind"] != "get_interaction"]) <= 2
    assert report.final_state in (JobState.BUDGET_EXHAUSTED, JobState.NEEDS_HUMAN), report.summary()


def test_budget_events_are_recorded(build_job) -> None:
    ctx, _provider, _ = build_job()
    run_job(ctx)

    events = ctx.db.budget_events_for_job(ctx.job_id)
    assert events, "no budget events were persisted"
    assert all("call_kind" in event for event in events)


def test_budget_is_reconstructed_on_resume_not_reset(build_job) -> None:
    """A resumed job must not be handed a second full budget."""
    ctx, _provider, _ = build_job()
    run_job(ctx)
    spent = ctx.budget.calls_made
    assert spent > 0

    from omni_homevlog.pipeline.context import JobContext

    reloaded = JobContext.load(ctx.job_id)
    assert reloaded.budget.calls_made == spent
    assert reloaded.budget.remaining_calls() == ctx.budget.remaining_calls()


# ─ human gates ────────────────────────────────────────────────────────────


def test_the_final_gate_holds_the_job_at_final_review(build_job, spec) -> None:
    gated = spec.model_copy(update={"human_gates": ["high-res", "final"]})
    ctx, _provider, _ = build_job(spec_override=gated)
    report = run_job(ctx)

    assert report.final_state is JobState.FINAL_REVIEW
    assert "approve" in " ".join(ctx.store.load().notes)


def test_approving_the_gate_lets_the_job_complete(build_job, spec) -> None:
    gated = spec.model_copy(update={"human_gates": ["high-res", "final"]})
    ctx, _provider, _ = build_job(spec_override=gated)
    run_job(ctx)
    assert ctx.store.load().state is JobState.FINAL_REVIEW

    ctx.manifest = ctx.store.mutate(
        state_history=[
            *ctx.manifest.state_history,
            {
                "from": "FINAL_REVIEW",
                "to": "FINAL_REVIEW",
                "at": "2026-09-21T12:00:00Z",
                "note": "approved:final",
            },
        ]
    )

    second = Orchestrator(ctx).run()
    assert second.final_state is JobState.COMPLETE
    assert ctx.store.load().final_path is not None


def test_no_gates_means_the_job_completes_unattended(build_job, spec) -> None:
    ungated = spec.model_copy(update={"human_gates": []})
    ctx, _provider, _ = build_job(spec_override=ungated)
    report = run_job(ctx)
    assert report.final_state is JobState.COMPLETE


# ── artifacts on disk ──────────────────────────────────────────────────────


def test_plan_artifacts_are_written(build_job) -> None:
    ctx, _provider, _ = build_job()
    run_job(ctx)

    assert ctx.paths.project_spec_path.is_file()
    assert ctx.paths.continuity_bible_path.is_file()
    assert ctx.paths.segment_plan_path.is_file()

    spec_payload = read_json(ctx.paths.project_spec_path)
    assert spec_payload["target_duration_s"] == 30
    assert len(read_json(ctx.paths.segment_plan_path)) == 3


def test_review_reports_are_written_per_segment(build_job) -> None:
    ctx, _provider, _ = build_job()
    run_job(ctx)

    reviews = sorted(ctx.paths.reviews_dir.glob("segment_*.json"))
    assert len(reviews) == 3
    first = json.loads(reviews[0].read_text())
    assert "report" in first
    assert "decision" in first
    assert "thresholds_used" in first


def test_manifest_is_valid_json_and_schema_conformant(build_job) -> None:
    from omni_homevlog.schemas import Manifest

    ctx, _provider, _ = build_job()
    run_job(ctx)

    raw = json.loads(ctx.paths.manifest_path.read_text())
    parsed = Manifest.model_validate(raw)
    assert parsed.job_id == ctx.job_id
    assert parsed.spec is not None


def test_debug_fixtures_are_written_and_redacted(build_job) -> None:
    ctx, _provider, _ = build_job()
    run_job(ctx)

    # The fake provider does not go through the transport, so there may be no
    # fixtures; what matters is that none of them contains a credential if there are.
    for fixture in ctx.paths.debug_dir.glob("*.json"):
        text = fixture.read_text()
        assert "AIza" not in text
        assert "private_key" not in text


def test_status_summary_is_serialisable(build_job) -> None:
    from omni_homevlog.pipeline.finalize import summary

    ctx, _provider, _ = build_job()
    run_job(ctx)

    data = summary(ctx)
    json.dumps(data, default=str)
    assert data["segments_rendered"] == 3
    # Native extension returns the complete film: 10s -> 20s -> 30s.
    # Summing these artifacts would incorrectly report a 60-second final film.
    assert [a.media.duration_s for a in ctx.manifest.segment_artifacts()] == [10, 20, 30]
    assert data["chain_seconds"] == 30
    from omni_homevlog.media.ffprobe import inspect_media

    assert inspect_media(ctx.paths.final_video_path).duration_s == 30
    assert ctx.budget.video_seconds_requested == 30
    assert data["unresolved_interactions"] == []


def test_metrics_collect_without_error(build_job) -> None:
    from omni_homevlog.observability.metrics import collect_job_metrics

    ctx, _provider, _ = build_job()
    run_job(ctx)

    metrics = collect_job_metrics(ctx.store.load(), db=ctx.db)
    assert metrics.interactions == 3
    assert metrics.segments_rendered == 3
    assert metrics.chain_completion == 1.0
    json.dumps(metrics.as_dict())
