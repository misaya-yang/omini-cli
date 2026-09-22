# Example project: a synthetic 30-second home vlog

This directory is a worked example. It shows the full command sequence for one job,
explains what each command writes to disk, and ships a schema-valid manifest.

**Read this first.**

- The subject is **fictional**. "Mira" exists only as a synthetic reference image. No
  real person, no real likeness, and no private data is used or described here.
- **This directory contains no image files.** The reference images are yours to
  supply, and they must be synthetic (or owned, or licensed) to pass the sanitizer.
- `manifest.example.json` is a **hand-built example**. It was built to match
  `src/omni_homevlog/schemas.py::Manifest` exactly, and it validates against that
  model, but it is not the output of a real run. Interaction ids, GCS URIs, file paths,
  and latencies in it are illustrative.
- The project id, bucket, and paths throughout are placeholders
  (`example-homevlog-project`, `gs://example-homevlog-output/omni-output/`,
  `/srv/omni-vlog/`). None of them exist.

---

## The example project

| field | value |
|---|---|
| subject | Mira, a fictional adult woman, synthetic reference images only |
| brief | 30-second intimate boyfriend-POV home vlog: tea at the kitchen table, walk to the bedroom, sit on the bed, cover the lens |
| target duration | 30 seconds, as a native chain of 10s + 10s + 10s |
| aspect ratio | 9:16 |
| resolution | 720p |
| mode | production |
| provider | vertex |
| delivery | GCS (`delivery: "uri"` with a `gcs_uri` prefix) |
| human gates | `high-res`, `final` |
| budget | 8 calls, 90 video seconds, no dollar ceiling |

---

## Before you start

```bash
gcloud auth application-default login

export GOOGLE_CLOUD_PROJECT=example-homevlog-project
export OMNI_OUTPUT_GCS_URI=gs://example-homevlog-output/omni-output/
```

Set `OMNI_OUTPUT_GCS_URI` to a bucket you are approved to write to. Extensions need
the previous render reachable by the provider, and without a bucket the inline-delivered
render has no address to reference.

---

## The command sequence

### 1. Measure the surface before trusting it

```bash
python scripts/doctor.py --provider vertex --project "$GOOGLE_CLOUD_PROJECT" --location global
# or, once installed:
omni-vlog doctor --provider vertex --project "$GOOGLE_CLOUD_PROJECT" --location global
```

Free checks only. It prints the §15.1 table and writes `CAPABILITY_REPORT.md`. Every
row is an observed result: PASS means a request demonstrated it, BLOCKED means the
probe could not tell (usually quota), and UNKNOWN or SKIPPED means not attempted.

Add `--run-generation` (and `RUN_LIVE_VIDEO_TESTS=1`) to measure the paid rows at the
cheapest settings, 3 seconds at 360p with no people. That spends money.

Read the report before creating a job. A capability that was not probed is not
supported, and the chaining strategy the job will use is derived from it.

### 2. Sanitize the references, which costs nothing

```bash
omni-vlog check-references \
  --reference refs/00_identity_closeup.png \
  --reference refs/01_identity_body.png \
  --reference refs/02_environment.png \
  --role identity_closeup --role identity_body --role environment \
  --provenance synthetic
```

Local checks plus one vision pass. This is the step that stops a storyboard, a
timestamp, or a second face from being burned into the render, so it runs before any
spend. §6.2's hard-reject conditions are not advisory: `require_approved` raises.

**Pass `--provenance synthetic`.** The default is `unknown`, and an identity reference
with `unknown` provenance is rejected, because replicating an identifiable real person
from an unverified source is not allowed. For a synthetic subject, say so.

### 3. Create the job

```bash
omni-vlog create \
  --provider vertex --project "$GOOGLE_CLOUD_PROJECT" \
  --reference refs/00_identity_closeup.png \
  --reference refs/01_identity_body.png \
  --reference refs/02_environment.png \
  --role identity_closeup --role identity_body --role environment \
  --provenance synthetic \
  --title "Synthetic 30-second boyfriend-POV home vlog" \
  --brief "30-second intimate boyfriend-POV home vlog: she drinks tea at the kitchen table, walks to the bedroom, sits on the bed, covers the lens." \
  --duration 30 --aspect 9:16 --resolution 720p \
  --mode production \
  --gcs-uri "$OMNI_OUTPUT_GCS_URI" \
  --human-gate high-res,final \
  --max-calls 8
```

This sanitizes the references again, pins `provider`, `project`, `model`, and `location`
on the spec for the life of the job (§8.3), then drives the job forward until it stops.
It stops at a human gate, at a budget ceiling, at a `NEEDS_HUMAN` decision, or when a
segment times out with an unknown outcome. It prints the job id, which every later
command needs.

`--max-calls 8` is the ceiling for a 30-second production job
(`2 + 2*extensions + 2`). Raising it does not help, because `budget_for_mode` takes the
minimum.

### 4. Inspect

```bash
omni-vlog status JOB_ID
omni-vlog status JOB_ID --json     # the same data, machine-readable
```

Prints the state, the pinned provider and model, the chain table (one row per accepted
segment: interaction id, task, parent, seconds, resolution), the chain total, the
budget position, any unresolved interactions, the final path, and the last few notes
and errors. Money is always labelled an estimate, because it is.

### 5. Read the reviews

```bash
omni-vlog review JOB_ID
omni-vlog review JOB_ID --show 1   # the full JSON report for segment 1
omni-vlog review JOB_ID --open     # print the reviews directory path
```

Prints one row per review: segment, attempt, the decision the policy derived, the
identity, anatomy, and continuity scores, whether the anchor is usable, and whether the
review was degraded. The full report on disk carries the Critic's scores, its own
advisory verdict, the policy's derived decision with its reasons, the thresholds that
were used, and any contradictions the Critic reported against itself.

The scores are a vision model's judgement, and the printed thresholds are policy knobs,
not measurements. The command says so on the last line.

### 6. Approve the human gate

```bash
omni-vlog approve JOB_ID --stage final --note "watched the full chain; the joins read as one take"
```

`--human-gate high-res,final` put a `final` gate in the spec, so the job stops at
`FINAL_REVIEW` even when every segment passed. Approval appends an entry to
`state_history` recording the stage and your note. It does not restart the job:
`omni-vlog resume` does that.

### 7. Resume

```bash
omni-vlog resume JOB_ID --dry-run   # show the plan, change nothing
omni-vlog resume JOB_ID
```

`resume` resolves recovery cases before it advances anything (§5.1): it queries any
interaction whose outcome was never observed rather than re-issuing it, then re-fetches
any output that completed but was never downloaded. Only then does it continue from the
current state.

After an approval, this is what finalizes the job and moves it to `COMPLETE`.

### 8. Export

```bash
omni-vlog export JOB_ID --output ./exports/final.mp4
```

Copies the finalized file to your path and prints where the manifest lives. The default
finalize path is a byte-for-byte copy, which is the only transform that preserves C2PA
exactly, so `final_derived` is false and no warning is printed. If a transform was
involved, the file is marked derived and the command says the content credentials may
not match the provider's original output.

---

## What is on disk

Everything for one job lives under `OMNI_DATA_DIR` (default `./.omni-vlog`), mirroring
the GCS layout so a job can move between local disk and a bucket without rewriting
paths.

```
.omni-vlog/jobs/<job_id>/
  job.db                              SQLite index (derived; the manifest is authoritative)
  input/
    references/00_identity_closeup_<sha8>.png   staged copy of each approved reference
    references/01_identity_body_<sha8>.png
    references/02_environment_<sha8>.png
    sanitizer/ref00_identity_closeup.json       local checks + vision pass per asset
  plans/
    project_spec.json                 the brief, normalised and frozen at creation
    continuity_bible.json             identity, outfit, environment, camera invariants
    segment_plan.json                 the three segments and their continuation anchors
  renders/
    segment_00/attempt_00_raw.mp4     the seed, straight from the provider
    segment_01/attempt_00_extend.mp4  extension 1
    segment_02/attempt_00_extend.mp4  extension 2
  reviews/
    segment_00_attempt_00.json        Critic report + the policy's derived decision
    segment_01_attempt_00.json
    segment_02_attempt_00.json
    final_review.json                 the chain-level review
  debug/
    0001_create_response.json         redacted raw exchange with the provider
    0002_get_response.json
  final/
    final.mp4                         the deliverable
    manifest.json                     the authoritative lineage
```

Notes on the files that are easy to misread:

- **Reference staging.** `check-references` names the staged copy
  `<index>_<role>_<sha256 prefix>.png`, so a re-run with the same input lands on the
  same filename. The sha256 in `input/references/` is the full hash on the
  `ReferenceAsset` in the manifest.
- **Attempt naming.** `attempt_NN_<kind>.mp4`, where `kind` is `raw` for the seed,
  `extend` for an extension, `edit` for a repaired segment, and `recovered` for bytes
  re-fetched from a URI after an interaction completed but the download did not. A
  repair replaces its segment's artifact in `manifest.segments`; the superseded file
  stays in `renders/segment_NN/` and its row stays in `interactions`.
- **`debug/`** holds the raw provider exchanges, redacted, and only when
  `OMNI_KEEP_RAW_RESPONSES=true`. This is where to look when the provider rejects
  something.
- **`final/manifest.json`** is the authoritative record. `job.db` is a derived index
  and `rebuild_from_manifest` repairs it. Keep a copy of the manifest for any run you
  care about.
- **`final/recovery.log`** is not written by this build. `write_recovery_note` exists in
  `pipeline/resume.py` and nothing calls it.

---

## The example manifest

`manifest.example.json` is one completed 30-second job, with `state: "COMPLETE"`. What
to look at:

- **`segments`** is the accepted chain, one artifact per planned segment, ordered by
  `segment_index`. Segment 0 has `"task": "reference_to_video"` and a null
  `parent_interaction_id`. Segments 1 and 2 have `"task": "extend"` and each names the
  previous segment's `interaction_id` as its `parent_interaction_id`. That is what
  `verify_chain_continuity` checks, and it is why the chain is a native continuation
  rather than three renders that happen to be the same length (§24.3).
- **`prompt` and `prompt_sha256`.** The three prompts are the compiler's own output
  shape, and each hash is the real SHA-256 of the prompt beside it. The seed prompt
  carries the `[# References <IMAGE_REF_0>@Image1 ...]` header, and those indices match
  the order the reference images appear in `input`, which is fixed by
  `order_references`.
- **`media`** reports what was measured: `container`, duration, dimensions, codecs,
  `has_audio`, `size_bytes`, and `probed_with`. `"probed_with": "ffprobe"` means the
  authoritative probe ran. The durations are 10.0, 10.0 and 9.9 seconds, so
  `chain_total_seconds` reports 29.9.
  `container` reads `"mov"` for an MP4 because ffprobe reports format name
  `mov,mp4,m4a,3gp,3g2,mj2` and the code keeps the first token.
- **`budget_events`** is the audit trail: one `authorize` event per paid call, and the
  three of them match the three interactions. **`budget`** is the current position and
  agrees with them: 3 calls made of 8, 30 video seconds requested of 90.
  Every cost reads `"0"`, because `pricing.yaml` ships with zeroed rates. That is the
  honest state of the estimate, not a claim that generation is free.
- **`c2pa_present: true`** and **`synthid_expected: true`** are different claims. C2PA
  is a manifest box that `media/mp4_probe.py` can actually find in the file. SynthID is
  a watermark this project cannot verify locally, so the field records an expectation
  about the provider, never a detection.
- **`degraded_cross_project_resume: false`.** The job ran its whole life against
  `example-homevlog-project`. This flag only ever becomes true with explicit human
  consent, via `resume --allow-cross-project`, and it is recorded rather than hidden
  because the chain's provenance is genuinely weaker afterwards.
- **`capability_snapshot` is null.** `omni-vlog create` does not run the probe, so a
  CLI-created job has no capability record on the manifest, and `pipeline/review.py`
  falls back to assuming edit and extend are available. Run `doctor` first and read
  `CAPABILITY_REPORT.md` as the record instead.
- **`quality_reports`** carries the shape `pipeline/review.py` persists: the Critic's
  report, the policy's derived decision with its reasons, the thresholds used, and any
  contradictions. The Critic's own `verdict` is advisory; the `decision` beside it is
  what the pipeline acts on, and both are kept so a disagreement stays visible.
- **`state_history`** is the transition log, in order, ending with the approval entry
  and the move to `COMPLETE`.

---

## Honest notes

- **Reference images are staged to GCS at dispatch time, and that needs a bucket.**
  `--reference` takes local paths. When the seed render dispatches,
  `BaseVideoProvider._resolve_reference_inputs` uploads each local file to
  `{OMNI_OUTPUT_GCS_URI}/jobs/<job_id>/input/references/` and sends the resulting URI.
  Without `OMNI_OUTPUT_GCS_URI` it falls back to sending the bytes inline as base64,
  which is a documented input form but was not part of the verified smoke test, so
  treat it as unverified. Set the bucket for anything you intend to keep.
- **The verified Vertex smoke test is one request**: text-to-video, 3s, 360p, 16:9,
  completed in 26.451 seconds. `previous_interaction_id` chaining, `extend`, and `edit`
  were NOT verified. Read `docs/LIMITATIONS.md` before relying on any of them, and
  re-run `doctor` before a production chain, because the model id carries `-preview`.
- **The job in the manifest used strategy C**, which you can see in `state_history`
  (`extending from <id> (strategy C)`). Strategy C means each extension re-uploaded the
  previous render as a video input. That is recorded per artifact precisely so a reader
  can tell it apart from a server-side-state chain.
- For failure modes and what to do about them, read `docs/RECOVERY.md`.
See [the no-person acceptance example](ACCEPTANCE.md) for the measured short native chain and local artifact locations.
