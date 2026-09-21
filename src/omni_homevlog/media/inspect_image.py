"""Local image checks for the Reference Sanitizer (§6.2, "本地快速检查").

This is the cheap first pass. It answers the questions that do not need a vision
model:

  * is it a real decodable image, and big enough to be useful
  * is the aspect ratio sane
  * does it look like a *grid* — a storyboard, a screenshot of a video player, a
    collage — as opposed to a single photograph
  * is it a duplicate of another reference (perceptual hash)
  * what are its EXIF and file hashes

The grid detector is the load-bearing one. §6.5 and §24.2 are emphatic that a
nine-panel storyboard must never reach the model as a character reference, and
the failure mode is silent: the model reproduces the panel borders and numbers
into the video. So we look for the specific evidence of a grid — long runs of a
uniform colour forming straight lines across the frame, and repeated near-equal
tiles — and we report the evidence rather than a bare boolean.

Everything here is heuristic. It is a *filter*, not a proof: the vision pass in
`pipeline/intake.py` makes the final call, and a local flag alone never approves
an asset. Only local *rejections* are decisive, and only for unambiguous cases
(a 40-byte file is not an image).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omni_homevlog.observability.logging import get_logger

logger = get_logger("inspect_image")

MIN_SHORT_EDGE = 256
MAX_ASPECT_RATIO = 4.0
MIN_ASPECT_RATIO = 0.25
MIN_FILE_BYTES = 2048

_BASE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_HEIF_SUFFIXES = {".heic", ".heif"}


def _heif_available() -> bool:
    """Whether HEIC/HEIF can actually be decoded in this environment.

    These were listed as supported unconditionally, and Pillow cannot open them
    without the optional `pillow-heif` plugin — so every valid iPhone photo was
    hard-rejected as "not a decodable image". The worst possible behaviour for
    someone whose reference set came off a phone.
    """
    try:
        import pillow_heif  # type: ignore[import-not-found]

        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False


#: Suffixes we can actually decode here.
SUPPORTED_SUFFIXES = _BASE_SUFFIXES | (_HEIF_SUFFIXES if _heif_available() else set())

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


@dataclass(slots=True)
class LocalImageReport:
    path: str
    ok: bool
    sha256: str
    perceptual_hash: str | None = None
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    exif: dict[str, Any] = field(default_factory=dict)
    grid_score: float = 0.0
    uniform_line_fraction: float = 0.0
    tile_repetition_score: float = 0.0
    has_letterbox_bars: bool = False
    warnings: list[str] = field(default_factory=list)
    hard_rejections: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "sha256": self.sha256,
            "perceptual_hash": self.perceptual_hash,
            "width": self.width,
            "height": self.height,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "exif": self.exif,
            "grid_score": round(self.grid_score, 4),
            "uniform_line_fraction": round(self.uniform_line_fraction, 4),
            "tile_repetition_score": round(self.tile_repetition_score, 4),
            "has_letterbox_bars": self.has_letterbox_bars,
            "warnings": self.warnings,
            "hard_rejections": self.hard_rejections,
        }


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_supported_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def mime_for_path(path: str | Path) -> str | None:
    return _MIME_BY_SUFFIX.get(Path(path).suffix.lower())


# ─────────────────────────────────────────────────────────────────────────────
# Perceptual hash (dHash)
# ────────────────────────────────────────────────────────────────────────────


def dhash(image: Any, hash_size: int = 8) -> str:
    """Difference hash, 64 bits as hex.

    Robust to rescaling and mild compression, which is what we want for spotting
    "the same photo submitted twice" — not for cryptographic identity.
    """
    small = image.convert("L").resize((hash_size + 1, hash_size))
    pixels = list(small.getdata())
    bits = 0
    index = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            if left > right:
                bits |= 1 << index
            index += 1
    return f"{bits:016x}"


def hamming_distance(a: str, b: str) -> int:
    if len(a) != len(b):
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def is_near_duplicate(a: str, b: str, *, threshold: int = 6) -> bool:
    return hamming_distance(a, b) <= threshold


# ─────────────────────────────────────────────────────────────────────────────
# Grid / collage / screenshot detection
# ─────────────────────────────────────────────────────────────────────────────


def _rows_uniform_fraction(
    gray: Any, *, tolerance: int = 6, stride: int = 1
) -> tuple[float, float]:
    """Fraction of rows and columns that are near-uniform in colour.

    A photographic row almost always varies. A panel border, a letterbox bar, or
    a table rule is a whole row at one value.
    """
    width, height = gray.size
    pixels = gray.load()
    assert pixels is not None

    uniform_rows = 0
    considered_rows = 0
    for y in range(0, height, stride):
        first = pixels[0, y]
        uniform = True
        for x in range(1, width, max(1, width // 64)):
            if abs(pixels[x, y] - first) > tolerance:
                uniform = False
                break
        considered_rows += 1
        if uniform:
            uniform_rows += 1

    uniform_cols = 0
    considered_cols = 0
    for x in range(0, width, stride):
        first = pixels[x, 0]
        uniform = True
        for y in range(1, height, max(1, height // 64)):
            if abs(pixels[x, y] - first) > tolerance:
                uniform = False
                break
        considered_cols += 1
        if uniform:
            uniform_cols += 1

    row_fraction = uniform_rows / considered_rows if considered_rows else 0.0
    col_fraction = uniform_cols / considered_cols if considered_cols else 0.0
    return row_fraction, col_fraction


def _tile_repetition(image: Any, *, tiles: int = 3) -> float:
    """How self-similar are the tiles of a `tiles x tiles` layout?

    A nine-panel storyboard has near-identical *frames* (borders, panel shape)
    even when the content differs. We downsample each tile hard, so only coarse
    structural similarity survives, then measure agreement.
    """
    gray = image.convert("L").resize((96, 96))
    tile_px = 96 // tiles
    if tile_px < 4:
        return 0.0

    signatures: list[list[int]] = []
    for ty in range(tiles):
        for tx in range(tiles):
            box = (tx * tile_px, ty * tile_px, (tx + 1) * tile_px, (ty + 1) * tile_px)
            tile = gray.crop(box).resize((8, 8))
            signatures.append(list(tile.getdata()))

    if len(signatures) < 4:
        return 0.0

    # Edge-structure signature: where does each tile transition from dark to light?
    def edge_profile(values: list[int]) -> list[int]:
        mean = sum(values) / len(values)
        return [1 if v > mean else 0 for v in values]

    profiles = [edge_profile(sig) for sig in signatures]
    pairs = 0
    agree = 0
    for i in range(len(profiles)):
        for j in range(i + 1, len(profiles)):
            pairs += 1
            matches = sum(1 for a, b in zip(profiles[i], profiles[j], strict=True) if a == b)
            if matches / len(profiles[i]) > 0.80:
                agree += 1
    return agree / pairs if pairs else 0.0


def _letterbox_bars(gray: Any, *, tolerance: int = 10) -> bool:
    """Solid bands across the top and bottom, as a video player would show."""
    width, height = gray.size
    pixels = gray.load()
    assert pixels is not None
    band = max(2, height // 20)

    def band_is_flat(y0: int, y1: int) -> bool:
        ref = pixels[0, y0]
        for y in range(y0, y1):
            for x in range(0, width, max(1, width // 32)):
                if abs(pixels[x, y] - ref) > tolerance:
                    return False
        return True

    return band_is_flat(0, band) and band_is_flat(height - band, height)


def inspect_image_local(path: str | Path) -> LocalImageReport:
    """Run every local check on one reference candidate."""
    p = Path(path)
    report = LocalImageReport(path=str(p), ok=False, sha256="")

    if not p.is_file():
        report.hard_rejections.append("file does not exist")
        return report

    size = p.stat().st_size
    report.size_bytes = size
    report.mime_type = mime_for_path(p)

    if size < MIN_FILE_BYTES:
        report.hard_rejections.append(f"file is only {size} bytes; too small to be a usable photo")
        return report

    if p.suffix.lower() not in SUPPORTED_SUFFIXES:
        if p.suffix.lower() in _HEIF_SUFFIXES:
            report.hard_rejections.append(
                f"{p.suffix} needs the optional `pillow-heif` plugin to decode. "
                "Install it (`pip install pillow-heif`), or convert the image to PNG "
                "or JPEG first."
            )
        else:
            report.hard_rejections.append(
                f"unsupported extension {p.suffix!r}; expected one of {sorted(SUPPORTED_SUFFIXES)}"
            )
        return report

    try:
        report.sha256 = file_sha256(p)
    except OSError as exc:
        report.hard_rejections.append(f"could not read file: {exc}")
        return report

    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        report.warnings.append("Pillow is not installed; only file-level checks ran")
        report.ok = not report.hard_rejections
        return report

    try:
        with Image.open(p) as img:
            img.load()
            width, height = img.size
            report.width = width
            report.height = height
            report.exif = _read_exif(img)

            if min(width, height) < MIN_SHORT_EDGE:
                report.hard_rejections.append(
                    f"image is {width}x{height}; short edge below {MIN_SHORT_EDGE}px "
                    "will not carry identity detail"
                )

            aspect = width / height if height else 0
            if aspect > MAX_ASPECT_RATIO or aspect < MIN_ASPECT_RATIO:
                report.hard_rejections.append(
                    f"extreme aspect ratio {aspect:.2f}; use a normal photo crop"
                )

            gray = img.convert("L")
            # Downscale before the O(n) scans; structure survives, cost does not.
            scan = gray.copy()
            scan.thumbnail((512, 512))

            report.perceptual_hash = dhash(scan)
            row_frac, col_frac = _rows_uniform_fraction(scan)
            report.uniform_line_fraction = max(row_frac, col_frac)
            report.tile_repetition_score = _tile_repetition(scan)
            report.has_letterbox_bars = _letterbox_bars(scan)

            # Combine the evidence into one score. Weights are heuristic and
            # tuned toward *not* firing on legitimate photos: a plain wall or a
            # clean floor produces a few uniform lines, a storyboard produces
            # many plus repeated tiles.
            grid_score = 0.0
            if report.uniform_line_fraction > 0.06:
                grid_score += min(0.6, report.uniform_line_fraction * 3.0)
            if report.tile_repetition_score > 0.30:
                grid_score += min(0.5, report.tile_repetition_score)
            if report.has_letterbox_bars:
                grid_score += 0.25
            report.grid_score = min(1.0, grid_score)

            if report.grid_score >= 0.55:
                report.warnings.append(
                    f"likely a grid, collage, or player screenshot "
                    f"(grid_score={report.grid_score:.2f}, "
                    f"uniform_lines={report.uniform_line_fraction:.3f}, "
                    f"tile_repetition={report.tile_repetition_score:.2f})"
                    + ("; solid top/bottom bands detected" if report.has_letterbox_bars else "")
                )

            # Suspicious EXIF: a screenshot tool usually leaves no camera data,
            # and some storyboard exports carry a document producer tag.
            software = str(report.exif.get("Software", "")).lower()
            for marker in ("photoshop", "premiere", "canva", "figma", "powerpoint", "keynote"):
                if marker in software:
                    report.warnings.append(
                        f"EXIF Software={report.exif.get('Software')!r} suggests an edited "
                        "or composited source, not a camera original"
                    )
                    break

    except UnidentifiedImageError:
        report.hard_rejections.append("file is not a decodable image")
        return report
    except Exception as exc:
        report.hard_rejections.append(f"image decode failed: {type(exc).__name__}: {exc}")
        return report

    report.ok = not report.hard_rejections
    return report


def _read_exif(img: Any) -> dict[str, Any]:
    """Small, JSON-safe EXIF subset. Never raises."""
    try:
        from PIL import ExifTags

        raw = img.getexif()
    except Exception:
        return {}
    if not raw:
        return {}

    interesting = {
        "Make",
        "Model",
        "Software",
        "DateTime",
        "DateTimeOriginal",
        "Orientation",
        "ImageWidth",
        "ImageLength",
        "LensModel",
    }
    out: dict[str, Any] = {}
    for tag_id, value in raw.items():
        name = ExifTags.TAGS.get(tag_id, str(tag_id))
        if name in interesting:
            out[name] = value if isinstance(value, (str, int, float)) else str(value)
    return out


def find_near_duplicates(
    reports: list[LocalImageReport], *, threshold: int = 6
) -> list[tuple[int, int]]:
    """Index pairs that are perceptually the same image."""
    pairs: list[tuple[int, int]] = []
    for i in range(len(reports)):
        for j in range(i + 1, len(reports)):
            a, b = reports[i], reports[j]
            if (
                a.perceptual_hash
                and b.perceptual_hash
                and is_near_duplicate(a.perceptual_hash, b.perceptual_hash, threshold=threshold)
            ):
                pairs.append((i, j))
    return pairs


def is_likely_portrait(image: Any) -> bool:
    size = image.size
    width, height = int(size[0]), int(size[1])
    return bool(height >= width)
