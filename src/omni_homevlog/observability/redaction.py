"""Redaction.

§22: logs must never contain API keys, service-account JSON, access tokens, or
base64 media payloads. Everything that leaves the process — log lines, debug
fixtures, error messages, CLI output — passes through here first.

This module is deliberately dependency-free and total: it never raises.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"
TRUNCATED = "<{n}_bytes_elided>"

#: Below this length a base64 blob is probably an id, not media.
_BASE64_MIN = 512

_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|authorization|"
    r"private[_-]?key|client[_-]?secret|password|bearer|credentials?|"
    r"service[_-]?account|sa[_-]?key)",
    re.IGNORECASE,
)

#: Google API keys, OAuth tokens, service-account private keys.
_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "<google_api_key>"),
    (re.compile(r"\bya29\.[0-9A-Za-z_\-\.]{20,}\b"), "<oauth_token>"),
    (re.compile(r"\bya29\.[0-9A-Za-z_\-\.]{20,}"), "<oauth_token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "<jwt>"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        "<private_key>",
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]{16,}", re.IGNORECASE), "Bearer <redacted>"),
    (re.compile(r"//[a-z0-9\-]+\.iam\.gserviceaccount\.com"), "//<service_account>"),
    (
        re.compile(r"\b[a-z0-9\-]{6,}@[a-z0-9\-]{4,}\.iam\.gserviceaccount\.com\b"),
        "<service_account_email>",
    ),
]

#: Long base64 runs (inline video/image payloads).
_BASE64_RUN = re.compile(rf"[A-Za-z0-9+/]{{{_BASE64_MIN},}}={{0,2}}")

#: A private-key header with no matching footer.
#:
#: The complete-block pattern above cannot catch this, and a truncated key is the
#: *more* dangerous case: it usually means something split the key across two log
#: lines or two truncated exception messages, so each fragment looks harmless on
#: its own. Without this, `an earlier chunk of a key` reaches the log verbatim.
_ORPHAN_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

#: How far past an orphaned header to keep scrubbing, when no footer follows.
#: Bounded so a stray header cannot swallow an entire log file.
_ORPHAN_SCRUB_LIMIT = 4096


def _scrub_orphan_private_keys(text: str) -> str:
    """Redact key material that starts with a header but never terminates."""
    out = text
    while True:
        match = _ORPHAN_PRIVATE_KEY.search(out)
        if match is None:
            return out
        end = out.find("-----END", match.end())
        if end == -1:
            # No footer anywhere: scrub from the header to the end, bounded.
            end = min(len(out), match.end() + _ORPHAN_SCRUB_LIMIT)
        out = out[: match.start()] + "<private_key>" + out[end:]


def looks_like_private_key_material(text: str) -> bool:
    """True if `text` contains a private-key header, complete or not."""
    return bool(_ORPHAN_PRIVATE_KEY.search(text))


def redact_string(value: str, *, max_len: int = 4000) -> str:
    """Scrub secrets and elide oversized base64 payloads from a string."""
    out = value
    for pattern, replacement in _VALUE_PATTERNS:
        out = pattern.sub(replacement, out)
    out = _scrub_orphan_private_keys(out)

    def _elide(match: re.Match[str]) -> str:
        blob = match.group(0)
        return TRUNCATED.format(n=len(blob))

    out = _BASE64_RUN.sub(_elide, out)

    if len(out) > max_len:
        out = out[:max_len] + f"... <{len(out) - max_len}_chars_elided>"
    return out


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-ish structure.

    Keys that name a secret are replaced wholesale. Long strings are scrubbed in
    place. Depth is bounded so a pathological structure cannot blow the stack.
    """
    if _depth > 12:
        return "<max_depth>"

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _SECRET_KEY_PATTERN.search(key_str) and not key_str.endswith("_present"):
                result[key_str] = REDACTED
            else:
                result[key_str] = redact(item, _depth=_depth + 1)
        return result

    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]

    if isinstance(value, str):
        return redact_string(value)

    if isinstance(value, bytes):
        return TRUNCATED.format(n=len(value))

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    return redact_string(str(value))


def looks_like_secret(text: str) -> bool:
    """True if the text contains something that must never be committed.

    Used by the preflight/acceptance check that greps the working tree.

    Two cases are checked beyond the value patterns, because a bare value scan
    misses them:

      * **a truncated private key** — the header with no footer, which is what a
        split log line or a clipped exception message leaves behind
      * **a credential-shaped JSON key** — `"private_key_id": ...` has no value
        that matches anything, but the field name says what it is
    """
    for pattern, _ in _VALUE_PATTERNS:
        if pattern.search(text):
            return True

    if looks_like_private_key_material(text):
        return True

    # The structural marker of a service-account key file. Its value is the boring
    # string "service_account", so no value pattern would ever catch it, but the
    # file it appears in is a credential.
    if re.search(r'"type"\s*:\s*"service_account"', text):
        return True

    # Field *names* that identify a credential regardless of the value beside them.
    # `_SECRET_KEY_PATTERN` covers private_key_id, client_secret, refresh_token and
    # the rest, none of which the value patterns above can see. Only the key name is
    # tested, so an unrelated JSON document does not trip this.
    return any(
        _SECRET_KEY_PATTERN.search(match.group(1))
        for match in re.finditer(r'"([A-Za-z_0-9]+)"\s*:', text)
        if not match.group(1).endswith("_present")
    )
