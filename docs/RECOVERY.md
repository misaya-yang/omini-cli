# Recovery

A generation timeout or 5xx may already have consumed quota. Do not issue a replacement generation until the recorded request is resolved. Video POST is never automatically retried.

## Inspect first

```bash
omni-vlog status JOB_ID
omni-vlog resume JOB_ID --dry-run
omni-vlog review JOB_ID
```

The job manifest is authoritative; SQLite is a derived index. Keep the complete job directory, including `debug/` receipts, render files, plans and budget events. Do not delete a pending interaction to make a job runnable.

## Unknown request outcome

```bash
omni-vlog resume JOB_ID
# Only when a request has no captured server ID and the operator found its ID:
omni-vlog resume JOB_ID --interaction-id SERVER_INTERACTION_ID
```

Server IDs are checkpointed before downloading. Resume first uses any verified local receipt, otherwise queries the original provider. Inline and URI responses can be materialised without a new create call. A pending response, failed query or failed download remains unresolved. A completed recovery is routed to review rather than re-rendering.

On 2026-09-22 the original unary GET returned Internal error and Deadline expired. The follow-up fixed the Vertex GET path with `stream=true` and SSE delta reconstruction; live recovery matched the original SHA256. Async reference generation was also recovered with GET only. Keep unresolved jobs if a transient error returns: there is no paid fallback.

## Human review and repair

```bash
omni-vlog approve JOB_ID --stage final
omni-vlog resume JOB_ID
omni-vlog retry JOB_ID --stage segment-2 --mode edit --edit-prompt "remove the timestamp overlay"
```

`final` approval works at FINAL_REVIEW. Segment/high-resolution gates retain their explicit stage names. Approval is not a blanket override of a quality defect; the next review still applies policy.

Retry refuses unknown outcomes, terminal jobs and earlier segments with existing descendants. Repair the latest cumulative video, or start a new chain. Each edit/regeneration consumes the persisted budget. `--force` is an explicit budget relaxation, not automatic recovery.

## Download, database and export

Missing URI-backed files may be fetched again. Saved local receipts include SHA256; corrupt or absent files do not pass local recovery. SQLite can be rebuilt through `Database.rebuild_from_manifest()` using the saved manifest; do not edit both stores by hand.

```bash
omni-vlog export JOB_ID --output ./exports/final.mp4
```

Export requires COMPLETE, writes the video byte-for-byte by default, and writes `final.manifest.json` beside it. An unapproved or rejected derived export leaves an existing destination unchanged.

Only one process may operate on a job at a time. A held lock produces an error before dispatch; the OS releases locks after process exit. A lock file remaining on disk does not itself mean a lock is held.

## Workbench recovery

Start `omni-vlog studio`, open the saved creation under 最近创作 and choose 查询进度 if needed. This queries the pinned server interaction and never creates a replacement video. While the server remains running it polls a pending result up to 20 times at 15-second intervals (plus query latency). Closing the webpage does not stop the server. Restarted work is never silently resubmitted. A missing server ID requires investigation of the saved version's job ledger. Workbench versions are stored under `.omni-vlog/studio`; each version has a separate one-call job budget and retains its parent version.
