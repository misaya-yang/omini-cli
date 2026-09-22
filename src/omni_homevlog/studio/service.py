"""Durable manual creations, immutable versions and GET-only recovery.

Studio intentionally does not invoke the Director/Critic repair loop. Uploaded
images are locally validated, not labelled as having passed a vision review.
Each version owns a normal pinned job and a one-call budget ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, cast

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

from omni_homevlog.budget import CallKind
from omni_homevlog.config import Settings, get_settings
from omni_homevlog.errors import RequestTimeoutUnknownOutcome
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.pipeline.render_seed import record_dispatch_pending
from omni_homevlog.providers.factory import latest_capabilities
from omni_homevlog.schemas import ProjectSpec, ReferenceAsset, RenderArtifact, utc_now_iso
from omni_homevlog.storage.local import atomic_write_json, read_json
from omni_homevlog.storage.locking import job_lock

logger = logging.getLogger(__name__)


class CreationInput(BaseModel):
    request_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    brief: str = Field(min_length=1, max_length=4000)
    duration: Literal[3, 4, 5, 6, 8, 10] = 4
    aspect: Literal["16:9", "9:16"] = "16:9"
    resolution: Literal["360p", "720p", "1080p"] = "360p"
    image_mode: Literal["reference", "first_frame"] = "reference"
    provenance: Literal["owned", "licensed", "synthetic"] = "owned"


class EditInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    version: int = Field(ge=0)


class StudioService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = self.settings.data_dir() / "studio"
        self.root.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="studio")
        self.guard = threading.Lock()
        self.active: set[str] = set()
        self.stopping = threading.Event()

    def close(self) -> None:
        self.stopping.set()
        self.pool.shutdown(wait=True, cancel_futures=False)

    def path(self, creation_id: str) -> Path:
        if len(creation_id) != 32 or any(c not in "0123456789abcdef" for c in creation_id):
            raise ValueError("无效的作品编号")
        return self.root / f"{creation_id}.json"

    def load(self, creation_id: str) -> dict[str, Any]:
        path = self.path(creation_id)
        if not path.is_file():
            raise FileNotFoundError("没有找到这份作品")
        return dict(read_json(path))

    def save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = utc_now_iso()
        atomic_write_json(self.path(data["id"]), data)

    def public(self, data: dict[str, Any]) -> dict[str, Any]:
        versions = []
        for i, version in enumerate(data["versions"]):
            artifact = version.get("artifact")
            available = bool(
                artifact and artifact.get("local_path") and Path(artifact["local_path"]).is_file()
            )
            versions.append(
                {
                    "index": i,
                    "prompt": version["prompt"],
                    "status": version["status"],
                    "message": version.get("message", ""),
                    "parent": version.get("parent"),
                    "url": f"/api/creations/{data['id']}/versions/{i}/video" if available else None,
                    "duration": (artifact.get("media") or {}).get("duration_s")
                    if artifact
                    else None,
                }
            )
        return {
            **{k: data[k] for k in ("id", "title", "brief", "options", "updated_at")},
            "versions": versions,
            "busy": data["id"] in self.active,
            "references": [
                f"/api/creations/{data['id']}/references/{i}"
                for i in range(len(data["references"]))
            ],
        }

    def list_creations(self) -> list[dict[str, Any]]:
        result = []
        for path in self.root.glob("*.json"):
            try:
                result.append(self.public(dict(read_json(path))))
            except (ValueError, KeyError, OSError):
                logger.warning("Unreadable studio record: %s", path.name)
        return sorted(result, key=lambda d: d["updated_at"], reverse=True)

    def create(self, options: CreationInput, uploads: list[bytes]) -> dict[str, Any]:
        cid = options.request_id or uuid.uuid4().hex
        digest = hashlib.sha256(
            options.model_dump_json().encode()
            + b"".join(hashlib.sha256(raw).digest() for raw in uploads)
        ).hexdigest()
        with job_lock(self.root / f"create-{cid}.lock"):
            if self.path(cid).exists():
                saved = self.load(cid)
                if saved.get("request_digest") != digest:
                    raise ValueError("这次请求的内容已改变，请重新提交")
                return self.public(saved)
            return self._create(cid, digest, options, uploads)

    def _create(
        self, cid: str, digest: str, options: CreationInput, uploads: list[bytes]
    ) -> dict[str, Any]:
        if not options.brief.strip():
            raise ValueError("请先写下你想拍摄的画面")
        if len(uploads) > (1 if options.image_mode == "first_frame" else 5):
            raise ValueError("起始画面只支持 1 张图；参考图片最多 5 张")
        # Validate all files before allocating a job or dispatching any paid work.
        images = []
        for raw in uploads:
            if len(raw) > 10 * 1024 * 1024:
                raise ValueError("每张图片请控制在 10 MB 以内")
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    if (
                        image.format not in {"JPEG", "PNG", "WEBP"}
                        or image.width * image.height > 25_000_000
                    ):
                        raise ValueError("请使用不超过 2500 万像素的 JPG、PNG 或 WebP 图片")
                    image.load()
                    clean = ImageOps.exif_transpose(image).convert("RGB")
                    clean.thumbnail((2048, 2048))
                    buffer = io.BytesIO()
                    clean.save(buffer, format="JPEG", quality=92)
                    images.append((buffer.getvalue(), clean.size))
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise ValueError("图片无法读取，请换一张 JPG、PNG 或 WebP 图片") from exc
        folder = self.root / cid
        folder.mkdir()
        references = []
        for i, (raw, size) in enumerate(images):
            path = folder / f"reference-{i}.jpg"
            path.write_bytes(raw)
            references.append(
                ReferenceAsset(
                    id=f"ref-{i}",
                    path_or_uri=str(path.resolve()),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    role="first_frame" if options.image_mode == "first_frame" else "environment",
                    provenance=options.provenance,
                    approved=False,
                    width=size[0],
                    height=size[1],
                    mime_type="image/jpeg",
                ).model_dump(mode="json")
            )
        data = {
            "id": cid,
            "request_digest": digest,
            "title": options.brief.strip()[:32],
            "brief": options.brief.strip(),
            "options": options.model_dump(),
            "references": references,
            "versions": [],
        }
        self.save(data)
        self.submit(cid, prompt=options.brief.strip())
        return self.public(self.load(cid))

    def submit(
        self, cid: str, *, prompt: str = "", parent: int | None = None, recover: bool = False
    ) -> None:
        with self.guard, job_lock(self.root / f"{cid}.lock"):
            if cid in self.active:
                raise ValueError("这份作品正在处理，请稍候")
            data = self.load(cid)
            pending = data["versions"] and data["versions"][-1]["status"] in (
                "queued",
                "running",
                "pending",
                "unknown",
            )
            if recover:
                if not pending:
                    raise ValueError("当前没有需要恢复的任务")
            else:
                if pending:
                    raise ValueError("上一次生成尚未确认结果，请先查询进度，避免重复付费")
                if not prompt.strip():
                    raise ValueError("请填写修改描述")
                if parent is not None and (
                    parent < 0
                    or parent >= len(data["versions"])
                    or not data["versions"][parent].get("artifact")
                ):
                    raise ValueError("请先选择一个已完成的视频版本")
                data["versions"].append(
                    {"prompt": prompt.strip(), "parent": parent, "status": "queued"}
                )
                self.save(data)
            self.active.add(cid)
            self.pool.submit(self._work, cid, recover)

    def _work(self, cid: str, recover: bool) -> None:
        try:
            with job_lock(self.root / f"{cid}.lock"):
                self._step(cid, recover)
                if not recover:
                    for _ in range(20):
                        if self.load(cid)["versions"][-1][
                            "status"
                        ] != "pending" or self.stopping.wait(15):
                            break
                        self._step(cid, True)
        finally:
            with self.guard:
                self.active.discard(cid)

    def _step(self, cid: str, recover: bool) -> None:
        try:
            with job_lock(self.root / f"{cid}.lock"):
                data = self.load(cid)
                version = data["versions"][-1]
                if recover:
                    artifact = self._recover(data, version)
                else:
                    version["status"] = "running"
                    version["message"] = "正在生成视频，通常需要几分钟。可以切换作品或关闭页面。"
                    self.save(data)
                    artifact = self._render(data, version)
                if artifact is not None:
                    if artifact.status == "completed" and artifact.local_path:
                        version["artifact"] = artifact.model_dump(mode="json")
                        version["status"] = "ready"
                        version["message"] = "视频已就绪，可以预览、下载或继续修改。"
                    elif artifact.status in ("failed", "cancelled", "incomplete"):
                        version["status"] = "failed"
                        version["message"] = "服务端未完成视频：" + (
                            artifact.error_message or artifact.status
                        )
                    else:
                        version["status"] = "pending"
                        version["message"] = "服务端仍在处理。查询进度不会重新生成。"
                self.save(data)
        except RequestTimeoutUnknownOutcome as exc:
            data = self.load(cid)
            version = data["versions"][-1]
            version["interaction_id"] = exc.interaction_id
            version["status"] = "pending" if exc.interaction_id else "unknown"
            version["message"] = (
                "生成已提交，正在等待结果。查询进度不会重新生成。"
                if exc.interaction_id
                else "请求结果尚未确认，已暂停，避免重复付费。请检查本地任务记录。"
            )
            self.save(data)
        except Exception as exc:
            logger.exception("Studio operation failed")
            data = self.load(cid)
            version = data["versions"][-1]
            # A reserved request without a result must never become retryable.
            from omni_homevlog.errors import OmniVlogError

            uncertain = bool(version.get("dispatched")) and (
                not isinstance(exc, OmniVlogError) or exc.outcome_unknown or recover
            )
            version["status"] = "unknown" if uncertain else "failed"
            version["message"] = (
                "请求结果尚未确认，请查询进度。" if uncertain else "操作未完成。"
            ) + self._error_message(exc)
            self.save(data)

    @staticmethod
    def _error_message(exc: Exception) -> str:
        code = getattr(exc, "code", "")
        return {
            "missing_credentials": "本机尚未登录 Google，请运行 gcloud auth application-default login。",
            "auth_error": "Google 登录已过期，请重新登录。",
            "permission_denied": "当前账号没有模型访问权限。",
            "quota_exhausted": "当前模型配额已用完，请稍后再试。",
            "budget_exhausted": "已达到本次调用预算，没有继续提交。",
            "capability_missing": "当前模型尚未验证这项能力，请先完成对应的 doctor 检查。",
        }.get(code, "请查看终端日志中的错误详情。")

    def _render(self, data: dict[str, Any], version: dict[str, Any]) -> RenderArtifact:
        options = CreationInput.model_validate(data["options"])
        parent = version["parent"]
        source = (
            RenderArtifact.model_validate(data["versions"][parent]["artifact"])
            if parent is not None
            else None
        )
        spec = ProjectSpec(
            title=data["title"],
            brief=version["prompt"],
            provider=self.settings.omni_provider,
            project=source.project if source else None,
            model=source.model if source else None,
            target_duration_s=10,
            resolution=options.resolution,
            aspect_ratio=options.aspect,
            max_total_calls=1,
            human_gates=[],
            background=True,
        )
        if source:
            source_ctx = JobContext.load(data["versions"][parent]["job_id"], settings=self.settings)
            spec.location = source_ctx.spec.location
            spec.provider = source.provider  # type: ignore[assignment]
        ctx = JobContext.create(spec=spec, settings=self.settings)
        caps = latest_capabilities(
            provider=ctx.binding.provider_name,
            project=ctx.binding.project,
            model=ctx.binding.model,
            settings=self.settings,
        )
        ctx.provider.capabilities = caps
        from omni_homevlog.errors import CapabilityMissingError

        if source and (not caps or not caps.edit):
            raise CapabilityMissingError("Edit capability has not been verified")
        version["job_id"] = ctx.job_id
        self.save(data)
        seconds = (
            round(source.media.duration_s)
            if source and source.media and source.media.duration_s
            else options.duration
        )
        ctx.authorize(
            CallKind.EDIT if source else CallKind.SEED,
            segment_index=0,
            attempt_index=0,
            video_seconds=seconds,
        )
        record_dispatch_pending(
            ctx,
            segment_index=0,
            attempt_index=0,
            task="edit" if source else "reference_to_video",
            parent_interaction_id=source.interaction_id if source else None,
        )
        version["dispatched"] = True
        self.save(data)
        if source:
            return cast(
                RenderArtifact,
                asyncio.run(
                    ctx.provider.edit(
                        artifact=source,
                        edit_prompt=version["prompt"],
                        spec=ctx.spec,
                        attempt_index=0,
                    )
                ),
            )
        refs = [ReferenceAsset.model_validate(r) for r in data["references"]]
        return cast(
            RenderArtifact,
            asyncio.run(
                ctx.provider.generate_seed(
                    prompt=version["prompt"],
                    assets=refs,
                    spec=ctx.spec,
                    duration_s=options.duration,
                )
            ),
        )

    def _recover(self, data: dict[str, Any], version: dict[str, Any]) -> RenderArtifact | None:
        if not version.get("job_id"):
            version["status"] = "failed"
            version["message"] = "任务尚未提交时程序退出，请重新生成。"
            return None
        ctx = JobContext.load(version["job_id"], settings=self.settings)
        interaction_id = version.get("interaction_id")
        if not interaction_id:
            rows = ctx.manifest.interactions
            if rows and not rows[-1].interaction_id.startswith("pending-"):
                interaction_id = rows[-1].interaction_id
        if not interaction_id or str(interaction_id).startswith("pending-"):
            version["status"] = "unknown"
            version["message"] = "未获得服务端任务编号，无法自动确认结果。已停止，避免重复付费。"
            return None
        version["interaction_id"] = interaction_id
        artifact = cast(RenderArtifact, asyncio.run(ctx.provider.get_interaction(interaction_id)))
        artifact.prompt = version["prompt"]
        artifact.prompt_sha256 = hashlib.sha256(version["prompt"].encode()).hexdigest()
        artifact.aspect_ratio = ctx.spec.aspect_ratio
        artifact.resolution = ctx.spec.resolution
        parent = version["parent"]
        artifact.task = (
            "edit"
            if parent is not None
            else ("reference_to_video" if data["references"] else "text_to_video")
        )
        if parent is not None:
            artifact.parent_interaction_id = data["versions"][parent]["artifact"]["interaction_id"]
        return artifact

    def media(self, cid: str, index: int, *, reference: bool = False) -> Path:
        data = self.load(cid)
        try:
            raw = (
                data["references"][index]["path_or_uri"]
                if reference
                else data["versions"][index]["artifact"]["local_path"]
            )
        except (IndexError, KeyError) as exc:
            raise FileNotFoundError("视频或图片尚未就绪") from exc
        path = Path(raw).resolve()
        if not path.is_relative_to(self.settings.data_dir().resolve()) or not path.is_file():
            raise FileNotFoundError("本地文件不存在")
        return path
