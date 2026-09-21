# Recovery runbook

How to diagnose and recover each failure mode this project knows about.

Structure of every entry: **symptom**, **diagnosis**, **action**. Error names are the
Python classes in `src/omni_homevlog/errors.py`; the string in brackets is the
`code` attribute the CLI prints. Every command below exists in `src/omni_homevlog/cli.py`.

Read this first:

1. **A request whose outcome you did not observe is never re-issued.** It may already
   be running and billable. `omni-vlog resume` queries it instead. This is recovery
   rule 2 in §5.1.
2. **The manifest is authoritative.** The SQLite index is derived and can be rebuilt.
3. **Quota failures stop the job.** No key rotation, no project rotation (§24.4, §24.5).

---

## Missing or expired ADC (401)

**Symptom.** Two different errors, depending on when it is caught.

- Before any request: `MissingCredentialsError [missing_credentials]`, message
  "Application Default Credentials are not available." The CLI prints the code, the
  message, and a `hint:` line carrying the remediation text from `errors.py`.
- From the service: HTTP 401 becomes `AuthError [auth_error]`,
  `retryable = False`, remediation "Refresh ADC: gcloud auth application-default
  login".

**Diagnosis.** `providers/auth.py::get_adc_credentials` calls `google.auth.default()`
once per process and caches the result. The credentials object refreshes its own
tokens, so an in-flight 401 means the underlying grant is gone, not that the token
merely aged out. `providers/auth.py` never reads a service-account JSON itself and
never prints a token, so the failure is always about ADC resolution.

Confirm with `omni-vlog doctor --provider vertex --project "$GOOGLE_CLOUD_PROJECT"`.
The free checks run first, so this costs nothing.

**Action.** Run one of these outside the program, then retry:

```bash
gcloud auth application-default login

# or, with service-account impersonation already configured:
gcloud auth application-default login \
  --impersonate-service-account=<your-vertex-sa>@<project>.iam.gserviceaccount.com
```

Then re-run `omni-vlog doctor`. A long-lived process needs a restart, because the ADC
result is cached in-process (`reset_auth_cache()` exists as a test hook only).

Do not paste a token onto a command line or into a query parameter. The handoff
requires it to travel only as an `Authorization: Bearer ...` header, which the
`AuthorizedSession` handles.

---

## 403 permission denied

**Symptom.** `PermissionError_ [permission_denied]`, `retryable = False`. The probe
shows "Endpoint reachable FAIL, HTTP 403, credentials rejected or IAM missing". A GCS
read can also fail here, which appears later as an un-downloadable output URI.

**Diagnosis.** One of three things is missing: the IAM role on the project, model
access for this model on this project, or bucket permission for the GCS prefix. The
provider message names which. The request body and the service response are kept in
the job's `debug/` directory as redacted fixtures when `OMNI_KEEP_RAW_RESPONSES=true`.

**Action.** Grant the minimum:

| need | grant |
|---|---|
| invoke the model | `roles/aiplatform.user` on the project |
| read GCS inputs | `roles/storage.objectViewer`, scoped to the bucket |
| write GCS outputs | `roles/storage.objectCreator`, scoped to the bucket |

**Do not grant** Owner, Editor, or broad Storage Admin for this agent. The handoff is
explicit about this, and `PermissionError_.remediation` repeats it.

If the role is already correct, check that the model is enabled for the project. A
403 on the first paid call in a project that has never used the model usually means
entitlement, not IAM.

---

## 400 invalid request

**Symptom.** `InvalidRequestError [invalid_request]`, `retryable = False`. Raised both
locally before dispatch and by the service.

**Diagnosis.** The payload does not satisfy the API contract. The checks in
`providers/request_builder.py` are the same ones the service enforces, so a local
failure and a service failure look alike. The four that change most often:

- **Duration.** `validate_duration` accepts an integer 3 through 10 inclusive
  (`ALLOWED_DURATIONS_S = range(3, 11)`), formatted as `"3s"`... `"10s"`. A 30-second
  request is never a single call; the chain is built from 10-second segments.
  `SegmentPlan.intended_duration_s` enforces the same range.
- **`delivery: "uri"` without `gcs_uri`.** `video_response_format` omits `delivery`
  entirely unless a `gcs_uri` is configured, because the API rejects the combination
  before generating. `validate_gcs_uri` additionally requires a `gs://` scheme and a
  bucket name.
- **MIME type.** Images: `image/png`, `image/jpeg`, `image/webp`, `image/heic`,
  `image/heif`. Video: `video/mp4`, `video/mpeg`, `video/mpg`, `video/mov`,
  `video/avi`, `video/x-flv`, `video/webm`, `video/quicktime`. HEIC spellings starting
  with `image/hei` are normalised rather than rejected.
- **Resolution and aspect ratio.** Resolution is one of `360p`, `720p`, `1080p`,
  `4k`. Aspect ratio is `16:9` or `9:16`.

Two local-only variants worth recognising:

- `Reference 'ref00_identity_closeup' has no gs:// URI. Stage it to GCS first, or pass
  it as first/last frame.` The references recorded by `check-references` are local
  files, and no pipeline stage uploads them.
- `Artifact <id> was delivered inline and no GCS prefix is configured, so the provider
  has nothing to reference.` An edit or extend needs the previous render reachable by
  the provider. Set `OMNI_OUTPUT_GCS_URI`.

**Action.** Read the service message, then fix the payload or the configuration. Do not
retry unchanged; the error is not retryable by classification, so a retry wrapper will
not help. A 400 whose provider code or message mentions safety is classified as
`SafetyBlockedError [safety_blocked]` instead, surfaced verbatim, and never
auto-rewritten. Adjust the creative brief by hand.

---

## 429 quota unavailable

**Symptom.** `QuotaExhaustedError [quota_exhausted]`, `retryable = False`. In the
probe, the row shows **BLOCKED**, not FAIL.

**Diagnosis.** This is a fixed-quota Preview model. Most 429s on it mean the project
has no usable quota for the model, not that the request was malformed. BLOCKED exists
precisely so this is not confused with a capability failure.

**Action.**

1. Stop. Do not retry in a loop, and do not reduce the request to "make it fit".
2. Report it. The remediation string says: "This is a fixed-quota Preview model.
   Report it and stop; do NOT rotate keys or projects to work around it."
3. Confirm the thinking by reading the BLOCKED row in `CAPABILITY_REPORT.md`.
4. Resolve it as an entitlement question with whoever owns the project quota.

**Never** rotate API keys or switch projects to get around this (§24.4, §24.5).
`omni-vlog projects` and `doctor --all-projects` list what the credentials can see, and
they deliberately do not rank or pool those projects.

---

## 5xx server error

**Symptom.** `ServerError [server_error]`, `retryable = True`.

**Diagnosis.** The transport retries a 5xx at most twice
(`MAX_SERVER_ERROR_RETRIES = 2`), so there can be up to three attempts in total. The
wait before attempt *n* is `min(20, 1.0 * 2**(n-1))` seconds plus up to 25 percent
jitter. The retry is only allowed because a 5xx proves the server did not return a
successful interaction. If every attempt fails, the last error is re-raised.

**Action.** If the job stopped, run `omni-vlog resume JOB_ID`. A 5xx is a clean
failure: no interaction id exists, so nothing billable is in flight and repeating the
stage is safe (recovery rule 1). Repeated 5xx across different stages points at the
service or the region endpoint, not at the payload.

---

## Client timeout with unknown outcome

**This is the section to read before anything else.** It is the one failure where the
obvious reaction costs money.

**Symptom.** `RequestTimeoutUnknownOutcome [timeout_unknown_outcome]`. The job moves to
`NEEDS_HUMAN` and `omni-vlog status` shows a line like "1 interaction(s) with unknown
outcome: ...".

**Diagnosis.** A synchronous request exceeded `omni_request_timeout_s` (default 600
seconds). Video calls routinely take tens of seconds; the verified 3-second smoke took
26.451 seconds. A timeout means **the generation may already exist and may be
billable**. The request left this process and no result came back.

The code has already recorded this:

- `pipeline/render_seed.py::_record_unknown_outcome` writes an `InteractionRecord`
  with `outcome_known = False` and a synthetic id of the form
  `unknown-<job_id>-s<segment>-a<attempt>`.
- `Budget.note_unknown_outcome` appends a `refund_unknown` budget event and leaves the
  call counted against the ceiling, so the possible spend stays visible.
- `RequestTimeoutUnknownOutcome.retryable` is `False`. This is deliberate: an
  over-eager retry wrapper cannot undo it.

**Action. Query, never re-issue.** (recovery rule 2)

```bash
# 1. See the plan without doing anything.
omni-vlog resume JOB_ID --dry-run

# 2. Resolve it by querying the interaction. Read-only, free, no new generation.
omni-vlog resume JOB_ID
```

`pipeline/resume.py::resolve_unknown_interaction` calls
`provider.get_interaction(target_id)` and then:

- still `in_progress`: leaves it unresolved and reports. Wait and run `resume` again.
- settled as anything other than `completed`: marks the record resolved, records the
  status, and does not retry.
- `completed` with a `gcs_uri` and a GCS client: downloads the output to
  `renders/segment_NN/attempt_NN_recovered.mp4` and records it as the artifact. This is
  recovery rule 3.
- `completed` but no recoverable URI (inline delivery): logs that the output cannot be
  fetched without a further generation, and does not start one.

If `resume` cannot resolve anything it exits 3 and prints the options: wait and re-run,
pass `--interaction-id`, or check the provider console.

**If no interaction id was captured.** With inline delivery a timed-out synchronous
request leaves no handle, and the record's id starts with `unknown-`. Querying that
synthetic id raises `JobNotFoundError` with the instructions: check the provider
console for a recent generation in the project, then re-run with
`--interaction-id <id>`.

```bash
omni-vlog resume JOB_ID --interaction-id <id-from-the-console>
```

`--interaction-id` is only applied when exactly one interaction is unresolved.

**What not to do.** Do not re-issue the request. Do not `omni-vlog retry`. Do not
delete and recreate the job. Each of those can pay twice for the same segment. If the
console shows a completed generation, its output is the artifact you already paid for;
recover that, do not regenerate it.

---

## Interaction completed but the download failed

**Symptom.** The manifest has a `gcs_uri` for a segment, `local_path` is null or the
file is gone, and the log says "Could not recover artifact output" or "Interaction
completed but the output could not be downloaded".

**Diagnosis.** The generation succeeded and is paid for. Only the local copy is
missing. This is recovery rule 3: re-fetch from the URI, never re-render.

**Action.**

```bash
omni-vlog resume JOB_ID
```

`pipeline/resume.py::fetch_missing_outputs` walks every artifact, skips those whose
local file already exists, and downloads the rest into
`renders/segment_NN/attempt_NN_recovered.mp4`. It prints "re-fetched N output(s) from
their URIs" and updates the manifest.

Requirements: a `gcs_uri` on the artifact, and a working GCS client
(`google-cloud-storage`, the optional `gcs` extra). Without a GCS client the step is
skipped silently at debug level, and `status` will keep showing the missing file.
A 403 on the download is a bucket permission problem, not a provider problem; see the
403 section above.

---

## Interaction id lost

**Symptom.** An `InteractionRecord` with a synthetic `unknown-` id, or a record whose
id does not match anything in the provider console.

**Diagnosis.** Two different situations, and they are not equally recoverable.

**Recoverable:**

- The bytes, if a `gcs_uri` exists. The manifest records the URI and the job's GCS
  layout mirrors the local one, so `gs://<prefix>/jobs/<job_id>/renders/segment_NN/`
  is where to look.
- Everything about the request: prompt, `prompt_sha256`, task, resolution, requested
  duration, timings, usage, and estimated cost are all in the manifest and in the
  interactions table.
- The job can continue, as long as the next stage does not need to reference the
  provider's server-side state for that specific interaction.

**Not recoverable:**

- Querying that interaction's own status, because there is no id to query.
- Editing or extending *from* it via `previous_interaction_id`. That handle is gone.

**Action.** Search the provider console or the bucket for the output. If you find a
completed generation and its URI, the artifact can be recovered as a file. There is no
CLI command that attaches a found interaction id to an existing manifest, and
`--interaction-id` only applies to resolving a record whose `outcome_known` is false.
Attaching one by hand means editing `final/manifest.json` and saying why in the commit
message, because `ManifestStore.transition` has no force flag.

To avoid this class of loss, configure `OMNI_OUTPUT_GCS_URI` so every render has a
durable address.

---

## Provider or project unavailable

**Symptom.** One of:

- `omni-vlog create` stops with "Provider not ready: ..." before spending anything.
- The job sits at `PROVIDER_UNAVAILABLE`.
- `ResumeConflictError [resume_conflict]`: "Job is bound to project X but the provider
  is now Y. Pass --allow-cross-project to continue (recorded as degraded), or restore
  the original project."

**Diagnosis.** `check_provider_ready` runs before job creation and again inside
`plan_resume` when the state is `PROVIDER_UNAVAILABLE`. `ensure_project_binding`
compares the manifest's pinned project with the current binding. A job binds to one
project for its whole life (§8.3), so a changed `GOOGLE_CLOUD_PROJECT` is refused
rather than followed.

**Action.**

1. Preferred: restore the original project. Set `GOOGLE_CLOUD_PROJECT` to the
   manifest's `project` and re-run `omni-vlog resume JOB_ID`, then
   `omni-vlog resume JOB_ID --dry-run` to confirm the plan.
2. If the original project is genuinely gone and you accept the cost:

   ```bash
   omni-vlog resume JOB_ID --allow-cross-project
   ```

   This sets `degraded_cross_project_resume = true` on the manifest and appends a note.
   §5.1 permits it only with explicit human consent and requires it to be visible,
   because the provider's server-side state does not carry across projects, so the
   chain is not continuous. Expect `verify_chain_continuity` to matter afterwards.

`resume` without `--allow-cross-project` on a divergent project raises
`ResumeConflictError` and changes nothing.

---

## Chain continuity check failed

**Symptom.** `omni-vlog status` or the run report carries notes starting
"continuity check:". `pipeline/finalize.py::mark_complete` raises "The chain is not
structurally continuous, so the job cannot be marked COMPLETE", and the orchestrator
moves the job to `NEEDS_HUMAN` with the same reasons.

**Diagnosis.** `pipeline/extend.py::verify_chain_continuity` checks three structural
facts over the usable artifacts, in order:

- every artifact after the first names the previous artifact's `interaction_id` as its
  `parent_interaction_id`;
- every artifact after the first has a `task` of `extend` or `edit`;
- the first artifact is not an `edit`, because an edit cannot be a seed.

A failure means the lineage is not a single native chain. That is the §24.3 condition:
three independent renders that happen to be the same length are not a 30-second
continuity shot. `media/transcode.py::concat_native_chain` refuses to concatenate
unless the caller asserts `confirmed_single_chain=True`, and the result is marked
`derived=True` if it is ever used.

**Action.**

1. Look at the chain table from `omni-vlog status JOB_ID`. The `parent` column shows
   each artifact's claimed parent.
2. Common causes: a cross-project resume, in which the provider's state did not follow
   the job; a repaired segment recorded with the wrong parent; a segment produced by a
   path that did not thread the previous artifact through.
3. Repair by re-rendering the first segment whose parent link is wrong, from the last
   segment whose links are intact. A regeneration is a new paid call and goes through
   the budget like any other.

A broken lineage is a real defect in the deliverable, not a formatting problem. The
final file is still the provider's own output for the last accepted segment; it is the
*claim of continuity* that the check refuses to certify.

---

## Corrupt or truncated manifest, or a damaged index

**Symptom.** `status` reports counts that disagree with the files on disk, `metrics`
misses a job that has a directory, or a manifest read raises
`JobNotFoundError [job_not_found]` / a JSON decode error.

**Diagnosis.** The two stores disagree. `storage/manifest.py` writes the manifest
first and the database second precisely so that a crash leaves the **manifest ahead of
the index**. The manifest is the authoritative record of lineage; the SQLite database
is a derived index. Neither write can leave a readable-but-truncated file:
`atomic_write_json` writes to a temporary file, fsyncs, and renames.

**Action.**

1. Inspect `final/manifest.json` for the job. If it parses, it is the truth.
2. Rebuild the index from it:

   ```python
   from omni_homevlog.config import get_settings
   from omni_homevlog.storage.local import LocalStore
   from omni_homevlog.storage.manifest import ManifestStore

   paths = LocalStore(get_settings().data_dir()).job("JOB_ID")
   store = ManifestStore(paths)
   store.db.rebuild_from_manifest(store.load())
   ```

   `Database.rebuild_from_manifest` re-upserts the job row, every artifact, every
   interaction, and every budget event. It parses the manifest first, so a malformed
   manifest cannot leave the database half-written.

3. If `job.db` is damaged beyond use, delete it. It is rebuilt from the manifest.

**If the manifest itself is unreadable**, and no copy exists, the lineage is gone. The
interactions table may still hold the per-call rows, but no code path reconstructs a
manifest from the database, because the database was never the source of truth. Keep a
copy of `final/manifest.json` outside the job directory for any run you care about.

---

## Budget exhausted

**Symptom.** `BudgetExhaustedError [budget_exhausted]`. The job moves to
`BUDGET_EXHAUSTED`, which is a human-gate state, or a create stage stops with "budget:
...".

**Diagnosis.** `Budget.check` is pure arithmetic over counters. The message names which
ceiling bound:

| ceiling | default | where it comes from |
|---|---|---|
| total calls | `min(--max-calls, 2 + 2*extensions + 2)` | `budget_for_mode` |
| video seconds requested | `max(30, target_duration_s * 3)` | `budget_for_mode` |
| seed attempts | 2 | `Budget.max_seed_attempts` |
| edits per segment | 1 | `Budget.max_edit_attempts_per_segment` |
| regenerations per segment | 1 | `Budget.max_regenerations_per_segment` |
| extends per segment | 2 | hardcoded in `Budget.check` |
| estimated cost | unset | `--max-cost`, or `OMNI_MAX_ESTIMATED_COST_USD` |

For a 30-second job the call ceiling computes to 8. Passing `--max-calls 20` does not
raise it, because `budget_for_mode` takes the minimum.

**Action.** Raising a ceiling is a deliberate decision, not a retry.

- Recreate the job with a higher ceiling: `omni-vlog create ... --max-calls N
  --max-cost X`. Raising it mid-job means editing the manifest by hand and saying why.
- For a single, explicitly accepted overspend: `omni-vlog retry JOB_ID --stage
  segment-1 --mode edit --force`. `--force` skips the pre-check, and the flag text says
  you accept exceeding the ceiling.
- `BUDGET_EXHAUSTED` does not lead anywhere automatically. `omni-vlog approve` records
  an approval; `omni-vlog resume` then continues from the gate.

Cost figures are **estimates**. `pricing.yaml` ships with zeroed rates, so an estimate
may read 0.00 while real money is spent. Reconcile against Cloud Billing, which is
authoritative.

---

## ffmpeg missing

**Symptom.** `MediaToolMissingError [media_tool_missing]`, message "ffmpeg ('ffmpeg')
not found on PATH", remediation "Install ffmpeg (macOS: `brew install ffmpeg`)." A
review report shows `critic_degraded: true`.

**Diagnosis.** ffmpeg and ffprobe are looked up on PATH and nothing is bundled.

**What degrades:**

- No keyframe extraction, so the Critic reviews with fewer than the 3 frames it needs
  for a full visual review. `agents/critic.py` marks the report degraded.
- The decision policy then refuses to auto-accept: `HUMAN_REVIEW`, with the reason that
  the degraded review's scores cannot authorise spend. `decide_final` blocks the chain
  when any segment review was degraded.
- No remux, concat, or audio strip. `finalize` still works, because its default
  `copy_verbatim` path is a byte copy.

**What still works.** `media/mp4_probe.py` is pure Python and reports duration, width,
height, video codec, audio codec, and C2PA presence. `MediaInfo.probed_with` records
`"mp4_probe"` so you can see which path ran.

**Action.** Install ffmpeg (`brew install ffmpeg`), or set `OMNI_FFMPEG_BIN` /
`OMNI_FFPROBE_BIN` to an existing binary. Until then, expect every job to stop for a
human decision, by design.

---

## Critic output unparseable

**Symptom.** `omni-vlog review JOB_ID` shows `degraded: yes` and a verdict of
`human_review`. The stored report has every score at 0.00, `critic_model: "unavailable"`,
and a severe defect reading "critic output could not be parsed: ...".

**Diagnosis.** `agents/critic.py::_unparseable_report` builds that report when the
Critic's output cannot be trusted at all. It is deliberately pessimistic: all scores
0.0, verdict `human_review`, `critic_degraded = True`. The alternative, guessing, is how
a broken Critic silently approves bad footage.

The policy then cannot accept it. `_hard_reject_reasons` sees the severe defect, and the
degraded check returns `HUMAN_REVIEW` with "the review itself was degraded, so its
scores cannot authorise spend". The two possible outcomes are `REGENERATE` (if the
budget allows and the score floors justify it) and `HUMAN_REVIEW`. **An unparseable
Critic is never an implicit pass.**

**Action.**

1. Confirm whether the review was thin as well as unparseable. `critic_degraded` is also
   set when fewer than 3 frames were available, which usually means ffmpeg is missing.
2. Check that the planning and review models are reachable. They are separate models
   (`DIRECTOR_MODEL`, `CRITIC_MODEL`, default `gemini-3.8-flash`), not the Omni video
   model, because Omni has no structured-output support (§4.1).
3. Read the redacted exchange in the job's `debug/` directory.
4. Then decide by hand: `omni-vlog retry JOB_ID --stage segment-N --mode regenerate`, or
   `omni-vlog approve` plus `omni-vlog resume` if you have watched the segment yourself
   and accept it.

Approval is recorded in `state_history` with the stage and your note. It is a human
decision, and the manifest says so.