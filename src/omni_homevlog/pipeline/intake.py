"""Intake and the Reference Sanitizer (§6).

This module exists to prevent one specific, expensive, silent failure: feeding a
storyboard, a nine-panel collage, or a player screenshot to the video model as a
character reference. The model then renders the panel borders, the shot numbers,
and the `00:00-00:03` timecodes **into the video**. It looks like a bug in the
video model. It is not; it is a bug in the input.

So intake runs two passes (§6.2):

  **Local** (`media/inspect_image.py`) — decodability, size, aspect ratio, a grid
  and letterbox detector, perceptual-hash duplicates, EXIF and file hashes. Cheap,
  and it catches the unambiguous cases before we spend a vision-model call.

  **Vision** — a structured JSON check for text overlays, timestamps, UI controls,
  collage structure, and multiple distinct people. This catches what pixels alone
  cannot: a clean-looking photo that nonetheless has a small timestamp in a corner.

Hard-reject conditions (§6.2), any one of which blocks the asset for identity use:
  * `has_timestamp` — the exact failure the plan is built around
  * `has_ui_controls`
  * `is_collage` when the role is an identity reference
  * multiple distinct people
  * an unverifiable adult subject
  * `provenance == "unknown"` when the user asked to replicate a real person

A degraded vision pass is treated as **not clean**, never as approval. If the
check could not run, we say so and require the operator to acknowledge it
explicitly rather than defaulting to "probably fine".
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from omni_homevlog.agents.llm import InlineImage, TextModelClient, build_client
from omni_homevlog.config import Settings, get_settings
from omni_homevlog.errors import PolicyBlockedError, ReferenceRejectedError
from omni_homevlog.media.inspect_image import (
    LocalImageReport,
    find_near_duplicates,
    inspect_image_local,
    mime_for_path,
)
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.schemas import (
    IDENTITY_ROLES,
    ReferenceAsset,
    ReferenceRejection,
    SanitizerReport,
    utc_now_iso,
)
from omni_homevlog.storage.local import JobPaths, atomic_write_json

logger = get_logger("intake")


# ── reference specifications ────────────────────────────────────────────────

#: §6.1's recommended combination. Not a hard requirement, but a job with no
#: identity reference at all cannot do identity-preserving work, and saying so up
#: front is better than discovering it in the output.
RECOMMENDED_ROLES = ("identity_closeup", "identity_body", "environment")


SANITIZER_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "has_text_overlay",
        "has_timestamp",
        "has_ui_controls",
        "is_collage",
        "multiple_distinct_people",
        "is_clean_identity_reference",
    ],
    "properties": {
        "has_text_overlay": {"type": "boolean"},
        "has_timestamp": {"type": "boolean"},
        "has_ui_controls": {"type": "boolean"},
        "is_collage": {"type": "boolean"},
        "multiple_distinct_people": {"type": "boolean"},
        "is_clean_identity_reference": {"type": "boolean"},
        "subject_is_adult": {"type": "boolean"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}

SANITIZER_PROMPT = """Inspect this image and report what it contains.

Answer these as booleans:
- has_text_overlay: is there any text rendered into the image itself? Titles, captions,
  labels, watermarks, channel names, or burned-in writing.
- has_timestamp: is there a clock, timecode, date stamp, or duration readout anywhere
  in the image, including small ones in a corner or along an edge?
- has_ui_controls: are there playback controls, progress bars, scrubbers, buttons,
  menu chrome, or device interface elements visible?
- is_collage: is this a multi-panel layout — a storyboard grid, a contact sheet, a
  before/after split, a screenshot of an image gallery, or several frames arranged
  together with borders or gaps between them?
- multiple_distinct_people: are there two or more recognisably different people in the
  image? Two views of the SAME person in different panels is still a collage, but it is
  not "multiple distinct people".
- is_clean_identity_reference: is this a single, clean photograph of one person,
  suitable as a character reference for video generation? False if any of the above
  artefact flags is true, or if the subject is not a person.
- subject_is_adult: does the person appear to be an adult? Answer false if they appear
  to be a minor, and true if they appear to be an adult. If no person is present, omit
  this field.

Also return a `warnings` array of short strings for anything else notable — heavy
filters, extreme motion blur, the face being mostly hidden, heavy vignetting, or
visible compression artefacts.

Report only what you can actually see. Do not guess."""


@dataclass(slots=True)
class IntakeResult:
    """Outcome of sanitizing one batch of references."""

    assets: list[ReferenceAsset] = field(default_factory=list)
    rejections: list[ReferenceRejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate_pairs: list[tuple[int, int]] = field(default_factory=list)
    vision_pass_ran: bool = False
    vision_pass_degraded: bool = False

    @property
    def approved(self) -> list[ReferenceAsset]:
        return [a for a in self.assets if a.approved]

    def ok(self) -> bool:
        return not self.rejections


class ReferenceSanitizer:
    """Two-pass reference validator."""

    def __init__(
        self,
        *,
        client: TextModelClient | None = None,
        settings: Settings | None = None,
        run_vision_pass: bool = True,
        allow_degraded_vision: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self.run_vision_pass = run_vision_pass
        # An operator can accept a run without the vision pass, but must say so.
        # Default is to refuse, because the whole point of this module is that
        # "we could not check" must not read as "it is fine".
        self.allow_degraded_vision = allow_degraded_vision

    @property
    def client(self) -> TextModelClient:
        if self._client is None:
            self._client = build_client(
                model=self.settings.critic_model,
                provider=self.settings.omni_provider,
                settings=self.settings,
                location=self.settings.critic_location,
            )
        return self._client

    # ── the two passes ─────────────────────────────────────────────────────

    def check_local(self, path: Path) -> LocalImageReport:
        return inspect_image_local(path)

    def check_vision(self, path: Path) -> SanitizerReport:
        """Structured vision check. Never raises for a model failure — it degrades."""
        import base64

        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            return SanitizerReport(
                degraded=True,
                degradation_reason=f"could not read {path.name}: {exc}",
                warnings=["vision check could not run"],
            )

        try:
            payload = self.client.generate_json(
                prompt=SANITIZER_PROMPT,
                images=[InlineImage(data_b64=data, mime_type=mime_for_path(path) or "image/png")],
                response_schema=SANITIZER_RESPONSE_SCHEMA,
                temperature=0.0,
                max_output_tokens=1024,
            )
        except Exception as exc:
            logger.warning(
                "Vision sanitizer pass failed; treating the asset as unchecked",
                extra={"extra_fields": {"path": str(path), "error": str(exc)}},
            )
            return SanitizerReport(
                degraded=True,
                degradation_reason=f"{type(exc).__name__}: {exc}",
                warnings=["vision check could not run"],
            )

        required = SANITIZER_RESPONSE_SCHEMA["required"]
        assert isinstance(required, list)
        if not isinstance(payload, dict) or any(type(payload.get(k)) is not bool for k in required):
            return SanitizerReport(degraded=True, degradation_reason="Sanitizer returned missing or non-boolean required fields")
        if payload.get("subject_is_adult") is not None and type(payload["subject_is_adult"]) is not bool:
            return SanitizerReport(degraded=True, degradation_reason="Sanitizer returned an invalid adult-subject field")
        warnings = payload.get("warnings") or []
        return SanitizerReport(
            has_text_overlay=bool(payload.get("has_text_overlay", False)),
            has_timestamp=bool(payload.get("has_timestamp", False)),
            has_ui_controls=bool(payload.get("has_ui_controls", False)),
            is_collage=bool(payload.get("is_collage", False)),
            multiple_distinct_people=bool(payload.get("multiple_distinct_people", False)),
            is_clean_identity_reference=bool(payload.get("is_clean_identity_reference", False)),
            subject_is_adult=payload.get("subject_is_adult"),
            warnings=[str(w) for w in warnings if isinstance(w, (str, int, float))],
        )

    # ── the decision ───────────────────────────────────────────────────────

    def evaluate(
        self,
        *,
        asset: ReferenceAsset,
        local: LocalImageReport,
        vision: SanitizerReport | None,
        requires_identity: bool,
    ) -> tuple[bool, list[str]]:
        """Apply §6.2's hard-reject conditions. Returns `(approved, reasons)`."""
        reasons: list[str] = []

        reasons.extend(local.hard_rejections)

        if requires_identity and local.grid_score >= 0.55:
            reasons.append(
                "the local check found grid, collage, or player-chrome structure in an "
                "image being used as an identity reference — this is the single most "
                "common cause of storyboard artefacts appearing in generated video"
            )

        if vision is not None and not vision.degraded:
            if vision.has_timestamp:
                reasons.append("a timestamp, clock, or timecode is visible in the image")
            if vision.has_ui_controls:
                reasons.append("player or device interface controls are visible")
            if vision.is_collage and requires_identity:
                reasons.append(
                    "the image is a multi-panel collage or storyboard and cannot be "
                    "used as an identity reference"
                )
            if vision.multiple_distinct_people:
                reasons.append("more than one distinct person appears in the image")
            if requires_identity and vision.subject_is_adult is not True:
                reasons.append("an adult subject could not be confirmed for this identity reference")
            if requires_identity and not vision.is_clean_identity_reference:
                reasons.append("the vision check did not confirm a clean identity reference")
            if vision.has_text_overlay:
                reasons.append("text is rendered into the image itself")

        if requires_identity and asset.provenance == "unknown":
            reasons.append(
                "provenance is 'unknown' and this image would replicate an identifiable "
                "person's likeness; mark it owned, licensed, or synthetic to proceed"
            )

        if (
            (vision is None or vision.degraded)
            and not self.allow_degraded_vision
        ):
            reasons.append(
                "the vision check could not run, so this image is unverified. "
                "Re-run with --allow-degraded-vision to accept unverified "
                "references at your own risk."
            )

        return (not reasons), reasons

    # ─ orchestration ───────────────────────────────────────────────────────

    def sanitize(
        self,
        *,
        references: list[tuple[Path, str, str]],  # (path, role, provenance)
        paths: JobPaths | None = None,
        copy_into_job: bool = True,
    ) -> IntakeResult:
        """Sanitize a batch of references.

        `references` is a list of `(path, role, provenance)` tuples.
        """
        result = IntakeResult()
        locals_: list[LocalImageReport] = []
        staged_paths: list[Path] = []

        for index, (path, role, provenance) in enumerate(references):
            path = Path(path).expanduser().resolve()
            requires_identity = role in IDENTITY_ROLES

            local = self.check_local(path)
            locals_.append(local)

            staged = path
            if paths is not None and copy_into_job and local.sha256:
                staged = self._stage(path, paths, index, role, local.sha256)
                staged_paths.append(staged)

            vision: SanitizerReport | None = None
            if self.run_vision_pass and local.ok:
                # Every role gets the vision pass. Identity references are where a
                # timestamp is most damaging, but an environment reference with a
                # watermark contaminates the render just as thoroughly, and the
                # check is one cheap call.
                vision = self.check_vision(staged if staged.exists() else path)
                result.vision_pass_ran = True
                if vision.degraded:
                    result.vision_pass_degraded = True
                    logger.warning(
                        "Vision sanitizer pass degraded",
                        extra={
                            "extra_fields": {
                                "path": str(path),
                                "reason": vision.degradation_reason,
                            }
                        },
                    )
            elif self.run_vision_pass and not local.ok:
                result.warnings.append(
                    f"{path.name}: skipped the vision check because the local check "
                    "already rejected it"
                )

            asset = ReferenceAsset(
                id=f"ref{index:02d}_{role}",
                path_or_uri=str(staged),
                role=role,
                sha256=local.sha256 or "",
                provenance=provenance,
                approved=False,
                width=local.width,
                height=local.height,
                mime_type=local.mime_type,
                perceptual_hash=local.perceptual_hash,
                sanitizer=vision,
            )

            approved, reasons = self.evaluate(
                asset=asset, local=local, vision=vision, requires_identity=requires_identity
            )
            asset.approved = approved
            result.assets.append(asset)

            if not approved:
                result.rejections.append(ReferenceRejection(asset_id=asset.id, reasons=reasons))
            elif vision and vision.warnings:
                result.warnings.extend(f"{path.name}: {w}" for w in vision.warnings)

            if paths is not None:
                atomic_write_json(
                    paths.sanitizer_path(asset.id),
                    {
                        "asset_id": asset.id,
                        "role": role,
                        "provenance": provenance,
                        "sanitized_at": utc_now_iso(),
                        "local": local.to_dict(),
                        "vision": vision.model_dump(mode="json") if vision else None,
                        "approved": approved,
                        "rejection_reasons": reasons,
                    },
                )

        result.duplicate_pairs = find_near_duplicates(locals_)
        for i, j in result.duplicate_pairs:
            if i < len(result.assets) and j < len(result.assets):
                result.warnings.append(
                    f"{Path(result.assets[i].path_or_uri).name} and "
                    f"{Path(result.assets[j].path_or_uri).name} look like the same photo; "
                    "duplicates add nothing and waste a reference slot"
                )

        return result

    def _stage(self, source: Path, paths: JobPaths, index: int, role: str, sha: str) -> Path:
        """Copy the reference into the job directory under a stable name.

        Named by role and a hash prefix so the job is self-contained and a re-run
        with the same inputs lands on the same filename.
        """
        suffix = source.suffix.lower() or ".png"
        target = paths.references_dir / f"{index:02d}_{role}_{sha[:8]}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
        return target


def require_approved(result: IntakeResult, *, strict: bool = True) -> list[ReferenceAsset]:
    """Raise if any reference was rejected.

    §6.2's rejections are not advisory. Proceeding past a rejected reference is how
    a timestamp ends up baked into a 30-second deliverable, so the default is to
    stop and make the operator look at the reasons.
    """
    if result.rejections and strict:
        lines = [f"  - {r.asset_id}: " + "; ".join(r.reasons) for r in result.rejections]
        raise ReferenceRejectedError(
            "Reference validation rejected one or more images:\n" + "\n".join(lines),
            detail={"rejections": [r.model_dump(mode="json") for r in result.rejections]},
        )
    return result.approved


def describe_result(result: IntakeResult) -> str:
    """Human-readable summary for the CLI."""
    lines: list[str] = []
    for asset in result.assets:
        status = "APPROVED" if asset.approved else "REJECTED"
        dims = f"{asset.width}x{asset.height}" if asset.width else "unknown size"
        lines.append(f"{asset.id:<28} {status:<9} {dims:<12} role={asset.role}")
    for rejection in result.rejections:
        for reason in rejection.reasons:
            lines.append(f"    ↳ {rejection.asset_id}: {reason}")
    for warning in result.warnings:
        lines.append(f"  warning: {warning}")
    return "\n".join(lines)


def blocked_error(reasons: list[str]) -> PolicyBlockedError:
    return PolicyBlockedError("Intake blocked: " + "; ".join(reasons), detail={"reasons": reasons})
