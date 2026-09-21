"""Content credentials (§22, §24.13).

The rule is short: **detect, record, never strip.**

`synthid_expected` is a separate idea from `c2pa_present` and the manifest keeps
them apart, because they are different claims:

  * C2PA is a manifest box we can look for in the file. We can prove its
    presence or absence.
  * SynthID is a watermark. We cannot verify it locally, and the plan's manifest
    field is called `synthid_expected` precisely because it is an expectation
    about the provider, not a measurement. Recording it as a detection would be
    a false claim.

If a C2PA manifest is present we surface it and carry the file forward unchanged
wherever possible. `media/transcode.py` records before/after state on every
transform so that a lost manifest is reported rather than discovered later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omni_homevlog.media.ffprobe import detect_c2pa
from omni_homevlog.observability.logging import get_logger

logger = get_logger("c2pa")

#: Providers known to attach content credentials to generated video.
_SYNTHID_PROVIDERS = {"vertex", "gemini_api"}


@dataclass(slots=True)
class ContentCredentialReport:
    c2pa_present: bool | None
    c2pa_detail: list[str]
    synthid_expected: bool
    synthid_verifiable_locally: bool = False
    notes: list[str] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "c2pa_present": self.c2pa_present,
            "c2pa_detail": self.c2pa_detail,
            "synthid_expected": self.synthid_expected,
            "synthid_verifiable_locally": self.synthid_verifiable_locally,
            "notes": self.notes or [],
        }


def inspect_content_credentials(path: str | Path, *, provider: str) -> ContentCredentialReport:
    """Look for C2PA in a generated file and note the SynthID expectation."""
    c2pa_present, detail = detect_c2pa(path)

    notes: list[str] = []
    if c2pa_present is False:
        notes.append(
            "No C2PA manifest box found. For an Omni output this may mean the "
            "provider did not attach one, or that a transcode dropped it — check "
            "whether this file is marked `derived` in the manifest."
        )
    elif c2pa_present is None:
        notes.append("C2PA state could not be determined for this file type.")

    return ContentCredentialReport(
        c2pa_present=c2pa_present,
        c2pa_detail=detail,
        synthid_expected=provider in _SYNTHID_PROVIDERS,
        notes=notes,
    )


def assert_not_stripped(before: bool | None, after: bool | None) -> tuple[bool, str]:
    """Did a transform lose content credentials?

    Callers use this to decide whether to warn. We never "fix" it by re-adding a
    manifest — that would mean fabricating provenance.
    """
    if before and not after:
        return False, (
            "C2PA was present before the transform and is absent after it. "
            "Mark the output as derived and keep the original as the "
            "credential-bearing artifact."
        )
    return True, "ok"
