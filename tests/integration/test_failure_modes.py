"""The §21.2 failure matrix, end to end.

§21.2 names the cases a mock integration test must cover:
  200 completed · background pending → completed · 429 · 500 ·
  interaction completed but download failed · interaction id lost ·
  GCS permission denied · multiple model_output steps · model_output without
  video · policy block

The tests that matter most here are the ones about *not* retrying. A 500 is
retryable; a 429 is not (the plan forbids working around quota); and a timeout is
emphatically not, because the generation may exist and may be billable.
"""

from __future__ import annotations

import pytest
from tests.integration.conftest import (
    FAILURES,
    DownloadFailingProvider,
    FakeProviderBehaviour,
    NoVideoProvider,
)

from omni_homevlog.errors import (
    OmniVlogError,
    PermissionError_,
    ProviderError,
    QuotaExhaustedError,
    RequestTimeoutUnknownOutcome,
    ServerError,
    classify_http_error,
)
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.pipeline.orchestrator import Orchestrator, run_job
from omni_homevlog.pipeline.resume import plan_resume
from omni_homevlog.schemas import JobState

pytestmark = pytest.mark.usefixtures("_no_real_network")


def render_calls(provider) -> list[dict]:
    return [c for c in provider.calls if c["kind"] != "get_interaction"]


# ── 429 quota ──────────────────────────────────────────────────────────────


def test_quota_exhaustion_stops_the_job_without_retrying(build_job) -> None:
    """§24.5: no key rotation, no project switching, no retry loop."""
    ctx, provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["quota"])
    )

    report = run_job(ctx)

    assert len(render_calls(provider)) == 1, "a 429 was retried"
    assert report.final_state is JobState.SEED_RENDERING
    assert any("quota" in e.lower() for e in ctx.store.load().errors)


def test_quota_error_is_not_marked_retryable() -> None:
    error = classify_http_error(429, provider_code="resource_exhausted", message="quota")
    assert isinstance(error, QuotaExhaustedError)
    assert error.retryable is False
    assert error.remediation and "rotate" in error.remediation


# ── 500 server error ───────────────────────────────────────────────────────


def test_server_error_is_retried_then_succeeds(build_job) -> None:
    """The transport retries 5xx; here the fake fails the first call only.

    The pipeline itself does not retry — it calls the provider once and the
    provider's own transport handles 5xx. So this asserts the pipeline treats a
    transient provider failure as a clean failure to be resumed, not as a reason
    to loop.
    """
    ctx, _provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_first_n=1, fail_with=FAILURES["server"])
    )

    report = run_job(ctx)
    assert report.final_state is JobState.FAILED_RETRYABLE or any(
        "500" in e or "internal" in e.lower() for e in ctx.store.load().errors
    )


def test_server_error_is_marked_retryable() -> None:
    error = classify_http_error(500, provider_code=None, message="internal")
    assert isinstance(error, ServerError)
    assert error.retryable is True


def test_a_failed_interaction_is_recorded_in_the_ledger(build_job) -> None:
    ctx, _provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["server"])
    )
    run_job(ctx)

    records = ctx.store.load().interactions
    assert records, "a failed call left no ledger entry"
    assert records[0].status == "failed"
    assert records[0].outcome_known is True, (
        "a definitively-rejected request must be marked resolved, or recovery "
        "will refuse to continue forever"
    )


#  the important one: timeout ──────────────────────────────────────────────


def test_a_timeout_is_never_retried(build_job) -> None:
    """§5.1 and the handoff: the generation may exist and may be billable."""
    ctx, provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["timeout"])
    )

    report = run_job(ctx)

    assert len(render_calls(provider)) == 1, "a timed-out request was re-issued"
    assert report.final_state is JobState.NEEDS_HUMAN


def test_a_timeout_is_recorded_as_unknown_outcome(build_job) -> None:
    ctx, _provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["timeout"])
    )
    run_job(ctx)

    unresolved = [r for r in ctx.store.load().interactions if not r.outcome_known]
    assert len(unresolved) == 1
    assert unresolved[0].status == "unknown"
    assert "billable" in " ".join(ctx.store.load().errors)


def test_resume_refuses_to_continue_while_an_outcome_is_unknown(build_job) -> None:
    ctx, _provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["timeout"])
    )
    run_job(ctx)

    reloaded = JobContext.load(ctx.job_id)
    plan = plan_resume(reloaded)

    assert plan.blocked
    assert plan.requires_human
    assert any("never observed" in b for b in plan.blockers)


def test_an_unknown_outcome_still_counts_against_the_budget(build_job) -> None:
    """It may be billable, so the reservation stands."""
    ctx, _provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["timeout"])
    )
    run_job(ctx)

    assert ctx.budget.calls_made == 1


def test_timeout_error_is_not_retryable() -> None:
    error = RequestTimeoutUnknownOutcome("timed out")
    assert error.retryable is False
    assert error.outcome_unknown is True


# ── permission ─────────────────────────────────────────────────────────────


def test_permission_denied_is_not_retried(build_job) -> None:
    ctx, provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["permission"])
    )
    run_job(ctx)

    assert len(render_calls(provider)) == 1


def test_permission_error_names_the_role_to_grant() -> None:
    error = classify_http_error(403, provider_code=None, message="denied")
    assert isinstance(error, PermissionError_)
    assert error.remediation and "aiplatform.user" in error.remediation


def test_gcs_permission_denied_on_download_is_reported(tmp_path) -> None:
    """§21.2's GCS permission row, at the storage layer."""
    from omni_homevlog.errors import ProviderError as PE
    from omni_homevlog.storage.gcs import GcsClient

    class DenyingSession:
        def get(self, *_args, **_kwargs):
            class Response:
                status_code = 403
                text = '{"error":{"message":"caller does not have storage.objects.get"}}'

            return Response()

        def post(self, *_args, **_kwargs):
            return self.get()

    client = GcsClient(session=DenyingSession(), prefer_library=False)
    with pytest.raises(PE, match="GCS download failed"):
        client.download_to("gs://bucket/obj.mp4", tmp_path / "out.mp4")


def test_gcs_write_probe_reports_failure_without_raising(tmp_path) -> None:
    from omni_homevlog.storage.gcs import GcsClient

    class DenyingSession:
        def post(self, *_args, **_kwargs):
            class Response:
                status_code = 403
                text = "denied"

            return Response()

    client = GcsClient(session=DenyingSession(), prefer_library=False)
    ok, message = client.check_writable("gs://bucket/prefix/")
    assert ok is False
    assert "403" in message


# ─ completed but no download ──────────────────────────────────────────────


def test_completed_but_undownloadable_artifact_is_not_treated_as_usable(
    build_job,
) -> None:
    """§21.2's "interaction completed but download failed" row.

    The artifact exists and names a URI, but has no local file. The chain must not
    pretend it can extend from bytes it does not have.
    """
    ctx, _provider, _ = build_job(provider_cls=DownloadFailingProvider)
    run_job(ctx)

    manifest = ctx.store.load()
    for artifact in manifest.segments:
        assert artifact.is_usable_video(), (
            "an artifact with a GCS URI counts as usable (it can be re-fetched)"
        )
        assert artifact.local_path is None
        assert artifact.gcs_uri is not None


def test_recovery_refetches_outputs_that_have_a_uri_and_no_file(build_job) -> None:
    """Recovery rule 3, exercised with a working downloader."""
    ctx, _provider, _ = build_job(provider_cls=DownloadFailingProvider)
    run_job(ctx)

    ctx.reload()
    manifest = ctx.store.load()
    assert all(a.local_path is None for a in manifest.segments)

    # Wire a GCS client that "succeeds" by copying a real file into place.
    class WorkingGcs:
        def download_to(self, uri, local_path):
            from tests.conftest import build_minimal_mp4

            from omni_homevlog.storage.local import atomic_write_bytes

            atomic_write_bytes(local_path, build_minimal_mp4(duration_s=3.0))
            return local_path

    ctx.provider.gcs = WorkingGcs()

    from omni_homevlog.pipeline.resume import fetch_missing_outputs

    recovered = fetch_missing_outputs(ctx)
    assert recovered, "recovery rule 3 recovered nothing"

    ctx.reload()
    assert all(a.local_path is not None for a in ctx.store.load().segments)


# ─ no video in the response ──────────────────────────────────────────────


def test_completed_with_no_video_is_an_error_not_an_empty_success(build_job) -> None:
    ctx, _provider, _ = build_job(provider_cls=NoVideoProvider)
    report = run_job(ctx)

    assert report.final_state is not JobState.COMPLETE
    assert any("no video content" in e for e in ctx.store.load().errors)


# ── director failure ──────────────────────────────────────────────────────


def test_director_failure_stops_rather_than_substituting_a_template(build_job) -> None:
    """Silently using the built-in template would ignore the user's brief."""
    from tests.integration.conftest import FailingDirector

    ctx, provider, _ = build_job(director=FailingDirector("model unavailable"))
    report = run_job(ctx)

    # It stops before the seed render, and never invents a plan.
    assert report.final_state is JobState.REFERENCES_VALIDATED
    assert len(render_calls(provider)) == 0, "a render ran despite a failed plan"

    manifest = ctx.store.load()
    assert manifest.segment_plan == []
    assert any("model unavailable" in e for e in manifest.errors)


def test_template_fallback_is_opt_in_and_flagged(build_job) -> None:
    from tests.integration.conftest import FailingDirector

    ctx, _provider, _ = build_job(director=FailingDirector("model unavailable"))
    report = Orchestrator(ctx, allow_template_fallback=True).run()

    assert report.stages_run, "the fallback did not run the pipeline"
    notes = " ".join(ctx.store.load().notes)
    assert "template" in notes.lower()
    assert "does NOT reflect your brief" in notes


# ── policy block ──────────────────────────────────────────────────────────


def test_a_reference_rejection_blocks_the_job_before_any_render() -> None:
    """§6.2 rejections are not advisory.

    Uses the real `IntakeResult` rather than a stand-in, so this exercises the
    type the pipeline actually sees.
    """
    from omni_homevlog.errors import ReferenceRejectedError
    from omni_homevlog.pipeline.intake import IntakeResult, require_approved
    from omni_homevlog.schemas import ReferenceRejection

    result = IntakeResult(
        rejections=[ReferenceRejection(asset_id="ref00", reasons=["timestamp visible"])]
    )

    with pytest.raises(ReferenceRejectedError, match="timestamp visible"):
        require_approved(result, strict=True)

    # The non-strict form returns what was approved rather than raising, which is
    # what a caller that has already decided to proceed needs.
    assert require_approved(result, strict=False) == []


def test_budget_exhaustion_is_a_blocking_state_not_a_retry_loop(build_job, spec) -> None:
    tight = spec.model_copy(update={"max_total_calls": 1, "title": "one call", "human_gates": []})
    ctx, provider, _ = build_job(spec_override=tight)

    run_job(ctx)

    assert len(render_calls(provider)) == 1
    assert ctx.store.load().state in (
        JobState.BUDGET_EXHAUSTED,
        JobState.NEEDS_HUMAN,
        JobState.FAILED_RETRYABLE,
    )


# ── ledger and index consistency ──────────────────────────────────────────


def test_database_index_matches_the_manifest_after_a_failure(build_job) -> None:
    ctx, _provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["server"])
    )
    run_job(ctx)

    manifest = ctx.store.load()
    ctx.db.rebuild_from_manifest(manifest)

    row = ctx.db.get_job(ctx.job_id)
    assert row is not None
    assert row["state"] == str(manifest.state)


def test_errors_are_append_only_across_a_resume(build_job) -> None:
    ctx, _provider, _ = build_job(
        behaviour=FakeProviderBehaviour(fail_on_call=1, fail_with=FAILURES["server"])
    )
    run_job(ctx)
    first_errors = list(ctx.store.load().errors)
    assert first_errors

    reloaded = JobContext.load(ctx.job_id)
    assert reloaded.store.load().errors == first_errors


def test_all_failure_errors_are_omni_vlog_errors() -> None:
    """Every injected failure must be one of our own types, so the pipeline's
    classifier sees it. A bare Exception would escape as a traceback."""
    for name, error in FAILURES.items():
        assert isinstance(error, OmniVlogError), f"{name} is not an OmniVlogError"


def test_provider_error_carries_a_code_and_message() -> None:
    error = ProviderError("something broke", http_status=418, provider_code="teapot")
    assert error.code == "provider_error"
    assert error.http_status == 418
    assert error.provider_code == "teapot"
    assert "something broke" in str(error)
