"""Quality regression set driver (§21.4).

Loads `tests/fixtures/quality_regression_set.yaml`, renders each scene once at the
configured settings, saves the clips, and writes `regression_run.json` next to
them so a human has the footage and the measurements together.

**This does not decide pass or fail.** §21.4 is explicit that automated scores are
not the sole evidence and that the real check is a person comparing the clips
against the previous run. All this module does is produce the material.

The fixture's own integrity is checked in
`tests/unit/test_quality_regression_fixture.py`, which runs on every test run. Only
the paid rendering lives here.

Opt-in twice over, because five renders is a real spend:

    RUN_LIVE_VIDEO_TESTS=1 OMNI_RUN_REGRESSION=1 pytest tests/live/test_quality_regression.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.providers.capability_probe import probe_generation
from omni_homevlog.providers.factory import build_provider

pytestmark = pytest.mark.live

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "quality_regression_set.yaml"


def load_regression_set() -> dict:
    with FIXTURE_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.mark.skipif(not Path(FIXTURE_PATH).exists(), reason="regression set fixture is missing")
def test_quality_regression_set_renders(live_settings, tmp_path_factory) -> None:
    """Render every scene once and record what came back.

    Skipped unless the live gate is satisfied. Even then, it renders — it does not
    grade. Read the output clips yourself.
    """
    import os

    if (os.environ.get("OMNI_RUN_REGRESSION") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        pytest.skip(
            "the regression set makes five paid renders. Run `omni-vlog doctor "
            "--run-generation` for a cheaper check, or set OMNI_RUN_REGRESSION=1 "
            "if you intend to spend on the full set."
        )

    from omni_homevlog.storage.local import LocalStore

    data = load_regression_set()
    settings_block = data["settings"]

    root = tmp_path_factory.mktemp("regression")
    store = LocalStore(root).ensure()
    paths = store.job("job-regression").ensure()

    binding = build_provider(paths=paths, settings=live_settings, with_gcs=False)

    observations: dict[str, dict] = {}
    for scene in data["scenes"]:
        result, envelope = asyncio.run(
            probe_generation(
                binding.provider,
                task="text_to_video",
                duration_s=int(settings_block["duration_s"]),
                resolution=str(settings_block["resolution"]),
                label=f"regression {scene['id']}",
            )
        )

        entry: dict = {
            "status": result.status.value,
            "detail": result.detail,
            "exercises": scene["exercises"],
        }

        if envelope is not None and envelope.video is not None and envelope.video.is_inline:
            from omni_homevlog.storage.local import atomic_write_bytes

            target = paths.debug_dir / f"{scene['id']}.mp4"
            atomic_write_bytes(target, envelope.video.decode())
            info = inspect_media(target)
            entry["file"] = str(target)
            entry["duration_s"] = info.duration_s
            entry["dimensions"] = f"{info.width}x{info.height}"
            entry["has_audio"] = info.has_audio

        observations[scene["id"]] = entry

    # Written next to the clips so a reviewer has the scores and the files together.
    import json

    report = root / "regression_run.json"
    report.write_text(
        json.dumps(
            {
                "settings": settings_block,
                "observations": observations,
                "reviewer_note": (
                    "Grade these by eye against the previous run block in "
                    "tests/fixtures/quality_regression_set.yaml. The automated "
                    "scores above are evidence, not the verdict (§21.4)."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    failed = [k for k, v in observations.items() if v["status"] != "PASS"]
    assert not failed, (
        f"{failed} did not render at all, which is a different problem from a "
        f"quality regression. See {report}."
    )
    print(f"\nclips and scores written under {root}")
    print("Now grade them by eye and add a run block to the fixture.")
