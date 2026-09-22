# Omni HomeVlog

本地视频生成工作台：选图、写创意、生成预览、按文字修改、切换版本和下载。底层保留完整的 CLI 自动规划与连续视频流程。

## 直接开始

本机已安装依赖后，双击项目里的 **`启动工作台.command`**，或运行：

```bash
.venv/bin/omni-vlog studio
```

打开 `http://127.0.0.1:8765`。默认 4 秒 / 360p，支持 3–10 秒、横竖屏和分辨率选择。每次生成或修改只提交一次视频请求，不自动重拍，不调用额外的文案/审片模型。结果等待期间可关闭网页，保留终端运行；重启服务后可在作品中查询已有任务。

工作台直接使用已有 `.env` / ADC 配置。选择的图片会在本机检查、纠正方向并缩小到最长边 2048 像素；生成时才发送给配置的模型。只做本地素材检查，不声称通过了 AI 人物审查。详细操作、恢复和测试范围见 [工作台使用说明](docs/STUDIO.md)。

## Current evidence

| Surface | Evidence |
|---|---|
| Vertex REST + ADC | Real 3.029 → 6.037 → 9.024 second native chain; local edit preserved 9.024 seconds |
| Video Critic | One real video-plus-keyframes call completed; quality scores remain heuristic |
| Remote recovery | Vertex stream=true GET restored the real 9.024s output with identical SHA256; asynchronous 3s reference render also recovered |
| Reference/person workflow | Real 3.029s reference-to-video accepted technically; person continuity not claimed |
| Gemini Developer API | Adapter and offline tests exist; no live acceptance claim |
| Local workbench | Browser upload → preview → edit → version switch → download tested with local fixtures; no paid UI retries |
| CLI, budget, locks, lineage, review/export | Offline regression coverage; details in acceptance record |

## Install

Python 3.12+, macOS/Linux. ffmpeg/ffprobe enable complete video review.

```bash
uv venv --python 3.12
uv pip install -r requirements-dev.lock
uv pip install -e .
brew install ffmpeg                 # macOS; use your package manager on Linux
cp .env.example .env
```

`requirements-dev.lock` records the tested dependency versions. The optional google-genai SDK is not required for the REST transport. See [SETUP](docs/SETUP.md) for credentials and migration.

Vertex uses Application Default Credentials:

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-project
```

Gemini API additionally requires `GEMINI_API_KEY`. Credentials stay out of version control. Configure a private GCS prefix only if needed; measured previous_interaction_id chaining itself needs no bucket.

## Probe before creation

```bash
# Free auth/routing check. Does not prove video generation.
omni-vlog doctor --provider vertex

# Paid smoke: at most four video calls, one shared chain.
# Seed and appends request 3s/360p; edit requests the full source duration.
RUN_LIVE_VIDEO_TESTS=1 omni-vlog doctor --provider vertex \
  --run-generation --checks seed,extend,third,edit --max-calls 4

# Separate input-mode probe, only when needed and with a clean reference:
RUN_LIVE_VIDEO_TESTS=1 omni-vlog doctor --provider vertex \
  --run-generation --checks seed --max-calls 1 --reference refs/room.png
```

Paid tests are optional and bounded. Do not run all examples simply to install the app.
Reports distinguish PASS/FAIL/BLOCKED/SKIPPED. A continuation must produce a verified growing video to qualify. The SQLite capability snapshot is keyed to the selected surface; free probes preserve measured capabilities. No unknown capability is silently treated as supported.

## Create and operate a job

```bash
omni-vlog check-references --reference refs/face.png --reference refs/body.png

omni-vlog create --provider vertex \
  --reference refs/face.png --reference refs/body.png --provenance owned \
  --brief "A continuous home vlog: drinks tea, walks to the bedroom, covers the lens." \
  --duration 30 --aspect 9:16 --resolution 720p --human-gate final --max-calls 5

omni-vlog status JOB_ID --json
omni-vlog review JOB_ID
omni-vlog approve JOB_ID --stage final
omni-vlog resume JOB_ID
omni-vlog export JOB_ID --output ./exports/final.mp4
```

Creation is paid work and begins immediately after validation. A reference-to-video capability measurement is required for reference inputs. A short probe establishes API mechanics; it does not certify a 30-second person-consistent result.

For a short concept attempt, use `--mode concept`; it produces one short seed, never a production extension chain. Each job stays on its original provider/project/model. A 1080p pass is a new generation, not an identical upgrade of a 360p draft.

The final file is the latest **cumulative** native output. Ten-, twenty- and thirty-second versions are not added together or concatenated. Formal export requires COMPLETE and writes a companion `.manifest.json`. Review can inspect intermediate artifacts without bypassing approval.

## Cost and recovery

Video calls, requested seconds, per-segment repairs and optional video-dollar estimates are bounded. Counters and reservations survive restarts. Default pricing is unknown, not free; a dollar ceiling requires valid rates in `pricing.yaml`. Text/vision calls are separately limited and audited (default 24 per job), including retries. Cloud Billing remains authoritative.

Video POST is never automatically repeated after timeout or 5xx. Server IDs are checkpointed before downloads, and complete local outputs get SHA256 receipts. Resume can use a verified receipt, query the existing interaction, or fetch its output. A failed remote query remains unresolved and never becomes a fresh generation.

A local task lock protects mutating commands against simultaneous processes. It does not provide multi-machine distributed locking.

## Review and artifacts

Director and Critic use an independent text/vision model. Critic receives references, continuity constraints, keyframes and video/audio bytes when available. A missing or over-18MB video, unavailable frames or failed model response is marked degraded; deterministic policy decides whether to continue. Model prose cannot grant extra spending.

Reference uploads retain the local copy for later reviews. Timestamp/UI/collage checks, role binding and provenance are enforced by intake. C2PA boxes are inspected and originals preserved; SynthID is only an expectation, not a locally verified watermark.

Job files live under `.omni-vlog/jobs/JOB_ID/`: input, plans, renders, reviews, debug receipts, SQLite, and `final/manifest.json`. Original and superseded attempts remain available. GCS mirroring and cleanup are optional; private materials and raw run directories are ignored by Git.

## Tests

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
.venv/bin/mypy src
```

Offline tests isolate developer configuration and block real HTTP. Mock renders use cumulative durations. Coverage includes native-chain accounting, capability evidence, recoverable downloads, budget reservations, current-version reviews, final approval/export, CLI retry and process locks.

The opt-in smoke suite shares **one** generated sample across its checks:

```bash
RUN_LIVE_VIDEO_TESTS=1 pytest tests/live/test_smoke_render.py
```

The five fixed quality scenarios are in `tests/fixtures/quality_regression_set.yaml`. Their separate paid regression suite is not part of ordinary CI or the limited-cost acceptance run.

No web UI, queue, multi-tenant billing, automatic publishing, project rotation or independent-clip splicing is included.
