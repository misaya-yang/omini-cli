"""Phase 0 capability probe (§16).

§16 is a hard gate: "Codex 必须先交付 `doctor` 和 `CAPABILITY_REPORT.md`，没有该报告
不得进入正式 Agent." This module is the machinery behind `omni-vlog doctor`.

The probe answers one question per capability and records PASS / FAIL / UNKNOWN
with the evidence. It is deliberately **conservative about what counts as
evidence**:

  * A capability is only PASS when a real request demonstrated it.
  * A read-only reachability check proves *auth and routing*, not generation.
  * A quota failure (429) is recorded as BLOCKED, not FAIL — the model may work
    fine; the project simply has no quota right now. Those are different facts
    and conflating them sends people debugging the wrong thing.
  * Anything not attempted is UNKNOWN. There is no default-to-PASS.

Cost control. The full matrix is expensive, so the probe defaults to
`run_generation=False`, which performs everything that is free (auth, endpoint
reachability, model-listing) and marks every generation row UNKNOWN with an
explicit "not attempted" reason. Generation tests run only when asked, one at a
time, each at the cheapest settings the surface allows (3s, 360p, no people),
exactly as §21.3 prescribes for live smoke tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from omni_homevlog.errors import (
    AuthError,
    BudgetExhaustedError,
    MissingCredentialsError,
    OmniVlogError,
    PermissionError_,
    QuotaExhaustedError,
    RequestTimeoutUnknownOutcome,
)
from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.schemas import ProviderCapabilities, utc_now_iso

logger = get_logger("probe")


class ProbeStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


@dataclass(slots=True)
class ProbeResult:
    name: str
    status: ProbeStatus
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    latency_s: float | None = None

    @property
    def symbol(self) -> str:
        return {
            ProbeStatus.PASS: "PASS",
            ProbeStatus.FAIL: "FAIL",
            ProbeStatus.UNKNOWN: "UNKNOWN",
            ProbeStatus.BLOCKED: "BLOCKED",
            ProbeStatus.SKIPPED: "SKIPPED",
        }[self.status]


@dataclass(slots=True)
class ProbeReport:
    provider: str
    project: str | None
    model: str
    started_at: str
    results: list[ProbeResult] = field(default_factory=list)
    capabilities: ProviderCapabilities | None = None
    sdk_version: str | None = None
    python_version: str | None = None
    error: str | None = None

    def get(self, name: str) -> ProbeResult | None:
        return next((r for r in self.results if r.name == name), None)

    def passed(self, name: str) -> bool:
        result = self.get(name)
        return result is not None and result.status is ProbeStatus.PASS

    def add(self, result: ProbeResult) -> ProbeResult:
        self.results.append(result)
        return result

    @property
    def ok(self) -> bool:
        """Any hard failure at all means the surface is not usable as probed."""
        return not any(r.status is ProbeStatus.FAIL for r in self.results)


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────


def check_auth(provider: Any) -> ProbeResult:
    """Is there a usable credential, before any spend? (handoff acceptance #2)"""
    started = time.monotonic()
    if provider.provider_name == "vertex":
        from omni_homevlog.providers.auth import check_adc_available

        ok, message, _ = check_adc_available()
        return ProbeResult(
            name="Auth",
            status=ProbeStatus.PASS if ok else ProbeStatus.FAIL,
            detail=message,
            latency_s=time.monotonic() - started,
        )

    # gemini_api: presence of a key is all we can check without spending.
    key = getattr(provider.transport, "_api_key", None)
    return ProbeResult(
        name="Auth",
        status=ProbeStatus.PASS if key else ProbeStatus.FAIL,
        detail="API key present" if key else "GEMINI_API_KEY missing",
        latency_s=time.monotonic() - started,
    )


def check_endpoint_reachable(provider: Any) -> ProbeResult:
    """Read-only reachability.

    Proves routing, token refresh, and API enablement — the handoff's own
    verified state. It does **not** prove generation works, and the detail string
    says so, because a green row here is easy to over-read.
    """
    started = time.monotonic()
    try:
        session = provider.transport.session
    except MissingCredentialsError as exc:
        return ProbeResult(
            name="Endpoint reachable",
            status=ProbeStatus.FAIL,
            detail=f"{exc.message} {exc.remediation or ''}".strip(),
        )

    url = provider.transport.create_url()
    try:
        # A GET on the collection root: read-only, and a structured error still
        # proves we reached the service.
        response = session.get(url, timeout=30)
        status = int(getattr(response, "status_code", 0))
        latency = time.monotonic() - started
        if status in (200, 400, 404, 405):
            return ProbeResult(
                name="Endpoint reachable",
                status=ProbeStatus.PASS,
                detail=(
                    f"HTTP {status} from {url}. Auth and routing confirmed; "
                    "generation is NOT implied by this row."
                ),
                evidence={"http_status": status},
                latency_s=latency,
            )
        if status in (401, 403):
            return ProbeResult(
                name="Endpoint reachable",
                status=ProbeStatus.FAIL,
                detail=f"HTTP {status} — credentials rejected or IAM missing.",
                evidence={"http_status": status, "body": _short(response)},
                latency_s=latency,
            )
        return ProbeResult(
            name="Endpoint reachable",
            status=ProbeStatus.UNKNOWN,
            detail=f"Unexpected HTTP {status}: {_short(response)}",
            evidence={"http_status": status},
            latency_s=latency,
        )
    except Exception as exc:
        return ProbeResult(
            name="Endpoint reachable",
            status=ProbeStatus.FAIL,
            detail=f"{type(exc).__name__}: {exc}",
            latency_s=time.monotonic() - started,
        )


def _short(response: Any, limit: int = 300) -> str:
    text = getattr(response, "text", "")
    return str(text)[:limit]


def _classify_probe_failure(exc: OmniVlogError) -> tuple[ProbeStatus, str]:
    """Map a provider error onto a probe verdict.

    The distinction that matters: a 429 says nothing about whether the model can
    do the thing. Recording it as FAIL would send someone hunting a code bug.
    """
    if isinstance(exc, QuotaExhaustedError):
        return (
            ProbeStatus.BLOCKED,
            "Quota exhausted. Preview models have fixed quota; this is a project "
            "entitlement issue, not evidence the capability is unsupported.",
        )
    if isinstance(exc, (AuthError, MissingCredentialsError)):
        return ProbeStatus.FAIL, f"Authentication failed: {exc.message}"
    if isinstance(exc, PermissionError_):
        return ProbeStatus.FAIL, f"Permission denied: {exc.message}"
    if isinstance(exc, RequestTimeoutUnknownOutcome):
        return (
            ProbeStatus.BLOCKED,
            "Timed out with unknown outcome. NOT retried: the generation may exist "
            "and may be billable.",
        )
    return ProbeStatus.FAIL, str(exc)


# ────────────────────────────────────────────────────────────────────────────
# Generation probes (cost money — opt in)
# ────────────────────────────────────────────────────────────────────────────


def _probe_prompt_for(task: str) -> str:
    """Cheapest useful prompt, and deliberately without people.

    §21.3: live smoke tests use "无人物" scenes. A probe that renders a person
    would also drag in the identity/consent questions the plan is careful about,
    for no extra information about the API.
    """
    if task == "text_to_video":
        return (
            "A calm static shot of an empty wooden table beside a window in the "
            "morning. Soft daylight, gentle handheld drift, no people, no text."
        )
    if task == "image_to_video":
        return "Slow handheld push toward the centre of the scene. No people, no text."
    if task == "reference_to_video":
        return (
            "A quiet handheld shot of the referenced room, keeping the same layout, "
            "colours, and lighting. No people, no text."
        )
    if task == "edit":
        return "Change only the tabletop colour to muted blue. Preserve the entire video duration, camera movement, lighting and audio. Change nothing else."
    return "Continue the shot naturally for a few more seconds. Change nothing else."


def _continuation_prompt() -> str:
    """The prompt for a probe that continues an earlier interaction.

    A continuation must *ask* to continue. The probe used to send the seed's
    `text_to_video` prompt — "generate a shot of an empty wooden table" — while
    also passing `previous_interaction_id`, so the model got two contradictory
    instructions and resolved them differently each time. Two identical runs
    produced 6.016s and 3.008s, which looks exactly like an unreliable API and is
    actually a badly-posed request.

    The distinction matters more than the probe: a chain whose extension prompt
    re-describes the scene invites the model to re-establish it, which is the
    scene reset §18 exists to prevent.
    """
    return (
        "Continue directly from the exact final moment of the previous video. "
        "The camera keeps drifting slowly over the same scene at the same time of "
        "day. Do not reintroduce the scene, do not cut, and do not change the "
        "lighting."
    )


async def probe_generation(
    provider: Any,
    *,
    task: str,
    duration_s: int = 3,
    resolution: str = "360p",
    include_video_input: str | None = None,
    previous_interaction_id: str | None = None,
    label: str | None = None,
    budget: Any | None = None,
    segment_index: int = -1,
    reference_path: str | None = None,
    background: bool = False,
    poll_attempts: int = 6,
    poll_interval_s: float = 2.0,
    last_frame_path: str | None = None,
    prior_steps: list[dict[str, Any]] | None = None,
    source_video: dict[str, str] | None = None,
) -> tuple[ProbeResult, Any | None]:
    """Run one paid generation probe. Returns the result and any parsed envelope.

    Every paid probe passes through `Budget.authorize` first (§12.2). A probe run
    without a budget is unbounded by this code, so `doctor` supplies one.
    """
    name = label or f"{task} {duration_s}s {resolution}"
    started = time.monotonic()

    if provider.provider_name == "vertex" and not provider.project:
        return (
            ProbeResult(name=name, status=ProbeStatus.FAIL, detail="No project bound."),
            None,
        )

    if budget is not None:
        from omni_homevlog.budget import CallKind
        from omni_homevlog.costing import estimate_video_seconds_cost

        try:
            budget.authorize(
                CallKind.PROBE,
                segment_index=segment_index,
                attempt_index=0,
                video_seconds=duration_s,
                estimated_cost_usd=estimate_video_seconds_cost(
                    model=provider.model, video_seconds=duration_s
                ),
            )
        except BudgetExhaustedError as exc:
            return (
                ProbeResult(
                    name=name,
                    status=ProbeStatus.SKIPPED,
                    detail=f"Budget refused this probe: {exc.message}",
                ),
                None,
            )

    if task == "extend" and not include_video_input and not previous_interaction_id:
        return (
            ProbeResult(
                name=name,
                status=ProbeStatus.SKIPPED,
                detail="Extend needs an input video or a previous interaction id.",
            ),
            None,
        )

    # Build the payload through the *provider*, not the raw builder. The surface
    # adapters apply rules the generic builder cannot know about — on Vertex, a
    # continuation must omit `generation_config` or the request is rejected — and a
    # probe that skipped those rules would measure a request shape the pipeline
    # never actually sends.
    from omni_homevlog.providers.base import RenderRequest

    request = RenderRequest(
        task=task,  # type: ignore[arg-type]
        background=background,
        # A continuation asks to continue; everything else uses its task prompt.
        # Sending the seed prompt here made the model choose between "generate a
        # shot of a table" and "continue the previous video", and it chose
        # differently on different runs.
        prompt=(
            _continuation_prompt()
            if (previous_interaction_id or task == "extend") and task != "edit"
            else _probe_prompt_for(task)
        ),
        segment_index=segment_index,
        attempt_index=0,
        aspect_ratio="16:9",
        resolution=resolution,
        duration_s=duration_s,
        parent_interaction_id=previous_interaction_id,
        input_video_uri=include_video_input,
        input_video=source_video,
        prior_steps=prior_steps,
        labels={"probe": "1"},
    )
    payload = provider.build_payload(request)
    for image_path in (reference_path, last_frame_path):
        if image_path:
            from pathlib import Path

            from omni_homevlog.media.inspect_image import mime_for_path
            ref = Path(image_path)
            payload["input"].append(provider.encode_inline_reference(ref, mime_for_path(ref) or "image/png"))
    if provider.paths is not None:
        from omni_homevlog.storage.local import atomic_write_json

        atomic_write_json(
            provider.paths.debug_dir / f"probe_dispatch_{segment_index}.json",
            {
                "label": name,
                "status": "dispatched",
                "previous_interaction_id": previous_interaction_id,
                "duration_s": duration_s,
                "budget": budget.snapshot() if budget else None,
            },
        )

    import asyncio

    created_id = None
    poll_count = 0
    initial_status = None
    try:
        result = await asyncio.to_thread(provider.transport.create_interaction, payload)
        created_id = result.envelope.interaction_id
        initial_status = result.envelope.status
        if provider.paths is not None:
            atomic_write_json(provider.paths.debug_dir / f"probe_dispatch_{segment_index}.json", {"label": name, "status": initial_status, "interaction_id": created_id, "previous_interaction_id": previous_interaction_id, "duration_s": duration_s, "budget": budget.snapshot() if budget else None})
        while result.envelope.is_pending and poll_count < poll_attempts:
            if poll_count:
                await asyncio.sleep(poll_interval_s)
            poll_count += 1
            result = await asyncio.to_thread(provider.transport.get_interaction, created_id)
    except OmniVlogError as exc:
        status, detail = _classify_probe_failure(exc)
        return (
            ProbeResult(
                name=name,
                status=status,
                detail=detail,
                evidence={"error_code": getattr(exc, "code", None), "interaction_id": created_id, "poll_count": poll_count},
                latency_s=time.monotonic() - started,
            ),
            None,
        )
    except Exception as exc:
        return (
            ProbeResult(
                name=name,
                status=ProbeStatus.FAIL,
                detail=f"Unexpected {type(exc).__name__}: {exc}",
                latency_s=time.monotonic() - started,
            ),
            None,
        )

    envelope = result.envelope
    latency = time.monotonic() - started
    evidence: dict[str, Any] = {
        "interaction_id": envelope.interaction_id,
        "background": background,
        "task": task,
        "requested_duration_s": duration_s,
        "initial_status": initial_status,
        "poll_count": poll_count,
        "status": envelope.status,
        "http_status": result.http_status,
        "parsed_from": envelope.parsed_from,
        "usage": envelope.usage,
        "latency_s": round(latency, 3),
        "video_uri": envelope.video.uri if envelope.video else None,
        "delivery": "inline" if (envelope.video and envelope.video.is_inline) else "uri",
    }

    if envelope.status.lower() != "completed":
        status = ProbeStatus.BLOCKED if envelope.is_pending else ProbeStatus.FAIL
        detail = f"Interaction status was '{envelope.status}'; existing ID: {envelope.interaction_id}. No new generation will be issued."
        if envelope.errors:
            detail += f": {envelope.errors[0].get('message')}"
        return ProbeResult(
            name=name, status=status, detail=detail, evidence=evidence, latency_s=latency
        ), envelope

    if not envelope.videos:
        return (
            ProbeResult(
                name=name,
                status=ProbeStatus.FAIL,
                detail=f"Status completed but no video content. Steps: {[s.get('type') for s in envelope.steps]}",
                evidence=evidence,
                latency_s=latency,
            ),
            envelope,
        )

    # Media verification: an HTTP 200 is not an output (handoff acceptance #4).
    media_detail = "not verified (inline payload not written to disk in probe mode)"
    if provider.paths is not None:
        try:
            video = envelope.video
            assert video is not None
            target = provider.paths.debug_dir / f"probe_{segment_index}_{task}_{time.time_ns()}.mp4"
            if video.is_inline:
                from omni_homevlog.storage.local import atomic_write_bytes

                atomic_write_bytes(target, video.decode())
            elif video.uri and video.uri.startswith("gs://") and provider.gcs is not None:
                provider.gcs.download_to(video.uri, target)
            elif video.uri and video.uri.startswith("https://"):
                provider._download_https(video.uri, target)
            else:
                target = None
            if target is not None and target.is_file():
                info = inspect_media(target)
                evidence["media"] = info.model_dump()
                evidence["local_path"] = str(target)
                import hashlib

                evidence["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
                media_detail = (
                    f"{info.width}x{info.height}, {info.duration_s}s, "
                    f"{info.video_codec}, audio={info.has_audio} (via {info.probed_with})"
                )
        except Exception as exc:
            media_detail = f"media verification failed: {type(exc).__name__}: {exc}"

    media = evidence.get("media") or {}
    verified = bool(media.get("duration_s") and not media.get("error") and media.get("video_codec"))
    return (
        ProbeResult(
            name=name,
            status=ProbeStatus.PASS if verified else ProbeStatus.FAIL,
            detail=f"completed in {latency:.1f}s; {media_detail}",
            evidence=evidence,
            latency_s=latency,
        ),
        envelope,
    )


# ─────────────────────────────────────────────────────────────────────────────
# The matrix
# ─────────────────────────────────────────────────────────────────────────────


async def probe_provider(
    provider: Any,
    *,
    run_generation: bool = False,
    budget: Any | None = None,
) -> ProviderCapabilities:
    """Fill in a `ProviderCapabilities` for one surface.

    `run_generation=False` performs only the free checks and leaves every
    generation capability False with an explanatory note. That is the honest
    default: an unprobed capability must not read as supported (§2.3).
    """
    report = await build_probe_report(provider, run_generation=run_generation, budget=budget)
    assert report.capabilities is not None
    return report.capabilities


async def build_probe_report(
    provider: Any,
    *,
    run_generation: bool = False,
    budget: Any | None = None,
    checks: tuple[str, ...] = ("seed", "extend", "third", "edit"),
    reference_path: str | None = None,
    background: bool = False,
    seed_mode: str = "auto",
    last_frame_path: str | None = None,
    chain_strategy: str = "previous",
    duration_s: int = 3,
) -> ProbeReport:
    """Run the probe matrix and return the full report."""
    import sys

    report = ProbeReport(
        provider=provider.provider_name,
        project=provider.project,
        model=provider.model,
        started_at=utc_now_iso(),
        python_version=sys.version.split()[0],
    )
    report.sdk_version = _sdk_version()

    auth = report.add(check_auth(provider))
    if auth.status is ProbeStatus.FAIL:
        report.capabilities = _capabilities_from(report)
        report.error = "Authentication failed; no further probing attempted."
        return report

    reachable = report.add(check_endpoint_reachable(provider))
    if reachable.status is ProbeStatus.FAIL:
        report.capabilities = _capabilities_from(report)
        report.error = "Endpoint unreachable; no further probing attempted."
        return report

    if not run_generation:
        for name in (
            "T2V",
            "I2V",
            "Reference-to-video",
            "Edit",
            "Extend",
            "previous_interaction_id",
            "steps replay",
            "GCS URI delivery",
            "Native audio",
        ):
            report.add(
                ProbeResult(
                    name=name,
                    status=ProbeStatus.SKIPPED,
                    detail=(
                        "Not attempted: generation probes cost money. Re-run with "
                        "`--run-generation` (and `RUN_LIVE_VIDEO_TESTS=1`) to measure it."
                    ),
                )
            )
        report.add(
            ProbeResult(
                name="Max tested chain",
                status=ProbeStatus.SKIPPED,
                detail="Not attempted without --run-generation.",
            )
        )
        report.capabilities = _capabilities_from(report)
        return report

    mode = ("reference" if reference_path else "text") if seed_mode == "auto" else seed_mode
    task, seed_label = {"text": ("text_to_video", "T2V"), "reference": ("reference_to_video", "Reference-to-video"), "image": ("image_to_video", "I2V"), "first-last": ("image_to_video", "First + last frame")}[mode]
    seed_result, envelope = await probe_generation(
        provider,
        task=task,
        duration_s=duration_s,
        label=seed_label,
        budget=budget,
        segment_index=0,
        reference_path=reference_path,
        last_frame_path=last_frame_path,
        background=background,
    )
    report.add(seed_result)
    current_result = seed_result
    if seed_result.status is ProbeStatus.PASS and envelope is not None:
        for index, check in enumerate((c for c in checks if c != "seed"), 1):
            chain_label = {"previous": "previous_interaction_id", "steps": "steps replay", "source": "Extend"}[chain_strategy]
            label = "Edit" if check == "edit" else ("third link" if check == "third" else chain_label)
            source_video = None
            prior_steps = None
            if chain_strategy == "steps":
                prior_steps = envelope.steps
            elif chain_strategy == "source":
                video = envelope.require_video()
                source_video = {"type": "video", "mime_type": video.mime_type or "video/mp4"}
                if video.uri:
                    source_video["uri"] = video.uri
                elif video.data_b64:
                    source_video["data"] = video.data_b64
            result, next_envelope = await probe_generation(
                provider,
                task="edit" if check == "edit" else "extend",
                duration_s=round(current_result.evidence["media"]["duration_s"])
                if check == "edit"
                else duration_s,
                label=label,
                background=background,
                previous_interaction_id=envelope.interaction_id,
                budget=budget,
                segment_index=index,
                prior_steps=prior_steps,
                source_video=source_video,
            )
            report.add(result)
            if result.status is not ProbeStatus.PASS or next_envelope is None:
                break
            if check in ("extend", "third"):
                _assert_continuation_grew(report, current_result, result, duration_s=duration_s)
                growth = report.results[-1]
                if check == "third":
                    growth.name = "third link grows the film"
                elif chain_strategy != "previous":
                    growth.name = f"{chain_strategy} continuation grows the film"
                if growth.status is not ProbeStatus.PASS:
                    break
            elif check == "edit":
                before = (current_result.evidence.get("media") or {}).get("duration_s")
                after = (result.evidence.get("media") or {}).get("duration_s")
                if before is None or after is None or abs(before - after) > 0.6:
                    result.status = ProbeStatus.FAIL
                    result.detail = "Edit changed cumulative duration; review manually."
                    break
            current_result, envelope = result, next_envelope
    successes = [r for r in report.results if r.status is ProbeStatus.PASS and r.evidence.get("media")]
    if any(r.evidence.get("poll_count", 0) > 0 for r in successes):
        report.add(ProbeResult(name="Async polling", status=ProbeStatus.PASS, detail="Acknowledged pending interaction retrieved by GET; exactly one POST per probe."))
        report.add(ProbeResult(name="Remote recovery", status=ProbeStatus.PASS, detail="Output retrieved through the provider GET path and media verified."))
    if any(r.evidence["media"].get("has_audio") for r in successes):
        report.add(ProbeResult(name="Native audio", status=ProbeStatus.PASS, detail="Audio track detected in downloaded output."))
    if any(str(r.evidence.get("video_uri", "")).startswith("gs://") for r in successes):
        report.add(ProbeResult(name="GCS URI delivery", status=ProbeStatus.PASS, detail="Generated output delivered through GCS and downloaded media verified."))
    for name in (
        "I2V",
        "Reference-to-video",
        "First + last frame",
        "Edit",
        "Extend",
        "steps replay",
        "GCS URI delivery",
        "Native audio",
    ):
        if report.get(name) is None:
            report.add(
                ProbeResult(
                    name=name,
                    status=ProbeStatus.SKIPPED,
                    detail="Not selected or no suitable input; no capability inferred.",
                )
            )
    report.capabilities = _capabilities_from(report)
    return report


def _assert_continuation_grew(report: ProbeReport, seed: ProbeResult, follow: ProbeResult, *, duration_s: int = 3) -> None:
    """Confirm a continuation produced a *longer* film, not just a 200.

    A request that is accepted and returns a fresh clip of the same length is not
    a continuation; it is an independent generation wearing one. The distinction is
    the whole basis of §24.3, and the only observable difference is the duration.
    """
    seed_seconds = (seed.evidence.get("media") or {}).get("duration_s")
    follow_seconds = (follow.evidence.get("media") or {}).get("duration_s")

    if seed_seconds is None or follow_seconds is None:
        follow.detail += (
            " | duration not verified (no media probe ran), so 'continued' means "
            "only that the request was accepted"
        )
        report.add(
            ProbeResult(
                name="continuation grows the film",
                status=ProbeStatus.UNKNOWN,
                detail=(
                    "Both durations are needed to tell a continuation from an "
                    "independent clip, and at least one could not be measured."
                ),
            )
        )
        return

    grew = abs(follow_seconds - seed_seconds - duration_s) <= 0.6
    report.add(
        ProbeResult(
            name="continuation grows the film",
            status=ProbeStatus.PASS if grew else ProbeStatus.FAIL,
            detail=(
                f"seed {seed_seconds:.3f}s -> continuation {follow_seconds:.3f}s"
                + (
                    ""
                    if grew
                    else ". The film did NOT grow, so this is an independent clip "
                    "rather than a continuation, and the 30-second chain cannot be "
                    "built from it without splicing (§24.3)."
                )
            ),
            evidence={"seed_s": seed_seconds, "continuation_s": follow_seconds},
        )
    )


def _capabilities_from(report: ProbeReport) -> ProviderCapabilities:
    """Turn probe rows into a capability record. UNKNOWN never becomes True."""
    caps = ProviderCapabilities(
        provider=report.provider,
        project=report.project,
        model=report.model,
        probed_at=utc_now_iso(),
    )
    caps.t2v = report.passed("T2V") or report.passed("T2V 3s 360p")
    caps.i2v = report.passed("I2V") or report.passed("I2V 3s 360p")
    caps.reference_to_video = report.passed("Reference-to-video")
    caps.first_last_frame = report.passed("First + last frame")
    caps.edit = report.passed("Edit")
    caps.async_polling = report.passed("Async polling")
    caps.remote_retrieval = report.passed("Remote recovery")
    caps.extend = report.passed("Extend") and report.passed("source continuation grows the film")
    caps.stateful_previous_interaction_id = report.passed(
        "previous_interaction_id"
    ) and report.passed("continuation grows the film")
    caps.stateful_steps_replay = report.passed("steps replay") and report.passed("steps continuation grows the film")
    caps.gcs_delivery = report.passed("GCS URI delivery")
    caps.uri_delivery = caps.gcs_delivery

    if caps.t2v:
        # A 3s probe proves *a* generation happened; it does not prove the 10s
        # ceiling. Record what was measured, not what is documented.
        caps.max_generation_s = 10
        caps.notes.append(
            "max_generation_s is the documented ceiling; measured_generation_s records this probe's actual output duration."
        )

    # The longest chain a probe actually demonstrated, in seconds.
    #
    # This used to read `3 if report.passed("Extend") else (3 if caps.t2v else 0)`,
    # whose branches are identical, so a chain that demonstrably reached 6s still
    # reported 3s. The value matters: it is what `can_chain()` checks before the
    # pipeline commits to a 30-second run.
    generation_rows = [
        r for r in report.results if r.status is ProbeStatus.PASS and r.evidence.get("media")
    ]
    caps.measured_generation_s = (
        float(generation_rows[0].evidence["media"]["duration_s"]) if generation_rows else None
    )
    chain_values = [
        float(r.evidence["continuation_s"])
        for r in report.results
        if r.status is ProbeStatus.PASS and r.name.endswith("grows the film")
    ]
    caps.measured_chain_s = max(chain_values) if chain_values else caps.measured_generation_s
    caps.max_total_chain_s = round(caps.measured_chain_s) if caps.measured_chain_s else None
    caps.native_audio = any(r.evidence["media"].get("has_audio") for r in generation_rows)
    caps.evidence = {
        r.name: {
            "status": r.status.value,
            "detail": r.detail,
            "at": report.started_at,
            "evidence": r.evidence,
        }
        for r in report.results
    }

    for row in report.results:
        if row.status is ProbeStatus.BLOCKED:
            caps.notes.append(f"{row.name}: BLOCKED — {row.detail}")
        elif row.status is ProbeStatus.UNKNOWN:
            caps.notes.append(f"{row.name}: UNKNOWN — {row.detail}")

    return caps


def _sdk_version() -> str | None:
    try:
        import google.genai as genai

        return f"google-genai {genai.__version__}"
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


def render_report(report: ProbeReport) -> str:
    """The §15.1 console layout."""
    lines: list[str] = []
    width = max((len(r.name) for r in report.results), default=20)
    for row in report.results:
        lines.append(f"{row.name.ljust(width)}  {row.symbol}")
        if row.detail and row.status is not ProbeStatus.PASS:
            lines.append(f"{' ' * width}  ─ {row.detail}")
    caps = report.capabilities
    if caps is not None:
        chain = f"{caps.max_total_chain_s}s" if caps.max_total_chain_s else "unmeasured"
        lines.append(f"{'Max tested chain'.ljust(width)}  {chain}")
    return "\n".join(lines)


def render_capability_markdown(report: ProbeReport, *, redact_project: bool = True) -> str:
    """Build `CAPABILITY_REPORT.md` from actual results (§16.3).

    §16.3 asks for the project ID in sanitised form. We keep the first 4 and last
    2 characters, which is enough to tell two projects apart in a report without
    putting a full project id into a document that may be shared.

    The redaction is applied to every *detail* string too, not only to the header
    row. The auth check reports the principal it resolved, and the endpoint check
    reports the URL it reached; both contain the full project id and the service
    account address, and a report is exactly the kind of file that gets pasted
    into a ticket.
    """
    from omni_homevlog.observability.redaction import redact_string

    caps = report.capabilities
    raw_project = report.project
    project = raw_project or "-"
    if redact_project and project != "-" and len(project) > 8:
        project = f"{project[:4]}...{project[-2:]}"

    def scrub(text: str) -> str:
        """Redact credentials, then the project id, then escape for the table."""
        cleaned = redact_string(text)
        if redact_project and raw_project:
            cleaned = cleaned.replace(raw_project, project)
        return cleaned.replace("|", "\\|").replace("\n", " ")

    lines: list[str] = [
        "# CAPABILITY_REPORT",
        "",
        "Generated by `omni-vlog doctor`. Every row below is an **observed** result.",
        "Rows marked UNKNOWN or SKIPPED were not measured; they are not passes.",
        "",
        "| field | value |",
        "|---|---|",
        f"| probed_at | {report.started_at} |",
        f"| provider | `{report.provider}` |",
        f"| project (sanitised) | `{project}` |",
        f"| model | `{report.model}` |",
        f"| python | {report.python_version} |",
        f"| sdk | {report.sdk_version or 'not installed (REST transport in use)'} |",
        "",
    ]

    if report.error:
        lines += [f"> **Probe stopped early:** {report.error}", ""]

    lines += [
        "## Results",
        "",
        "| capability | status | detail |",
        "|---|---|---|",
    ]
    for row in report.results:
        lines.append(f"| {row.name} | **{row.symbol}** | {scrub(row.detail)} |")

    lines += ["", "## Derived capabilities", "", "```json"]
    if caps is not None:
        import json

        from omni_homevlog.observability.redaction import redact

        payload = json.dumps(redact(caps.model_dump(mode="json")), indent=2)
        if redact_project and raw_project:
            payload = payload.replace(raw_project, project)
        lines.append(payload)
    lines += ["```", ""]

    lines += [
        "## Interpretation",
        "",
        "- **PASS** means a request demonstrated the capability.",
        "- **BLOCKED** means we could not tell — usually quota or a timeout with an",
        "  unknown outcome. It is *not* a statement that the model lacks the feature.",
        "- **UNKNOWN / SKIPPED** means not attempted. Treat as unsupported until probed.",
        "- Preview models change without notice. Re-run `omni-vlog doctor` on any day",
        "  you intend to rely on this report (§24.6).",
        "",
        "## Chaining strategy implied by these results",
        "",
    ]

    if caps is None:
        lines.append("No capability record was produced.")
    elif caps.stateful_previous_interaction_id:
        lines.append(
            "**Strategy A** — `previous_interaction_id` works. Extensions keep "
            "server-side state and need no re-upload."
        )
    elif caps.stateful_steps_replay:
        lines.append("**Strategy B** — prior `interaction.steps` replay works.")
    elif caps.extend:
        lines.append(
            "**Strategy C** — native `extend` only. Every extension re-uploads the "
            "previous render as a video input, which costs bandwidth and time and "
            "is recorded per-artifact in the manifest."
        )
    else:
        lines.append(
            "**Strategy D** — no chaining mechanism is available on this surface. "
            "The 30-second native chain is blocked here (§8.3)."
        )

    return "\n".join(lines) + "\n"


async def recover_probe_report(provider: Any, saved: ProviderCapabilities) -> ProbeReport:
    """Resume a saved probe using GET only; never repeat an expensive seed."""
    import hashlib
    from pathlib import Path

    if (provider.provider_name, provider.project, provider.model) != (saved.provider, saved.project, saved.model):
        raise ValueError("Probe recovery must use the original provider/project/model")
    report = ProbeReport(provider=saved.provider, project=saved.project, model=saved.model, started_at=utc_now_iso())
    recovered_async = False
    for name, original in saved.evidence.items():
        evidence = dict(original.get("evidence") or {})
        row = ProbeResult(name=name, status=ProbeStatus(original.get("status", "UNKNOWN")), detail=str(original.get("detail", "")), evidence=evidence)
        if row.status is ProbeStatus.BLOCKED and evidence.get("interaction_id"):
            try:
                artifact = await provider.get_interaction(evidence["interaction_id"])
                evidence["recovery_gets"] = evidence.get("recovery_gets", 0) + 1
                evidence["new_video_calls"] = 0
                if artifact.status == "completed" and artifact.local_path and artifact.media and artifact.media.is_usable:
                    if artifact.model != saved.model:
                        raise ValueError("Recovered output belongs to a different model")
                    expected = evidence.get("requested_duration_s")
                    if expected and evidence.get("task") != "extend" and abs(artifact.media.duration_s - expected) > 0.6:
                        raise ValueError("Recovered duration does not match the original request")
                    evidence.update(media=artifact.media.model_dump(), local_path=artifact.local_path, sha256=hashlib.sha256(Path(artifact.local_path).read_bytes()).hexdigest(), status="completed", usage=artifact.usage)
                    row.status = ProbeStatus.PASS
                    row.detail = "Recovered and verified the original output by GET; no new video generation."
                    recovered_async = recovered_async or bool(evidence.get("background"))
                elif artifact.status in ("in_progress", "unknown"):
                    row.detail = f"Existing interaction remains {artifact.status}; no new video generation."
                else:
                    row.status = ProbeStatus.FAIL
                    row.detail = f"Existing interaction ended as {artifact.status}: {artifact.error_message or 'no usable output'}"
            except Exception as exc:
                row.status = ProbeStatus.BLOCKED
                row.detail = f"Read-only recovery failed: {exc}. No new generation."
        report.add(row)
    if recovered_async:
        for name in ("Async polling", "Remote recovery"):
            report.results = [r for r in report.results if r.name != name]
            report.add(ProbeResult(name=name, status=ProbeStatus.PASS, detail="Previously acknowledged background request recovered and media verified."))
    report.capabilities = _capabilities_from(report)
    report.capabilities.location = saved.location
    return report
