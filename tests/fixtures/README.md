# Test fixtures

## `quality_regression_set.yaml`

The plan's §21.4 regression set: five fixed scene types, each isolating one
capability the continuity story depends on.

| scene | exercises |
|---|---|
| `cup-sip` | object contact, hand anatomy, facial identity |
| `stand-up` | whole-body motion, weight and balance, camera follow |
| `short-walk` | gait, motion continuity, handheld realism |
| `table-to-bedroom` | space traversal, environment topology, identity under motion |
| `lens-cover` | end-anchor quality, hand-to-camera depth, audio transition |

It is a **checklist, not a test suite.** §21.4 requires a manual A/B after every
SDK or model change and says plainly that the automated scores are not the only
evidence. Two tests support that:

- `tests/unit/test_quality_regression_fixture.py` checks the fixture itself —
  five scenes, unique ids, every capability covered, and prompts that would not
  trip the §9 guardrails. Free, runs on every `pytest`.
- `tests/live/test_quality_regression.py` renders the scenes and writes
  `regression_run.json`. Paid, and gated twice:

  ```bash
  RUN_LIVE_VIDEO_TESTS=1 OMNI_RUN_REGRESSION=1 \
    pytest tests/live/test_quality_regression.py
  ```

Grade the clips by eye against the previous run and append a block to `runs:` in
the YAML. Keep the old blocks: the comparison is against the last run, not against
an absolute standard, because there is no calibrated standard to compare against.

A single `fail` on identity, continuity, or artefacts is a release blocker
regardless of how the other four scored. Those three are what the project exists
to get right; the rest is polish.

## Why the prompts are short

They are deliberately terse and neutral. A regression set is a measuring
instrument, so the prompts must stay fixed while the model changes. Editing a
prompt to make a scene look better destroys the only thing the set is for.