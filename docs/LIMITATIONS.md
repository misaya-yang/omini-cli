# Current limitations

The current measured baseline is [the 2026-09-22 acceptance record](ACCEPTANCE_20260922.md).
It supersedes earlier contradictory claims about unprobed default strategy A/C and missing capability persistence.

## Measured versus unverified

Vertex REST + ADC produced a 3.029 → 6.037 → 9.024 second native chain and edited the last version without changing its duration. The no-person scene is not a test of identity continuity. Generated files contain audio and C2PA boxes; this is not signature validation or SynthID detection.

The earlier unary GET errors were resolved by requesting `stream=true` and reconstructing `step.delta` outputs. Real remote recovery now matches the original 9.024s video SHA256; a pending 3s reference render also recovered successfully. Transient failures and requests without a server ID remain unresolved rather than generating a substitute.

Reference-to-video, uploaded-source extension, first/last frames, steps replay, GCS delivery, 30/40-second production chains, longer-than-10-second editing and Gemini Developer API remain unverified. Capabilities are scoped to provider/project/model/location. Unknown capabilities are not automatically available. A measured 9-second chain is not proof of a 30-second production chain.

## Budget and media review

Video call and requested-second ceilings survive restarts. Unknown pricing is not free: a dollar ceiling requires configured video rates. Text/vision calls have a separate persisted counter (default 24 per job), including retries, with recorded usage and unknown dollar estimates. This is not a complete Cloud Billing cost cap.

Critic receives keyframes, references and inline video with its audio when the file is at most 18 MB. Missing video or incomplete frames produces a degraded review requiring human attention. ffmpeg/ffprobe are optional for metadata inspection, but needed for complete frame-based review. Critic scores are heuristic, not calibrated quality measurements.

Default export copies provider bytes. Derived files are marked; export refusal happens before any destination write. A completed manifest accompanies the export. Intermediate files remain inspectable in the job directory, but cannot bypass final approval through the export command.

`--no-audio` currently affects planning intent; it is not a verified provider audio-disable switch. No automatic stripping of audio/content credentials is performed.

## Recovery and scope

A job stays on its original provider/project. Cross-project migration is not implemented as an automatic recovery strategy; the legacy flag does not move server-side state. No key rotation, quota pooling, independent-video splicing, or automatic publishing exists.

Local job locking currently uses POSIX flock (macOS/Linux). Windows locking has not been implemented or tested. Locks do not coordinate several machines sharing a GCS prefix.

Phase 4 web UI, queue and deployment are not part of the CLI implementation. The five fixed quality scenarios remain opt-in and were not regenerated in this limited-cost acceptance run.

## Local workbench

The workbench is a manual 3–10 second generation and version-editing interface. It does not run the automatic Director/Critic loop, classify uploaded identities, or claim a quality score. Local image validation is not an AI review. Native long-video chaining and full automated review remain available through the CLI. Browser interaction tests use an isolated local substitute and existing real video bytes; they are not a separate live generation acceptance. The server binds only to loopback and is not a hosted multi-user service.
