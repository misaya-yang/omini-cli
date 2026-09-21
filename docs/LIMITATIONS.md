# Limitations

What this build has not verified, cannot do, and will not claim.

Every statement below is checkable against the source under `src/omni_homevlog/` and
against the plan document (`GEMINI_OMNI_HOMEVLOG_AGENT_CODEX_PLAN_20260921.md`,
referenced by section number). Where a capability is documented but unprobed, this
file says it is unprobed. An unprobed capability is treated as unsupported.

---

## Unverified surfaces

The **`gemini_api` adapter is implemented but has never been exercised end to end.**
`providers/gemini_api.py` and `providers/transport.py::GeminiApiRestTransport` exist
and construct well-formed payloads, but nothing in this project has sent a request to
`generativelanguage.googleapis.com`. The capability probe can test it
(`omni-vlog doctor --provider gemini_api --run-generation` with `GEMINI_API_KEY` set),
and until that runs, every gemini_api capability is UNKNOWN.

The **Vertex REST path is the verified one**, and it was verified by exactly one
request. The handoff records a paid smoke test on 2026-09-21:

| field | observed |
|---|---|
| model | `gemini-omni-1.1-flash-preview` |
| request | text-to-video, 3s, 360p, 16:9, one candidate |
| transport | HTTP 200, SSE, interaction status `completed` |
| latency | 26.451 s |
| output | MP4, H.264 video, AAC audio, 640x360, 3 seconds, 725368 bytes |
| usage | 45 text input tokens, 5793 video output tokens, 297 thought tokens |

That one request proves authentication, routing, model access, quota for one call, and
the response shape for that call. It does not prove anything else.

Specifically, the following were **NOT verified**:

- **`previous_interaction_id` chaining was NOT verified.** This is strategy A, the
  cheapest chain mechanism (§8.2, §8.3): the server keeps the state and no video is
  re-uploaded. Until it is probed, `VertexEnterpriseProvider.chain_strategy` returns
  strategy C whenever no capability record exists, because C is the only mechanism
  that does not depend on an unverified stateful feature.
- **`extend` was NOT verified.**
- **`edit` was NOT verified.**
- I2V, reference-to-video, first and last frame, GCS URI delivery, and native audio
  were NOT verified in this project.
- **`steps` replay was NOT verified and is never probed automatically.**
  `capability_probe.build_probe_report` records the row as UNKNOWN with the note that
  replaying prior `interaction.steps` needs a payload the probe does not synthesise.

The probe is honest about this by construction. `_capabilities_from()` turns a probe
row into a capability only when that row is PASS. UNKNOWN and SKIPPED never become
true. With `--run-generation` omitted, the rows "T2V 3s 360p", "I2V 3s 360p",
"Reference-to-video", "Edit", "Extend", "previous_interaction_id", "steps replay",
"GCS URI delivery" and "Native audio" are all marked SKIPPED with the reason that
generation probes cost money.

One further gap in the unprobed path:

- `omni-vlog create` does not run a probe, so a job created from the CLI has
  `manifest.capability_snapshot` set to null. `pipeline/review.py` then falls back to
  `can_edit = True` and `can_extend = True` rather than reading a capability record.
  Run `omni-vlog doctor` first and the snapshot is carried into any job created
  afterward in the same process; otherwise the pipeline assumes the capabilities are
  present and finds out from the provider, which is a worse error message.

### How reference images reach the provider

A job is created from local photo files. `BaseVideoProvider._resolve_reference_inputs`
resolves each reference at dispatch time, in this order:

1. already a `gs://` URI — passed through unchanged;
2. a readable local file **and** `OMNI_OUTPUT_GCS_URI` is set — the file is uploaded to
   `{prefix}/jobs/{job_id}/input/references/{name}` and the URI is used, then cached on
   the asset so a repair does not re-upload the same photo;
3. a readable local file with no bucket configured — the bytes are sent inline as
   base64, and a warning is logged.

Path 3 is a documented input form but is **not** covered by the handoff's verified
smoke, so treat it as unverified. It is also why a job with references and no
`OMNI_OUTPUT_GCS_URI` is best treated as a concept run: the same bucket is needed for
the native extension chain anyway (strategy C re-uploads each render as a video input),
so configuring it is not optional for production work.

---

## What the first live chaining experiment found

Run on 2026-09-21 against `gemini-omni-1.1-flash-preview`, 3s / 360p / 16:9, one
paid call each. This is the only chaining evidence in the project so far.

| request | result |
|---|---|
| `text_to_video`, no chaining | **PASS** — 3.008s, 640x360, H.264 + AAC, C2PA present, 412,628 bytes |
| `previous_interaction_id` **+** `generation_config.video_config.task` | **400** `invalid_request`: *"previous_interaction_id is not allowed when video task is set."* |
| `previous_interaction_id`, **no** `generation_config` | **PASS** — the film grew **3.008s → 6.016s** |

### The plan's §8.2 is wrong about this

§8.2 lists `previous_interaction_id` as the preferred chaining mechanism and says
to fall back to re-uploading a video only if it is unsupported. It is supported,
but **not in combination with an explicit task**. Sending both is a hard 400, so an
implementation that follows §8.2 literally produces a request the API rejects on
every extension.

`VertexEnterpriseProvider.build_payload` therefore drops `generation_config` for
any request carrying a parent interaction id, and the video task is inferred from
`response_format`. `apply_continuation_rule=False` disables the workaround if the
surface is ever fixed. `tests/unit/test_vertex_continuation.py` pins the behaviour
and records the measurement.

### What this changes for the design

Strategy **A** works, so the 30-second chain does not need a GCS bucket for
chaining, and it does not need to re-upload each render. That removes the main
reason strategy C looked necessary.

It also removes the main practical reason to configure a bucket, though a bucket is
still needed to keep the job's audit trail off the local machine.

### A probe must be probed too

The first version of the `previous_interaction_id` probe sent the *seed's* prompt
— "a calm static shot of an empty wooden table" — while also passing
`previous_interaction_id`. Two identical runs then produced 6.016s and 3.008s,
which looks precisely like an unreliable API and was actually a badly-posed
request: the model received "generate a shot of a table" and "continue the
previous video" at once, and resolved the contradiction differently each time.

The `continuation grows the film` row is what caught it. Without a check on the
*outcome*, both runs would have read as PASS and the conclusion would have been
"native chaining is flaky", which is wrong and would have discredited a working
mechanism.

Fixed, the probe is stable: five consecutive runs all chained 3.008s → 6.016s.

### The extension returns the whole film

A 3s seed extended by 3s returned **6.016s**, not a separate 3s fragment. That is
what the pipeline assumes: §24.3 forbids splicing independent clips, and if
`extend` returned only the appended portion, reassembling it would be exactly the
splice the plan prohibits. It does not, so no change is needed there.

### Still unverified

* a **10-second** seed, which is what the production chain actually uses
* a **third** link (segment 3 chaining from segment 2, itself chained)
* `edit`, reference-to-video, GCS URI delivery, and the whole `gemini_api` surface
* whether the concatenation of three 10s links stays under the documented 40s ceiling

The extension was also verified as *stateful*, not as *visually continuous*. Length
growing is consistent with a native continuation, but nothing here checked that the
frames actually continue rather than cutting. That needs an eye on the file, or the
Critic reviewing the previous segment's tail frames.

---

## Preview-model instability

The model id carries `-preview` (`gemini-omni-1.1-flash-preview` on Vertex,
`gemini-omni-1.1-flash` on the Gemini Developer API). §24.6 forbids treating preview
behaviour as a long-term stable protocol. Behaviour, quota, accepted parameters, and
even the response shape can change without notice.

§2.3 records differences between the two surfaces that already exist on paper:

- the Developer API recommends `previous_interaction_id`;
- the Cloud notebook replays the prior `interaction.steps` instead;
- the Developer API documents a 10-second limit on uploaded video for extend, while
  the Cloud notebook example states 30 seconds;
- some parameters have different SDK spellings on each surface.

That is why `ProviderCapabilities` exists and why nothing in this codebase stores a
capability as a global fact. Every record carries `probed_at`, and a capability is
only true for the provider, project, and model that were probed at that moment.
`VertexEnterpriseProvider.model_notes()` adds the preview warning to the record.

**Re-run `omni-vlog doctor` on any day you intend to rely on the report**, and before
starting a production chain.

---

## No resolution upgrade path

`360p`, `720p`, `1080p` and `4k` are all accepted values of
`response_format.resolution` (`request_builder.ALLOWED_RESOLUTIONS`). Per §2.1, 1080p
and 4k are **upscales** of the model's own output.

There is no lossless upgrade from a 360p draft to 1080p. Re-generating the same brief
at a higher resolution produces **different content**: the person, the framing, and
the motion can all change. §24.12 forbids describing a regenerated 1080p file as the
same content as the 360p draft, and §12.3 requires this to be stated before the
operator spends on it.

### `omni-vlog high-res` starts a new job, not an upgrade

Because a higher-resolution render is a different generation, the command does not
transform the existing output. It creates a **separate job** with its own lineage,
budget, and review cycle:

```bash
omni-vlog high-res JOB_ID --resolution 1080p --confirm-understanding
```

`pipeline/finalize.py::check_high_res_request` refuses in three cases:

- the operator has not passed `--confirm-understanding`, in which case
  `HighResConfirmationRequired` explains that the footage is re-generated;
- the requested resolution is not actually higher than the job's current one;
- the resolution string is not one of `360p` / `720p` / `1080p` / `4k`.

The new job inherits the source job's brief, style, gates, and approved references,
and each manifest records a note naming the other. That cross-link matters: without
it, a reader looking at the 1080p job alone would have no way to know it came from a
draft, and might assume the two match.

Practical consequence: choose the resolution at `omni-vlog create` time and keep it
for the whole chain (§12.3). Reach for `high-res` only once the draft has been
reviewed and you have decided the content is worth re-rendering.

---

## ffmpeg is optional and its absence is real

`ffmpeg` and `ffprobe` are looked up on PATH (`media/ffprobe.py::ffmpeg_available`,
`ffprobe_available`). Nothing is bundled.

Without ffmpeg:

- **No keyframe extraction.** `media/extract_frames.py::extract_frames` returns a
  `FrameSet` with `degradation_reason` set and no frames. It does not substitute a
  single frame and call it a review.
- **The Critic then reviews on thin evidence.** `agents/critic.py` marks the report
  `critic_degraded` when fewer than `MIN_FRAMES_FOR_FULL_REVIEW` (3) frames are
  available, or when no video input was supplied. It also downgrades a `verdict` of
  `accept` to `human_review` in that case.
- **The decision policy refuses to auto-accept it.**
  `agents/decision_policy.decide_segment` returns `HUMAN_REVIEW` for any degraded
  report, with the reason that the review was degraded so its scores cannot authorise
  spend. `decide_final` blocks the chain when any segment review was degraded.
- **No transcode, remux, or concat.** `media/transcode.py::_require_ffmpeg` raises
  `MediaToolMissingError` (`media_tool_missing`), whose remediation is
  `Install ffmpeg (macOS: brew install ffmpeg).`
- `media/extract_frames.py` and `media/transcode.py` raise the same error type when a
  caller passes `require_complete=True`.

Finalization still works without ffmpeg, because its default is `copy_verbatim`, a
byte-for-byte copy that shells out to nothing.

With or without ffmpeg, media facts come from `media/mp4_probe.py`: pure Python, reads
the ISO-BMFF boxes, and reports duration, width, height, video codec, audio codec, and
C2PA presence. `MediaInfo.probed_with` records `"ffprobe"` or `"mp4_probe"`, so a
report never implies more precision than it has.

---

## SynthID cannot be verified locally

The manifest field is `synthid_expected`. It is an **expectation, not a detection.**
`media/c2pa.py::inspect_content_credentials` sets it from the provider name (SynthID
is expected for `vertex` and `gemini_api` outputs) and reports
`synthid_verifiable_locally = False`. Nothing in this codebase reads or checks a
watermark, and no dependency is capable of doing so.

Do not present `synthid_expected: true` as evidence that a watermark is present in the
file. That is why the field is not called `synthid_present`.

---

## C2PA can be lost by transcoding

`media/ffprobe.py::detect_c2pa` looks for a C2PA box in an MP4 and returns
present, absent, or unknown. This project **detects only**. It never strips and never
rewrites content credentials (§22, §24.13).

Some ffmpeg operations drop the C2PA manifest box. The code makes that visible rather
than silent:

- `media/transcode.py` records C2PA state before and after, and every function that
  shells out to ffmpeg sets `derived=True` on its result.
- `media/c2pa.py::assert_not_stripped` reports a loss and refuses to "fix" it.
  Re-adding a manifest would mean fabricating provenance.
- `RenderArtifact.derived` and `Manifest.final_derived` carry the flag. `omni-vlog
  status` shows it, and `omni-vlog export` prints a warning when the finalized file is
  derived.

A derived file is not the provider's original bytes. Its content credentials may not
match the provider's output, and it should not be presented as the credential-bearing
artifact.

---

## Quota is not a code defect

This is a fixed-quota Preview model. A `429` on it usually means the **project has no
usable quota for the model**, not that the request was malformed. The handoff says so
directly.

The taxonomy keeps the two facts apart:

- In the capability probe a 429 becomes `BLOCKED`, with the detail "Preview models
  have fixed quota; this is a project entitlement issue, not evidence the capability is
  unsupported". BLOCKED is deliberately distinct from FAIL, because conflating them
  sends people debugging the wrong thing.
- In code the same failure is `QuotaExhaustedError` (`quota_exhausted`), with
  `retryable = False` and the remediation "This is a fixed-quota Preview model. Report
  it and stop; do NOT rotate keys or projects to work around it."

**This project never rotates keys or projects to work around quota** (§24.4, §24.5).
`providers/factory.py::list_available_projects`, behind `omni-vlog projects` and
`doctor --all-projects`, reports what the current credentials can see. It does not
rank, pick, or pool them, because a "helpful" ordering would invite exactly that.

---

## Heuristic scores

The Critic's scores come from a vision model's judgement. They are heuristic, never
ground truth (§11.5), and the thresholds they are compared against are uncalibrated.

`configs/quality_thresholds.yaml` carries `calibrated_at: "2026-09-21"` and a
`calibration_note` reading "Initial hand-set values, inherited from the plan document.
NOT calibrated against scored samples. Expect to move these after the first 20-30
reviewed renders."

State this plainly: **the scores and the thresholds are not an objective quality
standard.** A score of 0.83 does not mean the frame is 83 percent correct. Tune the
thresholds against real samples, then update `calibrated_at` and the note.

The Critic's own `verdict` field is **advisory**. `agents/decision_policy.py`
re-derives the decision in pure Python from the scores and flags, and only the derived
decision can authorise spending (§24.8). When the two disagree, the disagreement is
recorded in `critic_verdict_agrees` and in the review JSON rather than silently
overridden. `omni-vlog review` prints the thresholds in use and says they are policy
knobs, not measurements.

---

## Concurrency

There is **no parallel generation**. The chain is strictly sequential: each extension
is generated from the previously accepted render, so a later segment cannot start
before the earlier one exists. `pipeline/orchestrator.py` runs one stage at a time in
a single loop, and `RunReport` records the stages in order.

§2.2 records `candidateCount=1` for this model. No caller in this codebase requests
multiple candidates and none sets `candidateCount`. `build_create_payload` has no such
parameter; `extra_generation_config` could inject one, and nothing does.

`doctor --run-generation` runs its paid probes one at a time as well, cheapest first.

---

## Inline delivery has no recovery path

With inline delivery the video arrives as base64 inside the response body. If the
client times out on a synchronous request, the generation **may already exist and may
be billable**, and there is no interaction id to query.

What the code does instead of guessing:

- `pipeline/render_seed.py::_record_unknown_outcome` writes an `InteractionRecord`
  with a synthetic id of the form `unknown-<job_id>-s<segment>-a<attempt>` and
  `outcome_known = False`, records the reservation, and moves the job to
  `NEEDS_HUMAN`.
- `pipeline/resume.py::resolve_unknown_interaction` refuses to query a synthetic id and
  raises `JobNotFoundError`, telling the operator that a synchronous request which
  timed out leaves no handle and to check the provider console for a recent generation.
- `errors.RequestTimeoutUnknownOutcome` carries `retryable = False` and
  `outcome_unknown = True`, so an over-eager retry wrapper cannot undo the decision.

**GCS delivery (`delivery: "uri"` plus `gcs_uri`) is strongly preferable for
production.** It gives the output a durable address that can be re-fetched later
(recovery rule 3), and it is what strategy C requires anyway, because a native `extend`
is fed the previous render's URI as a video input.

Set `OMNI_OUTPUT_GCS_URI` to a bucket you are approved to write to.
`request_builder.video_response_format` omits `delivery` entirely unless a `gcs_uri` is
configured, because the API rejects `delivery: "uri"` without `gcs_uri` before it
generates anything. Even with strategy A, `providers/base.py::_require_source_uri`
needs a reachable URI for the previous render, so inline delivery plus no bucket means
extensions cannot be dispatched at all.

---

## Region and media restrictions

§2.1 records that some regions impose additional restrictions on uploaded-video
editing, on person images, and on identifiable faces. §22 requires confirming the
operator's regional restrictions before production. Nothing in this code detects the
region for you.

Related rules that are enforced in code:

- §6.2 hard-rejects an identity reference whose `provenance` is `unknown`, because
  replicating an identifiable real person from an unverified source is not allowed.
  `check-references` and `create` default to `--provenance unknown`, so a job whose
  references are identity roles is rejected unless you pass `--provenance synthetic`,
  `owned` or `licensed`. `pipeline/intake.py::require_approved` enforces this even when
  `create` is run without asking.
- An unverifiable adult subject is also a hard reject, and a failed vision pass reads
  as "not clean" rather than as approval.
- Uploaded reference video audio is ignored by the model, and uploads for edit or
  extend are length-limited (§2.1). `max_upload_edit_s` and `max_upload_extend_s` exist
  on `ProviderCapabilities` and are `None` until a probe measures them.