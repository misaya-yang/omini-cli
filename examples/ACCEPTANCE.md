# No-person technical acceptance example

The 2026-09-22 run used an empty room and a wooden table. It made three native-chain requests (3 → 6 → 9 seconds), then changed only the tabletop colour. One edit request had the wrong duration; the corrected request reused the same interaction chain. Total: five video requests, not five complete chains.

Local artifacts are under `outputs/acceptance_20260922/` (gitignored):

- `chain.mp4`, `edited.mp4`
- `chain_frames.jpg`, `edited_frames.jpg`
- `acceptance_manifest.json` (real hashes, lineage and request counts)
- `combined_report.md`, `critic_result.json`

This is a technical probe, not a production job marked COMPLETE. Remote GET recovery failed and the example makes no person-identity or 30-second quality claim. Do not import its measured capabilities into another project.

To repeat the mechanics deliberately, with a fresh independent budget:

```bash
RUN_LIVE_VIDEO_TESTS=1 omni-vlog doctor --provider vertex \
  --run-generation --checks seed,extend,third,edit --max-calls 4
```

The corrected doctor uses the cumulative source duration for editing. This command costs money; it is not needed to view the existing example. To check the files without network access:

```bash
ffprobe -v error -show_format -show_streams outputs/acceptance_20260922/edited.mp4
```
