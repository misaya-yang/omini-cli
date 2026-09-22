"""HTTP workflow and durable recovery without external requests."""

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from omni_homevlog.config import Settings
from omni_homevlog.errors import InteractionPending
from omni_homevlog.schemas import RenderArtifact
from omni_homevlog.studio.app import create_app
from omni_homevlog.studio.service import StudioService

HEADERS = {"X-Studio-Request": "1"}


class LocalService(StudioService):
    calls = 0
    gets = 0
    pending = False

    def _work(self, cid, recover):
        try:
            self._step(cid, recover)
        finally:
            with self.guard:
                self.active.discard(cid)

    def artifact(self, data, version):
        path = self.root / data["id"] / f"v{len(data['versions'])}.mp4"
        path.write_bytes(b"local-video-fixture")
        return RenderArtifact(
            interaction_id=f"id-{len(data['versions'])}",
            task="text_to_video",
            model="test",
            provider="vertex",
            status="completed",
            prompt=version["prompt"],
            prompt_sha256="test",
            local_path=str(path),
        )

    def _render(self, data, version):
        self.calls += 1
        if self.pending:
            raise InteractionPending("pending", interaction_id="remote-1")
        return self.artifact(data, version)

    def _recover(self, data, version):
        self.gets += 1
        return self.artifact(data, version)


@pytest.fixture
def studio(tmp_path):
    svc = LocalService(Settings(omni_data_dir=tmp_path, google_cloud_project="test-project"))
    with TestClient(create_app(svc)) as client:
        yield client, svc


def wait(client, cid):
    for _ in range(100):
        data = client.get("/api/creations/" + cid).json()
        if not data["busy"]:
            return data
        time.sleep(0.01)
    raise AssertionError("worker did not finish")


def create(client, **kwargs):
    import json

    response = client.post(
        "/api/creations",
        data={"options": json.dumps({"brief": "阳光洒在桌面", **kwargs})},
        headers=HEADERS,
    )
    assert response.status_code == 202, response.text
    return wait(client, response.json()["id"])


def test_create_edit_old_version_download_and_restart(studio):
    client, svc = studio
    data = create(client)
    cid = data["id"]
    first = data["versions"][0]["url"]
    assert data["versions"][0]["status"] == "ready"
    assert client.get(first).content == b"local-video-fixture"
    response = client.post(
        f"/api/creations/{cid}/edit", json={"prompt": "换成浅木色", "version": 0}, headers=HEADERS
    )
    assert response.status_code == 202
    data = wait(client, cid)
    assert len(data["versions"]) == 2 and data["versions"][1]["parent"] == 0
    assert svc.calls == 2
    assert client.get(first).status_code == 200
    assert "attachment" in client.get(first + "?download=true").headers["content-disposition"]
    assert client.get(first, headers={"Range": "bytes=0-4"}).status_code == 206
    reopened = LocalService(svc.settings)
    try:
        assert reopened.public(reopened.load(cid))["versions"] == data["versions"]
    finally:
        reopened.close()
    assert "job_id" not in str(client.get("/api/creations").json())


def test_pending_is_get_only_and_blocks_new_post(studio):
    client, svc = studio
    svc.pending = True
    data = create(client)
    cid = data["id"]
    assert data["versions"][0]["status"] == "pending"
    assert (
        client.post(
            f"/api/creations/{cid}/edit", json={"prompt": "test", "version": 0}, headers=HEADERS
        ).status_code
        == 400
    )
    assert client.post(f"/api/creations/{cid}/recover", headers=HEADERS).status_code == 202
    assert wait(client, cid)["versions"][0]["status"] == "ready"
    assert svc.calls == 1 and svc.gets == 1
    assert client.post(f"/api/creations/{cid}/recover", headers=HEADERS).status_code == 400


def test_upload_validation_and_reference_route(studio):
    client, svc = studio
    options = '{"brief":"测试"}'
    bad = client.post(
        "/api/creations",
        data={"options": options},
        files={"images": ("bad.jpg", b"not image", "image/jpeg")},
        headers=HEADERS,
    )
    assert bad.status_code == 400 and svc.calls == 0
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(buffer, "PNG")
    good = client.post(
        "/api/creations",
        data={"options": options},
        files={"images": ("../../test.png", buffer.getvalue(), "image/png")},
        headers=HEADERS,
    )
    data = wait(client, good.json()["id"])
    assert client.get(data["references"][0]).headers["content-type"] == "image/jpeg"
    saved = svc.load(data["id"])["references"][0]
    assert saved["approved"] is False
    assert Path(saved["path_or_uri"]).is_relative_to(svc.root)


def test_origin_and_input_protection(studio):
    client, svc = studio
    assert client.post("/api/creations", data={"options": '{"brief":"x"}'}).status_code == 403
    assert (
        client.post(
            "/api/creations",
            headers={**HEADERS, "Origin": "https://elsewhere.example"},
            data={"options": '{"brief":"x"}'},
        ).status_code
        == 403
    )
    assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/creations/invalid").status_code == 400
    assert client.get("/api/creations/" + "a" * 32).status_code == 404
    assert (
        client.post(
            "/api/creations", headers=HEADERS, data={"options": '{"brief":" ","duration":4}'}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/creations", headers=HEADERS, data={"options": '{"brief":"x","duration":40}'}
        ).status_code
        == 400
    )
    assert svc.calls == 0


def test_real_adapter_dispatch_has_one_budget_per_version(tmp_path, monkeypatch):
    from omni_homevlog.pipeline.context import JobContext
    from omni_homevlog.providers.base import BaseVideoProvider
    from omni_homevlog.schemas import ProviderCapabilities
    from omni_homevlog.studio import service as module

    calls = []

    async def generate(provider, **kwargs):
        calls.append(kwargs)
        path = provider.paths.renders_dir / "output.mp4"
        path.write_bytes(b"video")
        return RenderArtifact(
            interaction_id="generated",
            task="text_to_video",
            model=provider.model,
            provider=provider.provider_name,
            project=provider.project,
            status="completed",
            prompt=kwargs["prompt"],
            prompt_sha256="test",
            local_path=str(path),
        )

    async def edit(provider, **kwargs):
        return await generate(provider, prompt=kwargs["edit_prompt"], spec=kwargs["spec"])

    monkeypatch.setattr(BaseVideoProvider, "generate_seed", generate)
    monkeypatch.setattr(BaseVideoProvider, "edit", edit)
    monkeypatch.setattr(
        module,
        "latest_capabilities",
        lambda **kw: ProviderCapabilities(
            provider=kw["provider"], project=kw["project"], model=kw["model"], edit=True
        ),
    )
    svc = StudioService(Settings(omni_data_dir=tmp_path, google_cloud_project="test-project"))
    with TestClient(create_app(svc)) as client:
        data = create(client)
        cid = data["id"]
        assert data["versions"][0]["status"] == "ready", data
        assert (
            client.post(
                f"/api/creations/{cid}/edit",
                json={"prompt": "改变颜色", "version": 0},
                headers=HEADERS,
            ).status_code
            == 202
        )
        data = wait(client, cid)
        assert data["versions"][1]["status"] == "ready", data
        assert len(calls) == 2
        for version in svc.load(cid)["versions"]:
            ctx = JobContext.load(version["job_id"], settings=svc.settings)
            assert ctx.budget.calls_made == 1
            assert ctx.budget.max_total_calls == 1
            assert not (ctx.paths.debug_dir / "llm_calls.json").exists()


def test_duplicate_creation_request_does_not_generate_twice(studio):
    client, svc = studio
    first = create(client, request_id="b" * 32)
    second = create(client, request_id="b" * 32)
    assert first["id"] == second["id"]
    assert svc.calls == 1
