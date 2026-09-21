# omni-homevlog

A continuity-preserving **home-vlog video agent** built on Gemini Omni through the
Interactions API.

It exists to solve one specific problem: producing a 30-second, single-take,
boyfriend-POV home video in which **the same person stays the same person** across
three ten-second segments. Naive pipelines fail at this because they generate
three independent clips and splice them together — the face drifts, the room
changes, and the joins read as jump cuts.

The core loop is:

```text
clean reference images
  -> continuity plan
  -> 10s seed render
  -> automated review
  -> bounded edit or regenerate
  -> native extension
  -> review again
  -> native extension
  -> human approval
```

Success is not measured in generations produced. It is measured in: same person,
same space, continuous motion, no timestamps or UI burned into the frame, no
slideshow feel, bounded spend, and the ability to resume at any stage.

---

## Status of this build

| area | state |
|---|---|
| Vertex / Agent Platform adapter | Implemented against the **verified** REST interface |
| Gemini Developer API adapter | Implemented, **unverified** — probe it before trusting it |
| Full pipeline (plan → render → review → repair → extend → finalize) | Implemented |
| Sanitizer, Director, Prompt Compiler, Critic, decision policy | Implemented |
| Budget guard, state machine, SQLite + manifest lineage, resume | Implemented |
| ffmpeg-dependent steps (keyframes, transcode) | Degrade explicitly when ffmpeg is absent |

Run `omni-vlog doctor` and read `CAPABILITY_REPORT.md` before relying on any
capability. This project treats an unprobed capability as unsupported.

---

## Install

Requires Python 3.12+. `ffmpeg` is optional but recommended — without it,
keyframe-level review and transcoding are unavailable (the tool says so rather
than pretending otherwise).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"

cp .env.example .env
```

### Credentials

Vertex uses **Application Default Credentials**. The program never shells out to
`gcloud` per request, never reads a service-account JSON itself, and never prints
a token.

```bash
gcloud auth application-default login
# or, with service-account impersonation:
gcloud auth application-default login \
  --impersonate-service-account=<your-vertex-sa>@<project>.iam.gserviceaccount.com
```

Minimum IAM: `roles/aiplatform.user` on the project, plus bucket-scoped
`roles/storage.objectCreator` if you use GCS output. Do not grant Owner or Editor.

Set your project:

```bash
export GOOGLE_CLOUD_PROJECT=your-project
export OMNI_VERTEX_MODEL=gemini-omni-1.1-flash-preview
```

The Gemini Developer API surface additionally needs `GEMINI_API_KEY`.

---

## Quickstart

```bash
# 1. Free checks first: auth, routing, and what the surface can do.
omni-vlog doctor --provider vertex --project "$GOOGLE_CLOUD_PROJECT"

# 2. Optional: spend a little to measure generation capabilities.
#    This writes CAPABILITY_REPORT.md from the results.
RUN_LIVE_VIDEO_TESTS=1 omni-vlog doctor --provider vertex --run-generation

# 3. Sanitize reference photos BEFORE generating anything.
omni-vlog check-references \
  --reference refs/face.png --reference refs/body.png --reference refs/room.png

# 4. Create a job.
omni-vlog create \
  --provider vertex --project "$GOOGLE_CLOUD_PROJECT" \
  --reference refs/face.png --reference refs/body.png --reference refs/room.png \
  --brief "30-second intimate boyfriend-POV home vlog: she drinks tea at the table, walks to the bedroom, sits on the bed, covers the lens." \
  --duration 30 --aspect 9:16 --resolution 720p

# 5. Inspect, resume, approve, export.
omni-vlog status JOB_ID
omni-vlog review JOB_ID
omni-vlog resume JOB_ID
omni-vlog approve JOB_ID --stage final
omni-vlog retry JOB_ID --stage segment-1 --mode edit
omni-vlog export JOB_ID --output ./exports/final.mp4

# 6. Optional: a higher-resolution pass, which is a NEW job (see below).
omni-vlog high-res JOB_ID --resolution 1080p --confirm-understanding

# 7. Optional: catch the bucket up if a mirror failed or a bucket was added later.
omni-vlog sync JOB_ID
```

`OMNI_OUTPUT_GCS_URI` is optional for a first look and close to mandatory for real
work: reference photos are staged there at dispatch, and the native extension chain
needs it to hand each render back as a video input. Without it, references go inline
as base64 (documented but unverified) and strategy C chaining cannot run.

---

## The six things that actually matter

Most of this codebase is ordinary plumbing. These are the parts where getting it
wrong produces a video that looks fine in isolation and wrong as a whole.

### 1. Reference images are positionally bound, and contaminating ones are fatal

A storyboard or nine-panel collage used as a character reference teaches the model
to render **panel borders, shot numbers, and timecodes into the video**. The
failure is silent and expensive. So:

- `pipeline/intake.py` runs a local grid/letterbox/duplicate detector
  (`media/inspect_image.py`) and then a vision-model check.
- Hard-reject conditions (§6.2): a detected timestamp, player UI, collage used as
  an identity reference, multiple distinct people, or an unverifiable adult
  subject.
- Reference order is fixed by `providers/request_builder.order_references()`, and
  the prompt's `[# References <IMAGE_REF_0>@Image1 ...]` header is generated from
  that same order, so the prompt's indices always match the `input` positions.

### 2. The 30 seconds is a native chain, not a splice

`media/transcode.concat_native_chain()` refuses to run unless the caller asserts
`confirmed_single_chain=True`. That flag is a deliberate speed bump: if you are
reaching for it to get a 30-second file out of three unrelated renders, that is
exactly the failure it exists to stop (§24.3).

Chaining strategy is chosen from probed capabilities, in the plan's order:

| | strategy | cost |
|---|---|---|
| A | `previous_interaction_id` | cheapest, server-side state |
| B | prior `interaction.steps` replay | moderate |
| C | native `extend` fed the prior render's GCS URI | re-uploads video each time |
| D | none available | **chain blocked**, reported |

The strategy in use is recorded per artifact, so the manifest shows which one
produced the file.

**Measured caveat on strategy A.** The plan lists `previous_interaction_id` as the
preferred mechanism without noting that it conflicts with an explicit video task.
On Vertex, sending both is a hard 400:

```
invalid_request: previous_interaction_id is not allowed when video task is set.
```

A continuation therefore drops `generation_config` entirely and lets
`response_format` declare the video. Verified live on 2026-09-21: a 3.008s seed
chained to a **6.016s** film. `VertexEnterpriseProvider.build_payload` applies this
automatically, and `apply_continuation_rule=False` reverts it if the surface
changes. See `docs/LIMITATIONS.md` for the full measurement.

### 3. A timeout is not a retry

If a render request times out, the generation **may already exist and may be
billable**. The handoff is explicit and the plan agrees: do not re-issue. The
request is recorded as an interaction with `outcome_known=False`, the job moves to
`NEEDS_HUMAN`, and `omni-vlog resume` resolves it by *querying* the interaction
(read-only, free) rather than re-rendering.

`errors.RequestTimeoutUnknownOutcome` carries `retryable = False` so this cannot
be undone by an over-eager retry wrapper.

### 4. The Critic advises; deterministic code decides

`agents/critic.py` produces a `CritiqueReport` with scores and a suggested verdict.
That verdict is advisory. `agents/decision_policy.py` re-derives the decision from
the report against configured thresholds, and it is the only thing that can
authorise spending. §24.8: a model's free-text suggestion must never execute a
paid call.

Review input is also gated on completeness: if keyframes could not be extracted,
the report is marked degraded and **cannot** be auto-accepted, because a Critic
that saw one frame and reports a `motion_score` is worse than one that admits it
could not look.

### 5. Budget is arithmetic, checked before every call

Every paid call goes through `Budget.authorize()` (`budget.py`), which is pure
counter arithmetic over a fixed ceiling. There is no heuristic path and no model
consulted. Ceilings are per job, per segment, per call kind, and optionally in
dollars.

### 6. State lives in two places and they must agree

Every transition is written atomically to `manifest.json` **and** the SQLite index
(`storage/manifest.py:transition`). The manifest is authoritative; the database is
a derived index and `rebuild_from_manifest()` repairs it. Illegal transitions
raise — there is no force flag.

`manifest.segments` holds the **accepted chain**: at most one artifact per planned
segment. A repair replaces its segment's artifact rather than appending a fourth. The
superseded attempt is not lost — it stays in `interactions` (the full ledger) and in
the database's `artifacts` table, with its file still on disk under
`renders/segment_NN/`. Keeping those two apart is what makes
`verify_chain_continuity()` a meaningful check instead of a list of every render ever
attempted.

### 7. A higher-resolution pass is a new job

`omni-vlog high-res` creates a *separate job* rather than re-rendering in place.
§24.12 forbids presenting a regenerated 1080p file as the same content as the 360p
draft, and separate jobs are the only structure that keeps the two generations'
lineage honestly apart. Each manifest names the other.

---

## Configuration

Precedence: CLI flags > environment > `configs/default.yaml` > code defaults.

| file | holds |
|---|---|
| `.env` | project, bucket, keys, switches *(never committed)* |
| `configs/default.yaml` | model ids, timeouts, non-secret defaults |
| `configs/quality_thresholds.yaml` | decision-policy thresholds |
| `pricing.yaml` | cost-estimate rates + their staleness date |

**Cost figures are estimates.** `pricing.yaml` ships with zeroed rates because the
real numbers could not be verified at build time; the manifest records token counts
regardless, so a reconciliation against Cloud Billing is always possible.
`omni-vlog status` says "estimated" wherever it shows money.

### Thresholds are policy, not measurement

`configs/quality_thresholds.yaml` carries `calibrated_at` and a note saying the
values are uncalibrated. The plan is explicit that these must not be presented as
an objective quality standard, and the Critic's scores are heuristic judgements
from a vision model. Tune them against real samples, then update the note.

---

## Layout

```
src/omni_homevlog/
  cli.py              CLI (typer)
  config.py           settings + thresholds
  schemas.py          Pydantic v2 contracts (the plan's §7 models)
  errors.py           error taxonomy — each error knows its own retryability
  state_machine.py    job transitions, plain Python
  budget.py           call/seconds/cost ceilings
  costing.py          estimates only
  providers/
    auth.py                  ADC, never a token out
    transport.py             HTTP + SSE, retry discipline
    request_builder.py       payload construction (pure, testable)
    response_parser.py       SSE / JSON / SDK → InteractionEnvelope
    capability_probe.py      the Phase 0 matrix behind `doctor`
    vertex_enterprise.py     verified surface
    gemini_api.py            unverified surface
    factory.py               project binding, no implicit switching
  agents/             director, critic, decision_policy
  prompts/            compiler + seed/extend/edit/critic templates
  pipeline/           intake, plan, render_seed, review, repair, extend, finalize, resume
  media/              mp4_probe (pure Python), ffprobe, inspect_image, extract_frames, transcode, c2pa
  storage/            local, gcs, database, manifest
  observability/      logging + redaction
```

### Job data on disk

```
.omni-vlog/jobs/<job_id>/
  input/references/        input/sanitizer/
  plans/                   project_spec.json, continuity_bible.json, segment_plan.json
  renders/segment_00/      attempt_00_raw.mp4, attempt_01_edit.mp4
  reviews/                 segment_00_attempt_00.json
  debug/                   redacted raw provider exchanges
  final/                   final.mp4, manifest.json
  job.db                   SQLite index
```

This mirrors the GCS layout so a job can move between them without rewriting paths.

---

## Safety and privacy

- Reference images must be owned, licensed, or synthetic. `provenance` is recorded
  per asset and `unknown` provenance blocks identity replication of a real person.
- Buckets are assumed private. No public URLs are generated.
- Logs, debug fixtures, and CLI output pass through `observability/redaction.py`.
  There is no un-redacted output path.
- **C2PA is detected and never stripped** (§24.13). `synthid_expected` is recorded
  as an *expectation*, never as a detection we cannot make.
- Any ffmpeg transform sets `derived=True` and records C2PA state before and after,
  so a dropped manifest is visible rather than silent.
- `omni-vlog delete JOB_ID --yes` removes GCS objects first, then local files and
  DB rows. If any GCS delete fails it stops without touching local data, so the
  command is safe to re-run and cannot leave you believing material is gone when
  part of it is still in a bucket.
- Stages mirror what they change to GCS as they run (`plans/`, `reviews/`,
  `final/`, and the manifest). A mirror failure is recorded on the manifest and
  never fails the job, because the local copy is authoritative and a render that
  succeeded should not be lost to a bucket problem. `omni-vlog sync JOB_ID` is the
  catch-up command.

---

## Deliberately not built

Per the plan's constraints — these are decisions, not omissions:

- No LangGraph. A plain Python state machine is easier to audit (§24.1).
- No web UI, FastAPI, or queue. Phase 0–3 come first (§20).
- No multi-character plots, lip-sync dialogue, or automatic music editing.
- No model training or fine-tuning.
- No automatic project or key switching to work around quota (§24.4, §24.5).
- No automatic publishing to social platforms.

---

## Testing

```bash
pytest                      # unit + mock integration; no network, no spend
RUN_LIVE_VIDEO_TESTS=1 pytest tests/live   # opt-in, costs money
```

Live tests are opt-in at two levels: the environment variable *and* an explicit
`--run-generation` flag on `doctor`. Neither is set by default.

Mock integration tests cover the failure modes that matter: 429, 500, background
→ completed, interaction-completed-but-download-failed, lost interaction id, GCS
permission denied, multiple `model_output` steps, `model_output` without video, and
policy blocks.

`tests/fixtures/quality_regression_set.yaml` holds the §21.4 regression set: five
fixed scenes. Its integrity is checked on every `pytest` run, and it can render the
scenes on demand for a manual A/B after a model or SDK change. See
`tests/fixtures/README.md`.

```bash
RUN_LIVE_VIDEO_TESTS=1 OMNI_RUN_REGRESSION=1 pytest tests/live/test_quality_regression.py
```

Two other scripts sit outside the test suite:

```bash
python scripts/doctor.py --project "$GOOGLE_CLOUD_PROJECT"      # capability probe
RUN_LIVE_VIDEO_TESTS=1 python scripts/smoke_render.py --project "$GOOGLE_CLOUD_PROJECT"
```

`smoke_render.py` is the smallest thing that proves the paid path works: one
request, then verification of the resulting *file* — duration, dimensions, codec,
audio, and C2PA. It refuses to run without the opt-in.

---

## Known limitations

See [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) for the full list, including:

- Preview-model behaviour is not a stable protocol; re-probe before relying on it.
- 1080p/4K are upscales. A re-render at 1080p is **not** the same content as the
  360p draft, and the plan forbids presenting it as such (§24.12).
- `synthid_expected` cannot be verified locally.
- The gemini_api surface is unverified end-to-end in this project.