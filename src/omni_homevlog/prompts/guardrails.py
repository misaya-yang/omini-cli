"""Prompt shape validation (§9).

§9.1 and §9.5 are unusually specific about how *not* to write these prompts, and
they are specific for a reason: phrasing like

    Shot 1  00:00-00:03 ...
    Shot 2  00:03-00:06 ...
    Shot 3  00:06-00:09 ...

actively teaches the model the visual grammar of a storyboard — panel numbering,
timecode overlays, hard cuts. The model then renders those artefacts into the
video, and the resulting clip is unusable and expensive to discover.

So the compiler validates its own output before it can reach a provider. Every
check here maps to a bullet in §9.1 or §9.5, and the unit tests (§21.1) assert the
same three properties the plan calls out:

  * no timecode formats
  * no shot numbering
  * the no-text/no-UI constraint is always present
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from omni_homevlog.errors import OmniVlogError

#: `00:00-00:03`, `0:00–0:03`, `[00:01]`, `at 0:05` — any clock-like token.
_TIMECODE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b\d{1,2}:\d{2}\b"),  # 00:03, 0:03
    re.compile(r"\b\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}\b"),  # 00:00-00:03
    re.compile(r"\btimecode[s]?\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}\s*(?:s|sec|seconds)\s*[-–—]\s*\d{1,2}\s*(?:s|sec|seconds)\b"),
]

#: Colon tokens that are NOT timecodes.
#:
#: `9:16` and `16:9` are aspect ratios and must appear in these prompts, so a bare
#: `\d{1,2}:\d{2}` check produces a false positive on the one string every
#: vertical video prompt has to contain. The guardrail would then be unsatisfiable
#: without deleting the aspect ratio, which is exactly the wrong fix.
_SAFE_COLON_TOKENS = frozenset({"9:16", "16:9", "1:1", "4:3", "3:2", "2:3", "21:9", "4:5", "5:4"})

#: `Shot 1`, `shot #2`, `Scene 3`, `Panel 4`, `Frame 5:`.
_SHOT_NUMBERING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:shot|scene|panel|frame|cut|take)\s*#?\s*\d+\b", re.IGNORECASE),
    re.compile(r"^\s*\d+[\.\)]\s+", re.MULTILINE),  # a numbered list
    re.compile(r"\bshot\s+(?:one|two|three|four|five|six)\b", re.IGNORECASE),
]

#: Terms that describe an edit of *the film* rather than of a moment.
_MONTAGE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bmontage\b", re.IGNORECASE),
    re.compile(r"\b(?:jump|hard)\s+cut[s]?\b", re.IGNORECASE),
    re.compile(r"\b(?:then\s+cut\s+to|cut\s+to)\b", re.IGNORECASE),
    re.compile(r"\bsplit\s+screen\b", re.IGNORECASE),
    re.compile(r"\bcollage\b", re.IGNORECASE),
    re.compile(r"\bstoryboard\b", re.IGNORECASE),
    re.compile(r"\btransition[s]?\s+to\b", re.IGNORECASE),
]

#: The terms a prompt must forbid somewhere in a prohibition clause.
#:
#: Checked as "does a prohibition clause name all of these", not as a substring
#: match against canned phrases. The templates phrase the ban differently by design
#: ("no on-screen text, timestamps, ..." in the seed prompt, "Do not add any text,
#: timestamp, subtitles, ..." in an edit), and a phrase list would force the
#: templates to be written to satisfy the checker rather than to be clear.
_REQUIRED_CONSTRAINT_TOKENS = ("text", "timestamp", "subtitle")


@dataclass(slots=True)
class PromptViolation:
    rule: str
    detail: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail} (near: {self.excerpt!r})"


class PromptShapeError(OmniVlogError, ValueError):
    """A compiled prompt violates §9. Carries every violation, not just the first.

    Inherits `OmniVlogError` so the CLI's top-level handler reports it as a
    diagnosable error with a code instead of a bare traceback. It keeps `ValueError`
    as well, because `PromptCompiler.compile_edit` documents that it raises one and
    callers catch that.
    """

    code = "prompt_shape"

    def __init__(self, violations: list[PromptViolation]) -> None:
        self.violations = violations
        message = "Compiled prompt violates the §9 prompt rules:\n" + "\n".join(
            f"  - {v}" for v in violations
        )
        OmniVlogError.__init__(self, message, detail={"violations": [str(v) for v in violations]})


def _excerpt(text: str, match: re.Match[str], pad: int = 40) -> str:
    start = max(0, match.start() - pad)
    end = min(len(text), match.end() + pad)
    return text[start:end].replace("\n", " ")


#: Words that turn a list into a prohibition.
_NEGATIONS = ("no ", "not ", "never ", "without ", "avoid", "free of", "must not", "do not")

#: A clause boundary. A negation before one of these does not govern the match.
_CLAUSE_BREAK = re.compile(r"[.;:!?\n]|\s--\s")


def in_prohibition_clause(text: str, match: re.Match[str]) -> bool:
    """Is this match inside a clause the prompt is *forbidding*?

    The prohibition clause lists the banned terms by name ("no on-screen text,
    timestamps, ..., rapid montage, hard cuts"), so the guardrail has to tell a
    ban apart from an invitation. A naive fixed-width lookbehind fails here
    because the list is long and "no" sits dozens of characters earlier.

    So we look back to the start of the *clause* — the previous sentence
    punctuation, colon, semicolon, or line break — and ask whether a negation
    appears anywhere in it. That is what governs a list.
    """
    window_start = 0
    for boundary in _CLAUSE_BREAK.finditer(text, 0, match.start()):
        window_start = boundary.end()

    clause = text[window_start : match.start()].lower()
    return any(negation in clause for negation in _NEGATIONS)


def _clauses(text: str) -> list[str]:
    """Split into clauses on sentence punctuation, colons, and line breaks."""
    return [c.strip() for c in _CLAUSE_BREAK.split(text) if c.strip()]


def prohibition_clauses(text: str) -> list[str]:
    """Every clause that forbids something."""
    return [c for c in _clauses(text) if any(n in c.lower() for n in _NEGATIONS)]


def missing_constraint_terms(text: str) -> list[str]:
    """Which of text/timestamp/subtitle the prompt never forbids.

    A prompt satisfies §21.1 when a *prohibition* names these artefacts. Naming
    them outside a prohibition (for instance "the timestamp is in the corner")
    would be describing the problem, not preventing it.
    """
    prohibiting = " ".join(prohibition_clauses(text)).lower()
    return [term for term in _REQUIRED_CONSTRAINT_TOKENS if term not in prohibiting]


def find_violations(text: str) -> list[PromptViolation]:
    """Every §9 violation in `text`, collected rather than short-circuited."""
    violations: list[PromptViolation] = []

    for pattern in _TIMECODE_PATTERNS:
        for match in pattern.finditer(text):
            if match.group(0).strip() in _SAFE_COLON_TOKENS:
                continue
            violations.append(
                PromptViolation(
                    rule="no_timecodes",
                    detail=(
                        "§9.1 forbids clock-like time references; they teach the model "
                        "to render a timecode overlay"
                    ),
                    excerpt=_excerpt(text, match),
                )
            )

    for pattern in _SHOT_NUMBERING_PATTERNS:
        for match in pattern.finditer(text):
            violations.append(
                PromptViolation(
                    rule="no_shot_numbering",
                    detail=(
                        "§9.1 forbids shot/scene numbering; it pushes the model toward "
                        "storyboard and montage grammar"
                    ),
                    excerpt=_excerpt(text, match),
                )
            )

    for pattern in _MONTAGE_PATTERNS:
        for match in pattern.finditer(text):
            # A prohibition must be able to name the thing it forbids. If the term
            # sits inside a clause that opens with a negation, it is banned, not
            # invited.
            if in_prohibition_clause(text, match):
                continue
            violations.append(
                PromptViolation(
                    rule="no_montage_language",
                    detail=(
                        "§11.3 treats montage/slideshow output as a hard-reject defect; "
                        "the prompt must not invite it even to prohibit it elsewhere"
                    ),
                    excerpt=_excerpt(text, match),
                )
            )

    if not prohibition_clauses(text):
        violations.append(
            PromptViolation(
                rule="missing_no_text_constraint",
                detail=(
                    "§21.1 requires every prompt to forbid on-screen text and UI; "
                    "this prompt contains no prohibition clause at all"
                ),
                excerpt=text[:80],
            )
        )

    missing_tokens = missing_constraint_terms(text)
    if missing_tokens:
        violations.append(
            PromptViolation(
                rule="incomplete_no_text_constraint",
                detail=(
                    "a prohibition clause must name text, timestamps, and subtitles; "
                    f"never forbidden: {missing_tokens}"
                ),
                excerpt=text[:120],
            )
        )

    return violations


def assert_prompt_shape(text: str) -> None:
    """Raise `PromptShapeError` if the prompt violates §9."""
    violations = find_violations(text)
    if violations:
        raise PromptShapeError(violations)


def count_distinct_actions(text: str) -> int:
    """Rough count of action verbs in the body.

    §7.3 caps a segment at `max_distinct_actions`. This is a heuristic — it
    over-counts on prose that mentions the same action twice — so the compiler uses
    it to *warn*, and the Director prompt is what actually constrains the plan.
    """
    verbs = re.findall(
        r"\b(?:stands?|sits?|walks?|turns?|lifts?|lowers?|reaches?|opens?|closes?|"
        r"picks?\s+up|puts?\s+down|smiles?|looks?|glances?|leans?|steps?|moves?|"
        r"adjusts?|touches?|holds?|drinks?|sips?|covers?|extends?|guides?)\b",
        text,
        flags=re.IGNORECASE,
    )
    return len(verbs)


def strip_shot_numbering(text: str) -> str:
    """Remove shot numbering from a model-produced string.

    Used on Director output, which is model-authored prose and may arrive with
    "Shot 1:" headings despite being told not to. The video prompt is compiled
    from structured fields rather than free prose, so this is a second line of
    defence for the *critic* prompt and for anything echoed into a log.
    """
    out = text
    for pattern in _SHOT_NUMBERING_PATTERNS:
        out = pattern.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()
