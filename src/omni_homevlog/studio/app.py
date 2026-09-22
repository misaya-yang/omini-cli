"""Loopback-only HTTP interface; never exposes credentials or arbitrary paths."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from omni_homevlog.studio.service import CreationInput, EditInput, StudioService


def create_app(service: StudioService | None = None) -> FastAPI:
    svc = service or StudioService()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        svc.close()

    app = FastAPI(title="Omni Studio", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def local_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if request.headers.get("x-studio-request") != "1" or (
                origin and urlsplit(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse({"detail": "请从本地工作台提交操作"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def bad_input(request: Request, exc: ValueError) -> JSONResponse:
        message = (
            "输入格式不正确，请检查选项"
            if isinstance(exc, (ValidationError, json.JSONDecodeError))
            else str(exc)
        )
        return JSONResponse({"detail": message}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def not_found(request: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.get("/api/config")
    def config() -> dict[str, str]:
        return {
            "provider": svc.settings.omni_provider,
            "mode": "local",
            "generation_policy": "one-call-per-version",
        }

    @app.get("/api/creations")
    def creations() -> list[dict]:  # type: ignore[type-arg]
        return svc.list_creations()

    @app.get("/api/creations/{cid}")
    def creation(cid: str) -> dict:  # type: ignore[type-arg]
        return svc.public(svc.load(cid))

    @app.post("/api/creations", status_code=202)
    async def create(options: str = Form(...), images: list[UploadFile] = File(default=[])) -> dict:  # type: ignore[type-arg]
        parsed = CreationInput.model_validate_json(options)
        if len(images) > 5:
            raise HTTPException(400, "最多上传 5 张参考图片")
        content = []
        for image in images:
            raw = await image.read(10 * 1024 * 1024 + 1)
            await image.close()
            if len(raw) > 10 * 1024 * 1024:
                raise HTTPException(400, "每张图片请控制在 10 MB 以内")
            content.append(raw)
        return svc.create(parsed, content)

    @app.post("/api/creations/{cid}/edit", status_code=202)
    def edit(cid: str, body: EditInput) -> dict:  # type: ignore[type-arg]
        svc.submit(cid, prompt=body.prompt, parent=body.version)
        return svc.public(svc.load(cid))

    @app.post("/api/creations/{cid}/recover", status_code=202)
    def recover(cid: str) -> dict:  # type: ignore[type-arg]
        svc.submit(cid, recover=True)
        return svc.public(svc.load(cid))

    @app.get("/api/creations/{cid}/versions/{index}/video")
    def video(cid: str, index: int, download: bool = False) -> FileResponse:
        if index < 0:
            raise HTTPException(404)
        return FileResponse(
            svc.media(cid, index),
            media_type="video/mp4",
            filename=f"omni-{cid[:8]}-v{index + 1}.mp4" if download else None,
        )

    @app.get("/api/creations/{cid}/references/{index}")
    def reference(cid: str, index: int) -> FileResponse:
        if index < 0:
            raise HTTPException(404)
        return FileResponse(svc.media(cid, index, reference=True), media_type="image/jpeg")

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="studio")
    else:

        @app.get("/")
        def missing_build() -> JSONResponse:
            return JSONResponse(
                {"detail": "请先在 ui 目录运行 npm ci && npm run build"}, status_code=503
            )

    return app
