"""Async submit/recover contracts; all provider traffic is replaced."""
from __future__ import annotations

import asyncio
import base64
import json

from tests.conftest import build_minimal_mp4
from tests.integration.conftest import FakeProvider
from typer.testing import CliRunner

from omni_homevlog.budget import Budget
from omni_homevlog.cli import app
from omni_homevlog.config import get_settings
from omni_homevlog.errors import InteractionPending
from omni_homevlog.pipeline.orchestrator import RunReport, run_job
from omni_homevlog.providers.base import BaseVideoProvider
from omni_homevlog.providers.capability_probe import probe_generation
from omni_homevlog.providers.response_parser import parse_interaction
from omni_homevlog.providers.transport import TransportResult
from omni_homevlog.schemas import JobState, ProviderCapabilities, RenderArtifact
from omni_homevlog.storage.database import Database
from omni_homevlog.storage.local import LocalStore


def result(payload):
    return TransportResult(envelope=parse_interaction(payload), http_status=200, started_at=0, finished_at=1, raw_text="")


class PollTransport:
    debug_dir = None
    def __init__(self):
        self.creates = []
        self.gets = []
    def create_interaction(self, payload):
        self.creates.append(payload)
        return result({"id": "async-id", "status": "in_progress"})
    def get_interaction(self, interaction_id):
        self.gets.append(interaction_id)
        return result({"id": interaction_id, "status": "completed", "steps": [{"type": "model_output", "content": [{"type": "video", "data": base64.b64encode(build_minimal_mp4(duration_s=3)).decode(), "mime_type": "video/mp4"}]}]})


def test_background_probe_creates_once_and_queries_same_id(tmp_path):
    transport = PollTransport()
    provider = BaseVideoProvider(transport=transport, paths=LocalStore(tmp_path).job("job-async-probe").ensure())
    observed, envelope = asyncio.run(probe_generation(provider, task="text_to_video", background=True, budget=Budget(max_total_calls=1), poll_interval_s=0))
    assert observed.status.value == "PASS"
    assert observed.evidence["poll_count"] == 1
    assert len(transport.creates) == 1 and transport.creates[0]["background"] is True
    assert transport.gets == ["async-id"]
    assert envelope.interaction_id == "async-id"


def test_poll_exhaustion_is_blocked_not_a_new_create(tmp_path):
    transport = PollTransport()
    transport.get_interaction = lambda interaction_id: result({"id": interaction_id, "status": "in_progress"})
    provider = BaseVideoProvider(transport=transport, paths=LocalStore(tmp_path).job("job-pending-probe").ensure())
    observed, envelope = asyncio.run(probe_generation(provider, task="text_to_video", background=True, budget=Budget(max_total_calls=1), poll_attempts=2, poll_interval_s=0))
    assert observed.status.value == "BLOCKED"
    assert envelope.interaction_id == "async-id"
    assert len(transport.creates) == 1


class AsyncSeedProvider(FakeProvider):
    async def generate_seed(self, **kwargs):
        self.calls.append({"kind": "create", "background": kwargs["spec"].background})
        raise InteractionPending("queued", interaction_id="async-seed")
    async def get_interaction(self, interaction_id):
        from omni_homevlog.media.ffprobe import inspect_media
        self.calls.append({"kind": "get", "id": interaction_id})
        path = self.paths.attempt_path(0, 0, kind="recovered")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(build_minimal_mp4(duration_s=10))
        return RenderArtifact(interaction_id=interaction_id, task="text_to_video", model=self.model, provider=self.provider_name, project=self.project, segment_index=0, status="completed", local_path=str(path), media=inspect_media(path), prompt="", prompt_sha256="recovered")


def test_background_job_survives_reload_and_recover_only_does_not_spend(build_job, spec, monkeypatch):
    ctx, provider, _ = build_job(spec_override=spec.model_copy(update={"background": True}), provider_cls=AsyncSeedProvider, references=0)
    report = run_job(ctx)
    assert report.errors == []
    assert ctx.store.load().state == JobState.SEED_RENDERING
    assert ctx.store.load().interactions[0].interaction_id == "async-seed"
    assert ctx.store.load().spec.background
    assert ctx.budget.calls_made == 1
    monkeypatch.setattr("omni_homevlog.cli._load_context", lambda *a, **kw: ctx)
    invoked = CliRunner().invoke(app, ["resume", ctx.job_id, "--recover-only"])
    assert invoked.exit_code == 0, invoked.output
    assert ctx.store.load().state == JobState.SEED_REVIEW
    assert [r["kind"] for r in provider.calls] == ["create", "get"]
    assert ctx.budget.calls_made == 1
    assert ctx.store.load().segments[0].task == "text_to_video"


def test_create_json_has_no_banner_and_preserves_background(monkeypatch):
    cfg = get_settings()
    caps = ProviderCapabilities(provider="vertex", project="test-project", model=cfg.omni_vertex_model, location="global", t2v=True)
    Database(LocalStore(cfg.data_dir()).job("job-doctor-probe").ensure().db_path).save_probe(caps)
    monkeypatch.setattr("omni_homevlog.providers.factory.check_provider_ready", lambda _: (True, "ready"))
    def no_run(ctx, **kwargs):
        assert ctx.spec.background
        return RunReport(job_id=ctx.job_id, final_state=ctx.manifest.state)
    monkeypatch.setattr("omni_homevlog.pipeline.orchestrator.run_job", no_run)
    result = CliRunner().invoke(app, ["create", "--brief", "test", "--project", "test-project", "--duration", "10", "--json", "--background"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["final_state"] == "CREATED"
