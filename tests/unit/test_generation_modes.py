from __future__ import annotations

import asyncio
import base64
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from tests.conftest import build_minimal_mp4

from omni_homevlog.errors import InvalidRequestError
from omni_homevlog.providers.base import RenderRequest
from omni_homevlog.providers.request_builder import seed_input_mode
from omni_homevlog.providers.response_parser import parse_interaction
from omni_homevlog.providers.transport import TransportResult
from omni_homevlog.providers.vertex_enterprise import VertexEnterpriseProvider
from omni_homevlog.schemas import ProviderCapabilities, ReferenceAsset
from omni_homevlog.storage.local import LocalStore


def provider(tmp_path):
    return VertexEnterpriseProvider(project="test-project", session=object(), paths=LocalStore(tmp_path).job("job-mode-test").ensure())


def seed_artifact(p):
    request = RenderRequest(task="text_to_video", prompt="seed", segment_index=0, attempt_index=0, duration_s=3, resolution="360p", aspect_ratio="16:9")
    envelope = parse_interaction({"id":"seed", "model":p.model, "status":"completed", "steps":[{"type":"model_output", "content":[{"type":"video", "mime_type":"video/mp4", "data":base64.b64encode(build_minimal_mp4(duration_s=3)).decode()}]}]})
    return p._artifact_from_result(request, TransportResult(envelope=envelope,http_status=200,started_at=0,finished_at=1,raw_text=""), Decimal(0))


def test_first_and_last_frames_use_literal_order_and_task(tmp_path, spec, bible, segments):
    p = provider(tmp_path)
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    first.write_bytes(b"first frame bytes")
    last.write_bytes(b"last frame bytes")
    refs = [ReferenceAsset(id="last",path_or_uri=str(last),role="last_frame",sha256="a",provenance="synthetic",approved=True), ReferenceAsset(id="first",path_or_uri=str(first),role="first_frame",sha256="z",provenance="synthetic",approved=True)]
    p._render = AsyncMock()
    asyncio.run(p.generate_seed(prompt="animate", assets=refs, spec=spec))
    request = p._render.call_args.args[0]
    assert request.task == "image_to_video"
    payload = p.build_payload(request)
    assert base64.b64decode(payload["input"][1]["data"]) == first.read_bytes()
    assert base64.b64decode(payload["input"][2]["data"]) == last.read_bytes()
    from omni_homevlog.prompts.compiler import PromptCompiler
    text = PromptCompiler(bible=bible,references=refs,spec=spec).compile_seed(segments[0]).text
    assert "literal opening frame" in text and "literal closing frame" in text
    assert "only as identity" not in text


@pytest.mark.parametrize("roles", [["last_frame"], ["first_frame", "environment"], ["first_frame", "first_frame"], ["environment"]*6])
def test_ambiguous_image_modes_are_rejected_before_dispatch(roles):
    with pytest.raises(InvalidRequestError):
        seed_input_mode(roles)


def test_steps_replay_preserves_saved_state_and_omits_server_parent(tmp_path, spec):
    p = provider(tmp_path)
    artifact = seed_artifact(p)
    p.capabilities = ProviderCapabilities(provider="vertex",project="test-project",model=p.model,stateful_steps_replay=True)
    p._render = AsyncMock()
    asyncio.run(p.extend(artifact=artifact,extension_prompt="continue",spec=spec,duration_s=3))
    request = p._render.call_args.args[0]
    payload = p.build_payload(request)
    assert request.parent_interaction_id == "seed"  # local lineage retained
    assert "previous_interaction_id" not in payload and "generation_config" not in payload
    assert payload["input"][0]["type"] == "model_output"
    assert payload["input"][-1] == {"type":"user_input","content":[{"type":"text","text":"continue"}]}
    assert request.expected_duration_s == 6


def test_native_uploaded_source_works_inline_without_bucket(tmp_path, spec):
    p = provider(tmp_path)
    artifact = seed_artifact(p)
    p.capabilities = ProviderCapabilities(provider="vertex",project="test-project",model=p.model,extend=True)
    p._render = AsyncMock()
    asyncio.run(p.extend(artifact=artifact,extension_prompt="continue",spec=spec,duration_s=3))
    payload = p.build_payload(p._render.call_args.args[0])
    assert payload["generation_config"]["video_config"]["task"] == "extend"
    assert "previous_interaction_id" not in payload
    assert payload["input"][-1]["type"] == "video"
    assert base64.b64decode(payload["input"][-1]["data"]) == build_minimal_mp4(duration_s=3)
