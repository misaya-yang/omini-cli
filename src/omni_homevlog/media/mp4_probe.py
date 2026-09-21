"""Pure-Python MP4/ISO-BMFF inspection.

Exists because ffprobe is not always installed, and because the *first* thing
this agent must do with a provider response is prove it is really a video of the
duration it claims. shelling out to ffprobe is preferable when available
(`media/ffprobe.py`), but "ffmpeg is missing" must not mean "we cannot tell
whether the render is 3 seconds or 0 bytes".

Also detects C2PA content credentials: C2PA manifests live in a top-level
`jumb` box (or a `uuid` box for the older layout), so a box walk is enough to
answer "did the provider attach provenance?" without a C2PA library (§22:
detect, never strip).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Boxes whose payload is a container of child boxes.
_CONTAINER_BOXES = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"stbl",
    b"dinf",
    b"edts",
    b"udta",
    b"mvex",
    b"moof",
    b"traf",
}

#: C2PA lives in one of these.
_C2PA_BOXES = {b"jumb", b"jumbf"}

#: uuid box identifier for C2PA in the older layout.
_C2PA_UUID = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")

_CONTAINER_BRANDS = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"dash", b"qt  ", b"M4V "}


@dataclass(slots=True)
class Mp4Box:
    type: bytes
    offset: int
    size: int
    header_size: int

    @property
    def payload_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def payload_size(self) -> int:
        return self.size - self.header_size


@dataclass(slots=True)
class Mp4Info:
    """What we could learn. `None` means "not present / not determined"."""

    container: str | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    has_audio: bool | None = None
    size_bytes: int | None = None
    c2pa_present: bool = False
    c2pa_detail: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "duration_s": self.duration_s,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "has_audio": self.has_audio,
            "size_bytes": self.size_bytes,
            "c2pa_present": self.c2pa_present,
            "c2pa_detail": self.c2pa_detail,
            "brands": self.brands,
            "error": self.error,
        }


def _read_boxes(data: bytes, start: int, end: int, depth: int = 0) -> list[Mp4Box]:
    """Walk sibling boxes in `data[start:end]`.

    Depth-bounded. Every read is bounds-checked against `end` so a corrupt file
    yields a short list rather than an exception.
    """
    if depth > 8:
        return []
    boxes: list[Mp4Box] = []
    offset = start
    while offset + 8 <= end:
        size = struct.unpack_from(">I", data, offset)[0]
        box_type = data[offset + 4 : offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > end:
                break
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = end - offset  # box extends to end of file
        if size < header or offset + size > end:
            break
        boxes.append(Mp4Box(type=box_type, offset=offset, size=size, header_size=header))
        offset += size
    return boxes


def _find(boxes: list[Mp4Box], box_type: bytes) -> Mp4Box | None:
    return next((b for b in boxes if b.type == box_type), None)


def _walk_all(data: bytes, boxes: list[Mp4Box], depth: int = 0) -> list[Mp4Box]:
    """Every box in the tree, flattened."""
    out: list[Mp4Box] = []
    for box in boxes:
        out.append(box)
        if box.type in _CONTAINER_BOXES:
            out.extend(
                _walk_all(
                    data,
                    _read_boxes(data, box.payload_offset, box.offset + box.size, depth + 1),
                    depth + 1,
                )
            )
        elif box.type == b"meta":
            # `meta` is a FullBox: 4 bytes of version/flags precede its children.
            out.extend(
                _walk_all(
                    data,
                    _read_boxes(data, box.payload_offset + 4, box.offset + box.size, depth + 1),
                    depth + 1,
                )
            )
    return out


def _parse_mvhd(data: bytes, box: Mp4Box) -> float | None:
    """Movie header → duration in seconds."""
    offset = box.payload_offset
    if offset + 4 > len(data):
        return None
    version = data[offset]
    try:
        if version == 1:
            timescale = struct.unpack_from(">I", data, offset + 20)[0]
            duration = struct.unpack_from(">Q", data, offset + 24)[0]
        else:
            timescale = struct.unpack_from(">I", data, offset + 12)[0]
            duration = struct.unpack_from(">I", data, offset + 16)[0]
    except struct.error:
        return None
    if not timescale:
        return None
    # `struct.unpack_from` is untyped upstream, so the division result would be Any.
    return float(duration) / float(timescale)


def _parse_tkhd(data: bytes, box: Mp4Box) -> tuple[int, int] | None:
    """Track header → (width, height) as 16.16 fixed point."""
    offset = box.payload_offset
    version = data[offset]
    # width/height are the last 8 bytes of the box in both versions.
    end = box.offset + box.size
    if end - 8 < offset:
        return None
    try:
        width = struct.unpack_from(">I", data, end - 8)[0] / 65536.0
        height = struct.unpack_from(">I", data, end - 4)[0] / 65536.0
    except struct.error:
        return None
    _ = version
    return round(width), round(height)


def _parse_hdlr(data: bytes, box: Mp4Box) -> str | None:
    """Handler box → 'vide' / 'soun'."""
    offset = box.payload_offset + 8  # version/flags (4) + pre_defined (4)
    if offset + 4 > box.offset + box.size:
        return None
    return data[offset : offset + 4].decode("ascii", errors="replace")


def _parse_stsd(data: bytes, box: Mp4Box) -> list[str]:
    """Sample description → the codec fourccs of this track."""
    offset = box.payload_offset
    if offset + 8 > len(data):
        return []
    try:
        count = struct.unpack_from(">I", data, offset + 4)[0]
    except struct.error:
        return []
    entries_start = offset + 8
    entries = _read_boxes(data, entries_start, min(box.offset + box.size, len(data)), depth=1)
    codecs: list[str] = []
    for entry in entries[:count]:
        codecs.append(entry.type.decode("ascii", errors="replace").strip())
    return codecs


def probe_mp4_bytes(data: bytes, *, filename: str = "") -> Mp4Info:
    """Inspect an in-memory MP4."""
    info = Mp4Info(size_bytes=len(data))
    if len(data) < 16:
        info.error = "file too small to be a video container"
        return info

    top = _read_boxes(data, 0, len(data))
    if not top:
        info.error = "no ISO-BMFF boxes found at the top level"
        return info

    ftyp = _find(top, b"ftyp")
    if ftyp is None:
        info.error = "no ftyp box; not an MP4"
        return info

    info.container = "mp4"
    if ftyp.payload_size >= 4:
        major = data[ftyp.payload_offset : ftyp.payload_offset + 4]
        info.brands.append(major.decode("ascii", errors="replace").strip())
        compat_start = ftyp.payload_offset + 8
        compat_end = ftyp.offset + ftyp.size
        for pos in range(compat_start, compat_end - 3, 4):
            brand = data[pos : pos + 4].decode("ascii", errors="replace").strip()
            if brand and brand not in info.brands:
                info.brands.append(brand)

    # ── content credentials ───────────────────────────────────────────────
    for box in top:
        if box.type in _C2PA_BOXES:
            info.c2pa_present = True
            info.c2pa_detail.append(f"top-level '{box.type.decode()}' box present")
        elif box.type == b"uuid" and box.payload_size >= 16:
            uuid_bytes = data[box.payload_offset : box.payload_offset + 16]
            label = "c2pa" if uuid_bytes == _C2PA_UUID else uuid_bytes.hex()
            info.c2pa_detail.append(f"uuid box: {label}")
            if uuid_bytes == _C2PA_UUID:
                info.c2pa_present = True

    # ── structure ─────────────────────────────────────────────────────────
    all_boxes = _walk_all(data, top)

    moov = _find(top, b"moov")
    if moov is None:
        info.error = "no moov box; file is not finalized (or is a fragment)"
        return info

    moov_children = _read_boxes(data, moov.payload_offset, moov.offset + moov.size, depth=1)
    mvhd = _find(moov_children, b"mvhd")
    if mvhd is not None:
        info.duration_s = _parse_mvhd(data, mvhd)
    else:
        # Fragmented MP4 keeps duration in mvex/mehd instead.
        mvex = _find(moov_children, b"mvex")
        if mvex is not None:
            mehd = _find(
                _read_boxes(data, mvex.payload_offset, mvex.offset + mvex.size, depth=2), b"mehd"
            )
            if mehd is not None and mehd.payload_size >= 8:
                version = data[mehd.payload_offset]
                try:
                    if version == 1:
                        timescale = struct.unpack_from(">I", data, moov.offset + 8)[0] or 1000
                        duration = struct.unpack_from(">Q", data, mehd.payload_offset + 4)[0]
                    else:
                        timescale = 1000
                        duration = struct.unpack_from(">I", data, mehd.payload_offset + 4)[0]
                    info.duration_s = duration / timescale
                except (struct.error, ZeroDivisionError):
                    pass

    has_video = False
    has_audio = False
    for trak in [b for b in moov_children if b.type == b"trak"]:
        trak_children = _read_boxes(data, trak.payload_offset, trak.offset + trak.size, depth=2)
        mdia = _find(trak_children, b"mdia")
        if mdia is None:
            continue
        mdia_children = _read_boxes(data, mdia.payload_offset, mdia.offset + mdia.size, depth=3)
        hdlr = _find(mdia_children, b"hdlr")
        handler = _parse_hdlr(data, hdlr) if hdlr else None

        minf = _find(mdia_children, b"minf")
        codecs: list[str] = []
        if minf is not None:
            minf_children = _read_boxes(data, minf.payload_offset, minf.offset + minf.size, depth=4)
            stbl = _find(minf_children, b"stbl")
            if stbl is not None:
                stbl_children = _read_boxes(
                    data, stbl.payload_offset, stbl.offset + stbl.size, depth=5
                )
                stsd = _find(stbl_children, b"stsd")
                if stsd is not None:
                    codecs = _parse_stsd(data, stsd)

        if handler == "vide":
            has_video = True
            info.video_codec = codecs[0] if codecs else info.video_codec
            tkhd = _find(trak_children, b"tkhd")
            if tkhd is not None:
                dims = _parse_tkhd(data, tkhd)
                if dims and dims[0] and dims[1]:
                    info.width, info.height = dims
        elif handler == "soun":
            has_audio = True
            info.audio_codec = codecs[0] if codecs else info.audio_codec

    info.has_audio = has_audio
    if not has_video:
        info.error = "container has no video track"

    _ = all_boxes  # traversal is only needed for the C2PA scan above
    return info


def probe_mp4(path: str | Path) -> Mp4Info:
    """Inspect an MP4 on disk.

    Reads the whole file: renders here are tens of megabytes at most, and a
    partial read would make the box walk unreliable for `moov`-at-end files.
    """
    p = Path(path)
    if not p.is_file():
        return Mp4Info(error=f"not a file: {p}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        return Mp4Info(error=f"read failed: {exc}")
    info = probe_mp4_bytes(data, filename=p.name)
    info.size_bytes = len(data)
    return info


def looks_like_mp4(path: str | Path, *, min_bytes: int = 1024) -> tuple[bool, str]:
    """Cheap validity gate used right after a download.

    The handoff's acceptance criterion #4 is explicit that an HTTP 200 is not
    proof of an output: the bytes have to be verified.
    """
    p = Path(path)
    if not p.is_file():
        return False, "file does not exist"
    size = p.stat().st_size
    if size < min_bytes:
        return False, f"file is only {size} bytes"
    with p.open("rb") as handle:
        head = handle.read(12)
    if len(head) < 12:
        return False, "file too short for a container header"
    if head[4:8] not in _CONTAINER_BRANDS and head[4:8] != b"ftyp":
        return False, f"unexpected box type at offset 4: {head[4:8]!r}"
    return True, f"ok ({size} bytes)"
