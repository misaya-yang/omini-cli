"""Prompt Compiler (§4.1, §9).

Turns a structured plan into the short prompts Omni actually receives, and
**validates every one of them** against §9 before it can reach a provider.

What the compiler owns:

  * writing the no-text/no-UI prohibition into the main prompt, since there is no
    separate negative-prompt parameter (§9.1)
  * keeping the character description word-for-word identical across segments
  * describing only the current ~10 seconds
  * for extensions: only the next action, never a restatement of the story
  * for edits: exactly one local change
  * refusing to emit a prompt that trips `guardrails`

Validation is not advisory. A prompt that fails `assert_prompt_shape` raises, so a
regression in a template cannot quietly start producing storyboard-shaped prompts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omni_homevlog.observability.logging import get_logger
from omni_homevlog.prompts import edit as edit_prompts
from omni_homevlog.prompts import extend as extend_prompts
from omni_homevlog.prompts import guardrails
from omni_homevlog.prompts import seed as seed_prompts
from omni_homevlog.schemas import (
    ContinuityBible,
    ProjectSpec,
    ReferenceAsset,
    SegmentPlan,
)

logger = get_logger("compiler")


@dataclass(slots=True)
class CompiledPrompt:
    """A prompt plus the provenance needed to audit it later."""

    text: str
    kind: str  # "seed" | "extend" | "edit"
    segment_index: int
    warnings: list[str] = field(default_factory=list)
    char_count: int = 0

    def __post_init__(self) -> None:
        self.char_count = len(self.text)


class PromptCompiler:
    """Compiles structured plans into provider prompts.

    Holds the job's `ContinuityBible` and reference set so every segment is
    compiled from identical continuity inputs. That is what makes the character
    description stable across segments — not discipline, but construction.
    """

    def __init__(
        self,
        *,
        bible: ContinuityBible,
        references: list[ReferenceAsset] | None = None,
        spec: ProjectSpec | None = None,
    ) -> None:
        self.bible = bible
        self.references = references or []
        self.spec = spec

    # ─ seed ───────────────────────────────────────────────────────────────

    def compile_seed(self, segment: SegmentPlan) -> CompiledPrompt:
        """Compile the initial segment."""
        text = seed_prompts.render_seed_prompt_for_plan(
            plan_segment=segment,
            bible=self.bible,
            references=self.references,
            duration_s=segment.intended_duration_s,
            aspect_ratio=self.spec.aspect_ratio if self.spec else "9:16",
        )
        from omni_homevlog.providers.request_builder import seed_input_mode
        task, mode = seed_input_mode([a.role for a in self.references])
        if task == "image_to_video":
            instruction = "Use the first supplied image as the literal opening frame."
            if mode == "first_last_frame":
                instruction += " Use the second supplied image as the literal closing frame and connect them in one continuous motion."
            text = text.replace(seed_prompts.REFERENCE_INSTRUCTION, instruction).replace(seed_prompts.CLOSING, "Preserve the supplied frame composition without adding text, panels or cuts.")
        warnings = self._check_action_density(segment, text)
        guardrails.assert_prompt_shape(text)
        return CompiledPrompt(
            text=text, kind="seed", segment_index=segment.index, warnings=warnings
        )

    # ─── extension ─────────────────────────────────────────────────────────

    def compile_extend(
        self,
        segment: SegmentPlan,
        *,
        previous_anchor: str | None = None,
        include_camera: bool = False,
    ) -> CompiledPrompt:
        """Compile an extension for `segment` (which follows the previous one).

        Kept short on purpose. `previous_anchor` is appended as a concrete state to
        continue from, which is the single most effective anti-reset measure.
        """
        renderer = (
            extend_prompts.render_extend_prompt_with_camera
            if include_camera
            else extend_prompts.render_extend_prompt
        )
        text = renderer(
            segment=segment,
            bible=self.bible,
            duration_s=segment.intended_duration_s,
        )

        note = extend_prompts.continuation_note(previous_anchor)
        if note:
            text = f"{text}\n\n{note}"

        warnings = self._check_action_density(segment, text)
        guardrails.assert_prompt_shape(text)
        return CompiledPrompt(
            text=text, kind="extend", segment_index=segment.index, warnings=warnings
        )

    # ── edit ───────────────────────────────────────────────────────────────

    def compile_edit(self, *, fix: str, segment_index: int) -> CompiledPrompt:
        """Compile a single-fix edit prompt."""
        text = edit_prompts.render_edit_prompt(fix=fix)
        ok, problems = edit_prompts.validate_edit_prompt(text)
        if not ok:
            # An edit prompt that is really a regeneration must not go out through
            # the edit path; the caller should regenerate instead (§11.4).
            raise ValueError("Edit prompt rejected as a non-local change: " + "; ".join(problems))
        guardrails.assert_prompt_shape(text)
        return CompiledPrompt(text=text, kind="edit", segment_index=segment_index)

    def compile_edit_for_defect(self, *, defect: str, segment_index: int) -> CompiledPrompt | None:
        """Compile an edit prompt from a Critic defect string, or None if unmapped."""
        text = edit_prompts.render_edit_prompt_for_defect(defect=defect)
        if text is None:
            return None
        return self.compile_edit(fix=_extract_fix(text), segment_index=segment_index)

    def compile_edit_from_suggestion(
        self, *, suggestion: str, segment_index: int
    ) -> CompiledPrompt | None:
        """Wrap a Critic's free-text suggestion into a validated edit prompt.

        §24.8 forbids executing a Critic's prose as a paid call, and there was a
        path that did exactly that: when no canned defect hint matched, the
        orchestrator handed `report.suggested_edit_prompt` straight to
        `provider.edit`, bypassing the preservation wrapper, the single-fix check,
        and every §9 guardrail.

        The consequence is not theoretical. The Critic reads frames rendered by the
        video model, so text the model painted into a frame can steer the next paid
        call — and a reply like "remove the flicker at 00:07; cut to a closer
        framing" would be dispatched verbatim, teaching the model the
        timecode-and-cut grammar this project exists to keep out.

        Returns None when the suggestion cannot be made to satisfy §9, so the caller
        falls through to regeneration rather than sending something unvalidated.
        """
        cleaned = suggestion.strip()
        if not cleaned:
            return None

        try:
            return self.compile_edit(fix=cleaned, segment_index=segment_index)
        except (ValueError, guardrails.PromptShapeError) as exc:
            logger.warning(
                "Refusing a Critic's suggested edit: it does not survive the §9 prompt "
                "rules, so sending it would teach the model the grammar this project "
                "keeps out",
                extra={
                    "extra_fields": {
                        "segment": segment_index,
                        "error": str(exc)[:300],
                        "suggestion_preview": cleaned[:160],
                    }
                },
            )
            return None

    # ── shared ─────────────────────────────────────────────────────────────

    def _check_action_density(self, segment: SegmentPlan, text: str) -> list[str]:
        """§7.3 caps distinct actions per segment. Warn, do not fail.

        The verb counter over-counts on natural prose, so failing here would reject
        good prompts. The Director prompt is what actually enforces the cap; this is
        a signal that the plan drifted.
        """
        count = guardrails.count_distinct_actions(text)
        if count > segment.max_distinct_actions * 2:
            message = (
                f"segment {segment.index} prompt mentions {count} action verbs, well "
                f"above the plan's cap of {segment.max_distinct_actions}; the render "
                "may read as busy or montage-like"
            )
            logger.warning(message, extra={"extra_fields": {"segment": segment.index}})
            return [message]
        return []

    def describe_for_review(self, segment: SegmentPlan, kind: str) -> str:
        """The short segment description handed to the Critic."""
        if kind == "extend":
            return (
                f"Extension segment {segment.index}: starting from the previous "
                f"segment's final moment, {segment.action}"
            )
        return f"Segment {segment.index}: {segment.start_state} Then {segment.action}"


def _extract_fix(rendered_edit_prompt: str) -> str:
    """Pull the correction sentence back out of a rendered edit prompt."""
    body = rendered_edit_prompt.replace(edit_prompts.PRESERVE_HEADER, "")
    body = body.replace(edit_prompts.PRESERVE_TAIL, "")
    return body.strip().strip(".").strip() or "apply the requested correction"


def compile_for_segment(
    *,
    compiler: PromptCompiler,
    segment: SegmentPlan,
    is_extension: bool,
    previous_anchor: str | None = None,
    include_camera: bool = False,
) -> CompiledPrompt:
    """Route to the right template. The pipeline's single entry point."""
    if is_extension:
        return compiler.compile_extend(
            segment, previous_anchor=previous_anchor, include_camera=include_camera
        )
    return compiler.compile_seed(segment)
