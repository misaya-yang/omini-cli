"""§21.1: prompt shape rules.

The plan names three properties explicitly:
  * prompts contain no timecode formats
  * prompts contain no shot numbering
  * prompts always contain the no-text/UI constraint

These are cheap to check and expensive to get wrong, which is the whole point of
the guardrail module.
"""

from __future__ import annotations

import pytest

from omni_homevlog.prompts import edit as edit_prompts
from omni_homevlog.prompts import extend as extend_prompts
from omni_homevlog.prompts import guardrails
from omni_homevlog.prompts.compiler import PromptCompiler
from omni_homevlog.prompts.guardrails import PromptShapeError
from omni_homevlog.schemas import ContinuityBible, ProjectSpec, ReferenceAsset, SegmentPlan

# ── the three named properties ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "Shot 1 00:00-00:03 she sits down.",
        "At 0:05 she stands up.",
        "Segment runs 0:00 to 0:10.",
        "The shot lasts 3s - 6s.",
        "Timecode 00:12 appears on screen.",
    ],
)
def test_timecodes_are_rejected(bad: str) -> None:
    violations = guardrails.find_violations(bad)
    assert any(v.rule == "no_timecodes" for v in violations), (
        f"expected a timecode violation in {bad!r}, got {violations}"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "Shot 1: she drinks tea.",
        "Scene 3 begins in the bedroom.",
        "Panel 4 shows the doorway.",
        "Cut 2 is a close-up.",
        "1. She stands\n2. She walks",
    ],
)
def test_shot_numbering_is_rejected(bad: str) -> None:
    violations = guardrails.find_violations(bad)
    assert any(v.rule == "no_shot_numbering" for v in violations), (
        f"expected a shot-numbering violation in {bad!r}, got {violations}"
    )


def test_missing_no_text_constraint_is_rejected() -> None:
    violations = guardrails.find_violations(
        "A calm handheld shot of a woman at a table in warm light."
    )
    rules = {v.rule for v in violations}
    assert "missing_no_text_constraint" in rules


def test_the_plan_section_9_5_example_is_rejected() -> None:
    """The exact anti-pattern §9.5 calls out."""
    forbidden = (
        "Shot 1 00:00-00:03 she sits at the table.\n"
        "Shot 2 00:03-00:06 she stands up.\n"
        "Shot 3 00:06-00:09 she walks to the bedroom."
    )
    violations = guardrails.find_violations(forbidden)
    rules = {v.rule for v in violations}
    assert "no_timecodes" in rules
    assert "no_shot_numbering" in rules


def test_prohibition_clause_is_allowed_to_name_banned_things() -> None:
    """A prohibition must be able to say "no hard cuts" without tripping the check.

    Without the negation lookbehind the guardrail would reject its own required
    prohibition clause, and the only way to satisfy it would be to remove the
    clause, which is exactly backwards.
    """
    text = (
        "A calm handheld shot. No on-screen text, timestamps, subtitles, captions, "
        "logos, watermarks, storyboard panels, split screen, collage, rapid montage, "
        "or hard cuts."
    )
    violations = guardrails.find_violations(text)
    assert not [v for v in violations if v.rule == "no_montage_language"], violations


def test_montage_invitation_is_rejected() -> None:
    violations = guardrails.find_violations(
        "Then cut to the bedroom for a split screen montage. No text, timestamps, or subtitles."
    )
    assert any(v.rule == "no_montage_language" for v in violations)


# ── compiled prompts satisfy the rules ─────────────────────────────────────


@pytest.fixture
def compiler(bible: ContinuityBible, segments: list[SegmentPlan], reference_asset: ReferenceAsset):
    return PromptCompiler(
        bible=bible,
        references=[reference_asset],
        spec=ProjectSpec(title="t", brief="b", provider="vertex", project="p", aspect_ratio="9:16"),
    )


def test_compiled_seed_prompt_passes_every_rule(compiler: PromptCompiler, segments) -> None:
    compiled = compiler.compile_seed(segments[0])
    assert compiled.kind == "seed"
    guardrails.assert_prompt_shape(compiled.text)  # raises on violation

    lowered = compiled.text.lower()
    for marker in ("no on-screen text", "timestamps", "subtitles"):
        assert marker in lowered, f"compiled seed prompt is missing {marker!r}"


def test_compiled_extend_prompt_passes_every_rule(compiler: PromptCompiler, segments) -> None:
    compiled = compiler.compile_extend(segments[1], previous_anchor=segments[0].continuation_anchor)
    guardrails.assert_prompt_shape(compiled.text)

    lowered = compiled.text.lower()
    assert "continue directly from the exact final moment" in lowered
    assert "no on-screen text" in lowered


def test_extend_prompt_names_the_previous_anchor(compiler: PromptCompiler, segments) -> None:
    """Naming the concrete prior state is the anti-reset measure."""
    anchor = segments[0].continuation_anchor
    compiled = compiler.compile_extend(segments[1], previous_anchor=anchor)
    assert anchor.rstrip(".") in compiled.text


def test_extend_prompt_does_not_restate_the_story(compiler: PromptCompiler, segments) -> None:
    """§9.1: an extension describes only the next action.

    Re-describing the apartment and the character gives the model permission to
    re-establish them, which is the scene reset §18 forbids.
    """
    compiled = compiler.compile_extend(segments[1], previous_anchor=segments[0].continuation_anchor)
    lowered = compiled.text.lower()
    # The seed's start state must not appear in the extension prompt.
    assert segments[0].start_state.lower().rstrip(".") not in lowered
    # And the extension must not re-describe the character from scratch.
    assert "clearly adult woman with stable facial identity" not in lowered


def test_seed_prompt_includes_reference_tokens_in_order(
    compiler: PromptCompiler, segments, reference_asset: ReferenceAsset
) -> None:
    compiled = compiler.compile_seed(segments[0])
    assert "[# References <IMAGE_REF_0>@Image1]" in compiled.text


def test_compiler_rejects_a_regeneration_disguised_as_an_edit(compiler: PromptCompiler) -> None:
    with pytest.raises(ValueError, match=r"not-local change|non-local|one"):
        compiler.compile_edit(
            fix=(
                "Restage the whole scene in a different room and reshoot everything "
                "from a new angle with another person."
            ),
            segment_index=1,
        )


def test_edit_prompt_changes_exactly_one_thing() -> None:
    prompt = edit_prompts.render_edit_prompt(fix="remove the timestamp overlay")
    ok, problems = edit_prompts.validate_edit_prompt(prompt)
    assert ok, problems
    assert "Preserve the entire video exactly as it is" in prompt


def test_edit_prompt_from_an_unmapped_defect_returns_none() -> None:
    """An unrecognised defect should fall through to regeneration, not be guessed at."""
    assert edit_prompts.render_edit_prompt_for_defect(defect="the mood is wrong") is None


def test_edit_prompt_maps_a_known_defect() -> None:
    prompt = edit_prompts.render_edit_prompt_for_defect(defect="a timestamp in the corner")
    assert prompt is not None
    assert "timestamp" in prompt.lower()


def test_profile_prompt_does_not_leak_banned_language_into_the_video_prompt(
    compiler: PromptCompiler, segments
) -> None:
    """The prohibition clause is generated from the bible's own forbidden list."""
    text = compiler.compile_seed(segments[0]).text
    for banned in ("on-screen text", "timestamps", "storyboard panels", "split screen"):
        assert banned in text.lower()


def test_prompt_shape_error_lists_every_violation() -> None:
    with pytest.raises(PromptShapeError) as excinfo:
        guardrails.assert_prompt_shape("Shot 1 00:00-00:03 something happens.")
    rules = {v.rule for v in excinfo.value.violations}
    assert "no_timecodes" in rules
    assert "no_shot_numbering" in rules
    assert "missing_no_text_constraint" in rules


def test_strip_shot_numbering_cleans_director_prose() -> None:
    cleaned = guardrails.strip_shot_numbering("Shot 1: she sits. Shot 2: she stands.")
    assert "Shot 1" not in cleaned
    assert "Shot 2" not in cleaned
    assert "she sits" in cleaned


def test_extension_template_includes_the_flow_instruction() -> None:
    """Physical continuity between the previous pose and the new movement."""
    assert "must flow physically into the new movement" in extend_prompts.FLOW_INSTRUCTION
    assert "do not restart the story" in extend_prompts.FLOW_INSTRUCTION
