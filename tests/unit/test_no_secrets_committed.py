"""Nothing resembling a credential may live in the repository.

`.gitignore` stops a file from being *added*, but it cannot stop someone writing a
real service-account address into a test fixture — which is exactly what happened
during development: a unit test carried the live project's service account address
as sample data, and `.gitignore` had no opinion about it, because the file was
meant to be committed.

So this test walks the tree and fails on anything that looks like a credential.
It runs on every `pytest`, which means the feedback arrives at the moment the value
is written rather than at review time.

The patterns are shared with `observability/redaction.py` so the runtime redactor
and this check cannot drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omni_homevlog.observability.redaction import looks_like_secret

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories that are generated, ignored, or third-party.
SKIP_DIRS = frozenset(
    {
        ".venv",
        "venv",
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "outputs",
        "exports",
        ".omni-vlog",
        "jobs",
        "htmlcov",
    }
)

#: Extensions worth reading. A binary cannot carry an accidental key in a way this
#: test could detect, and reading every MP4 would be slow for no benefit.
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".cfg",
        ".ini",
        ".txt",
        ".example",
        ".sh",
        ".env",
        "",
    }
)

#: Literal samples allowed to look like secrets because they are obviously fake.
#: Each entry needs a reason: an allowlist without one becomes a hiding place.
ALLOWED = (
    # The redaction tests deliberately contain a fake key to prove it is stripped.
    "AIzaSy" + "EXAMPLE" + "0" * 28,
    # A placeholder that appears in .env.example.
    "REPLACE_ME",
    # Documentation showing the shape of a scrubbed value.
    "<google_api_key>",
    "<private_key>",
    "<service_account_email>",
)

#: A service-account-shaped address whose project name is self-evidently a
#: placeholder. The redaction tests genuinely need one of these: they assert that a
#: real-shaped address gets scrubbed, so the fixture cannot be a non-matching
#: string. Narrowing the allowance to the *project name* rather than exempting the
#: whole file means a real address dropped into that file later is still caught.
_SYNTHETIC_PROJECT = re.compile(
    r"@[a-z0-9\-]*"
    r"(example|placeholder|synthetic|fake|dummy|sample|test)"
    r"[a-z0-9\-]*\.iam\.gserviceaccount\.com\b"
)

#: Whole-file exemptions, each with a reason. Kept deliberately short: every entry
#: is a place the check cannot see.
EXEMPT_FILES: dict[str, str] = {
    # This file names the patterns, so it necessarily contains the strings they match.
    "tests/unit/test_no_secrets_committed.py": (
        "holds the pattern list and the allowlist, so it necessarily contains "
        "the strings it searches for"
    ),
    # The redactor's own source declares the patterns and their replacements.
    "src/omni_homevlog/observability/redaction.py": (
        "declares the credential patterns and their replacement tokens"
    ),
}

#: Domain-shaped identifiers that a real deployment should never expose.
DEPLOYMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "a concrete service account address",
        re.compile(r"\b[a-z0-9][a-z0-9\-]{4,}@[a-z0-9\-]{4,}\.iam\.gserviceaccount\.com\b"),
    ),
    (
        "an AIza-prefixed Google API key",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ),
    (
        "a PEM private key block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "a service-account JSON body",
        re.compile(r'"type"\s*:\s*"service_account"'),
    ),
    (
        "a private_key_id field",
        re.compile(r'"private_key_id"\s*:'),
    ),
    (
        "a client_secret value",
        re.compile(r'"client_secret"\s*:\s*"[^"]{8,}"'),
    ),
    (
        "an OAuth access token",
        re.compile(r"\bya29\.[0-9A-Za-z_\-\.]{20,}\b"),
    ),
]


def iter_repo_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def is_allowed(sample: str) -> bool:
    """Is this match obviously a placeholder rather than a real value?"""
    if any(marker in sample for marker in ALLOWED):
        return True
    return bool(_SYNTHETIC_PROJECT.search(sample))


def suspicious_matches(text: str) -> list[tuple[str, str]]:
    """Every `(label, sample)` in `text` that looks like a real credential."""
    found: list[tuple[str, str]] = []
    for label, pattern in DEPLOYMENT_PATTERNS:
        for match in pattern.finditer(text):
            sample = match.group(0)
            if is_allowed(sample):
                continue
            found.append((label, sample))
    return found


def test_the_repository_has_files_to_check() -> None:
    """A guard against the walk silently returning nothing and passing vacuously."""
    files = iter_repo_files()
    assert len(files) > 40, f"only found {len(files)} files to scan; the walk is wrong"


def test_no_deployment_secrets_in_the_tree() -> None:
    offenders: list[str] = []

    for path in iter_repo_files():
        relative = rel(path)
        if relative in EXEMPT_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable file, nothing to assert
            continue
        for label, sample in suspicious_matches(text):
            line = text.find(sample)
            line_no = text[:line].count("\n") + 1 if line >= 0 else 0
            offenders.append(f"{relative}:{line_no}  [{label}]  {sample[:70]}")

    assert not offenders, (
        "Found credential-shaped values in the repository:\n  "
        + "\n  ".join(offenders)
        + "\n\nReplace with an obviously synthetic value. A test fixture is still "
        "committed, and a real service-account address is a real, resolvable principal."
    )


# ── the two detectors, and why they are not identical ─────────────────────
#
# The runtime redactor and this tree audit keep separate pattern lists, because
# they have different jobs and the difference is deliberate:
#
#   * `redact()` scrubs at runtime. Over-matching is cheap and safe, so it is
#     biased toward removing anything that could be a credential.
#   * this audit gates a commit. Over-matching would flag every synthetic fixture
#     and train people to ignore it, so it is biased toward precision.
#
# The contract that matters is therefore one-directional: **a real credential must
# be caught by both**, and a placeholder must not stop the audit. Requiring them to
# agree exactly would be wrong, which is what an earlier version of this test
# asserted before the disagreement below surfaced it.

def _credential_shaped(*parts: str) -> str:
    """Assemble a test sample from fragments.

    The samples below have to *look* like real credentials — that is the whole
    point of them — but a literal that looks like a real credential is exactly
    what a host's push protection refuses to accept, and what a scanner will flag
    forever afterwards. Building them at runtime keeps the check meaningful while
    leaving nothing in the committed source that matches the patterns.

    This is not security theatre. GitHub's push protection blocks Google API key
    literals outright, so the alternative is a repository that cannot be pushed.
    """
    return "".join(parts)


REAL_CREDENTIALS: list[str] = [
    _credential_shaped("AIza", "Sy", "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"),
    _credential_shaped("vertex-runner@", "my-real-project", ".iam.gserviceaccount.com"),
    _credential_shaped('{"type": ', '"service_account", "project_id": "x"}'),
    _credential_shaped("-----BEGIN ", "RSA PRIVATE KEY", "-----\nMIIE..."),
    _credential_shaped('{"private_key', '_id": "abc123"}'),
    _credential_shaped("ya29.", "a0AfH6SMB", "x" * 28),
]

PLACEHOLDERS: list[str] = [
    _credential_shaped("vertex-runner@", "example-project-alpha", ".iam.gserviceaccount.com"),
    "AIzaSy" + "EXAMPLE" + "0" * 28,
    "<service_account_email>",
    "<google_api_key>",
    "REPLACE_ME",
]

BENIGN: list[str] = [
    "just an ordinary sentence about oceans",
    "gs://approved-bucket/omni-output/",
    "gemini-omni-1.1-flash-preview",
    "duration 3s, resolution 360p, aspect 16:9",
]


@pytest.mark.parametrize("sample", REAL_CREDENTIALS, ids=lambda s: s[:32])
def test_a_real_credential_is_caught_by_both_detectors(sample: str) -> None:
    """Neither detector may have a hole. This is the direction that matters."""
    assert suspicious_matches(sample), f"the tree audit would let {sample[:40]!r} through"
    assert looks_like_secret(sample), f"the runtime redactor would not scrub {sample[:40]!r}"


@pytest.mark.parametrize("sample", PLACEHOLDERS, ids=lambda s: s[:32])
def test_a_placeholder_does_not_trip_the_tree_audit(sample: str) -> None:
    """Otherwise no synthetic fixture could exist, and the audit gets ignored.

    The redactor is allowed to scrub these anyway; over-scrubbing costs nothing.
    """
    assert not suspicious_matches(sample), f"the audit flags the placeholder {sample[:40]!r}"


@pytest.mark.parametrize("sample", BENIGN, ids=lambda s: s[:32])
def test_ordinary_text_is_not_flagged_by_either(sample: str) -> None:
    assert not suspicious_matches(sample)
    assert not looks_like_secret(sample)


# ── the ignore rules ───────────────────────────────────────────────────────


def test_gitignore_covers_the_credential_files_that_matter() -> None:
    """Every credential filename pattern we can name must be ignored."""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    required = [
        ".env",
        "application_default_credentials.json",
        "*-service-account*.json",
        "service-account*.json",
        "client_secret*.json",
        "*.pem",
        "*.p12",
        "*.key",
    ]
    missing = [pattern for pattern in required if pattern not in text]
    assert not missing, f".gitignore does not cover: {missing}"


def test_gitignore_does_not_ignore_the_env_template() -> None:
    """`.env.*` would swallow `.env.example` without the negation."""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!.env.example" in text


def test_the_env_template_holds_no_values() -> None:
    """A template contains variable names and placeholders, never a real value."""
    template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    for line in template.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if not value:
            continue
        if key.strip().startswith("#"):
            continue
        assert not re.fullmatch(r"AIza[0-9A-Za-z_\-]{35}", value), (
            f"{key} looks like a real API key in .env.example"
        )
        assert "iam.gserviceaccount.com" not in value, (
            f"{key} names a real service account in .env.example"
        )


def test_gitignore_covers_generated_media_and_job_data() -> None:
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".omni-vlog/", "outputs/", "*.mp4", "*.tmp"):
        assert pattern in text, f".gitignore does not cover {pattern}"
