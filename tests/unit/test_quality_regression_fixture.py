"""Quality regression set fixture integrity (§21.4).

These checks read the fixture and assert on it. They need no credentials and make
no API calls, so they live here rather than under `tests/live/` — a broken fixture
should fail in every run, not only when someone opts in to spending money.
"""

from __future__ import annotations

from pathlib import Path

import yaml

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "quality_regression_set.yaml"


def load_regression_set() -> dict:
    with FIXTURE_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_the_regression_set_is_well_formed() -> None:
    """Checks the fixture itself. Free, so it runs even without the live opt-in."""
    data = load_regression_set()

    assert data["version"] == 1
    assert data["updated_at"]

    scenes = data["scenes"]
    assert len(scenes) == 5, "§21.4 fixes five scene types"

    ids = [s["id"] for s in scenes]
    assert len(set(ids)) == len(ids), "scene ids must be unique"

    # The prompts are what make a regression attributable to the model rather than
    # to a prompt edit, so they must stay clean of the shapes §9 forbids.
    from omni_homevlog.prompts.guardrails import find_violations

    for scene in scenes:
        assert scene["exercises"], f"{scene['id']} does not say what it exercises"
        assert scene["watch_for"], f"{scene['id']} has no review guidance"

        violations = find_violations(scene["prompt"])
        timecode_or_numbering = [
            v for v in violations if v.rule in ("no_timecodes", "no_shot_numbering")
        ]
        assert not timecode_or_numbering, (
            f"{scene['id']} prompt would trip the §9 guardrails: {timecode_or_numbering}"
        )


def test_the_five_adjacent_duration_paths_are_all_covered() -> None:
    """The scene list must exercise the same capabilities the chain depends on."""
    data = load_regression_set()
    exercised: set[str] = set()
    for scene in data["scenes"]:
        exercised.update(scene["exercises"])

    for required in (
        "object_contact",
        "hand_anatomy",
        "facial_identity",
        "whole_body_motion",
        "space_traversal",
        "end_anchor",
    ):
        assert required in exercised, (
            f"no scene exercises {required}, so a regression there would go unnoticed"
        )
