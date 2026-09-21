"""Local-edit prompt template (§9.4).

§9.1: "编辑段只改变一个问题" — the edit prompt changes exactly one thing.

§11.4 lists when an edit is the right call: the identity, action, and space are
already correct, the problem is single and local, and fixing it does not require
restaging the shot. Typical cases are a stray timestamp in a corner, one bad hand,
a lost hair accessory, or over-smoothed skin.

The template therefore leads with preservation and states the fix once. Adding a
second fix does not make the edit "more efficient"; it broadens the diff, and the
model applies unrequested changes to whatever else it touches.
"""

from __future__ import annotations

import re

from omni_homevlog.prompts import guardrails

PRESERVE_HEADER = "Preserve the entire video exactly as it is except for the following correction:"

PRESERVE_TAIL = (
    "Keep the same timing, identity, face, hair, outfit, background, camera path, "
    "lighting, audio, and all unaffected objects. Do not redesign or restage the "
    "scene. Do not add any text, timestamp, subtitles, logos, UI, or new people."
)

#: Defect categories the Critic can report, mapped to the kind of correction that
#: fixes them. Used to sanity-check that a suggested edit matches its defect.
DEFECT_TO_FIX_HINT: dict[str, str] = {
    "timestamp": "remove the timestamp overlay from the corner of the frame",
    "text": "remove the on-screen text without changing anything else",
    "subtitle": "remove the subtitle text without changing anything else",
    "logo": "remove the logo or watermark without changing anything else",
    "ui": "remove the interface overlay without changing anything else",
    "hand": "correct the anatomy of the hand so it has five natural fingers",
    "finger": "correct the fingers so they are anatomically natural",
    "accessory": "restore the hair accessory that was present before",
    "hair": "restore the hairstyle to match the rest of the video",
    "skin": "reduce the over-smoothed skin so natural texture returns",
    "lip": "correct the lip sync so the mouth matches the audio",
    "outfit": "restore the original clothing without changing anything else",
}

#: An edit prompt must stay short. A long one is a behavioural sign that someone
#: is trying to smuggle a regeneration in through the edit path.
MAX_EDIT_PROMPT_CHARS = 600


def render_edit_prompt(*, fix: str) -> str:
    """Compile an edit prompt from a single, specific fix."""
    correction = fix.strip().rstrip(".")
    if correction and not correction[0].isupper():
        correction = correction[0].upper() + correction[1:]
    return "\n\n".join([PRESERVE_HEADER, correction + ".", PRESERVE_TAIL])


def render_edit_prompt_for_defect(*, defect: str) -> str | None:
    """Derive an edit prompt from a defect string reported by the Critic.

    Returns None when the defect does not map to a known local fix — the caller
    then falls through to regeneration rather than inventing an instruction. An
    unrecognised defect is more likely to need a re-render than a targeted edit,
    and guessing here would spend an edit attempt on the wrong remedy.
    """
    lowered = defect.lower()
    for key, hint in DEFECT_TO_FIX_HINT.items():
        # Word boundary, not a bare substring. `"text" in "skin texture"` is true,
        # so a skin-smoothing defect was "fixed" by removing on-screen text, and
        # `"ui" in "guide line"` mapped a framing problem onto the interface
        # overlay. Both spent a real edit attempt on the wrong remedy.
        if re.search(rf"\b{re.escape(key)}\b", lowered):
            return render_edit_prompt(fix=hint)
    return None


def validate_edit_prompt(prompt: str) -> tuple[bool, list[str]]:
    """Is this prompt a *local* edit, or a regeneration wearing a disguise?"""
    problems: list[str] = []

    if len(prompt) > MAX_EDIT_PROMPT_CHARS:
        problems.append(
            f"edit prompt is {len(prompt)} chars (limit {MAX_EDIT_PROMPT_CHARS}); "
            "a long prompt usually means more than one change is being requested"
        )

    if PRESERVE_HEADER.split(":")[0].lower() not in prompt.lower():
        problems.append(
            "edit prompt must open with the preservation instruction so the model "
            "knows everything outside the stated fix is frozen"
        )

    body = prompt
    for sentence in (PRESERVE_HEADER, PRESERVE_TAIL):
        body = body.replace(sentence, "")

    # More than one sentence of instructions means more than one fix.
    instructions = [
        s.strip() for s in re.split(r"[.\n]+", body) if s.strip() and len(s.strip()) > 12
    ]
    if len(instructions) > 2:
        problems.append(
            f"edit prompt appears to request {len(instructions)} changes; §9.1 requires exactly one"
        )

    # The preservation clause itself says "do not redesign or restage the scene",
    # so a bare substring search would flag the template against itself. Only a
    # *positive* restaging instruction is a problem, so matches inside a
    # prohibition clause are skipped.
    restage_markers = (
        "restage",
        "reshoot",
        "reimagine",
        "start over",
        "change the scene",
        "different room",
        "another person",
    )
    for marker in restage_markers:
        for match in re.finditer(re.escape(marker), prompt, flags=re.IGNORECASE):
            if guardrails.in_prohibition_clause(prompt, match):
                continue
            problems.append(
                f"edit prompt contains restaging language ({marker!r}); that is a "
                "regeneration, not a local edit (§11.4)"
            )

    return (not problems), problems
