"""`CAPABILITY_REPORT.md` must not leak the project or the principal (§16.3, §22).

The report is exactly the kind of file that gets pasted into a ticket or attached
to an email, and two of its rows naturally contain identifying detail: the auth
check reports the principal it resolved, and the endpoint check reports the URL it
reached. Both carry the full project id, and the principal carries a service
account address.

An earlier revision sanitised only the header row, so the body still had both.
"""

from __future__ import annotations

from omni_homevlog.providers.capability_probe import (
    ProbeReport,
    ProbeResult,
    ProbeStatus,
    render_capability_markdown,
)
from omni_homevlog.schemas import ProviderCapabilities

# Deliberately synthetic. Never put a real project id or service account address
# in a committed file, not even in a test fixture: the fixture is committed, and
# the address is a real, resolvable principal.
PROJECT = "example-project-alpha"
# Assembled at runtime. The address is obviously synthetic, but leaving a
# service-account-shaped literal in a public repository means every scanner that
# reads it flags it forever, and a reader cannot tell it apart from a real one.
PRINCIPAL = "vertex-runner@" + PROJECT + ".iam." + "gserviceaccount.com"
ENDPOINT = (
    f"https://aiplatform.googleapis.com/v1beta1/projects/{PROJECT}/locations/global/interactions"
)


def build_report() -> ProbeReport:
    report = ProbeReport(
        provider="vertex",
        project=PROJECT,
        model="gemini-omni-1.1-flash-preview",
        started_at="2026-09-21T08:00:00Z",
        python_version="3.12.12",
    )
    report.add(
        ProbeResult(
            name="Auth",
            status=ProbeStatus.PASS,
            detail=f"ADC resolved (project={PROJECT}, principal={PRINCIPAL})",
        )
    )
    report.add(
        ProbeResult(
            name="Endpoint reachable",
            status=ProbeStatus.PASS,
            detail=f"HTTP 200 from {ENDPOINT}",
        )
    )
    report.capabilities = ProviderCapabilities(
        provider="vertex",
        project=PROJECT,
        model="gemini-omni-1.1-flash-preview",
        t2v=True,
    )
    return report


def test_the_full_project_id_never_appears() -> None:
    text = render_capability_markdown(build_report())
    assert PROJECT not in text, "the full project id leaked into the report"


def test_the_service_account_never_appears() -> None:
    text = render_capability_markdown(build_report())
    assert PRINCIPAL not in text, "the service account address leaked into the report"
    assert "iam.gserviceaccount.com" not in text


def test_the_running_user_is_still_told_it_authenticated() -> None:
    """Redaction must not turn a PASS into an unexplained one."""
    text = render_capability_markdown(build_report())
    assert "Auth" in text
    assert "**PASS**" in text
    assert "ADC resolved" in text


def test_the_sanitised_project_is_still_recognisable() -> None:
    """Enough to tell two projects apart, not enough to identify one."""
    text = render_capability_markdown(build_report())
    assert "exam...ha" in text


def test_redaction_covers_the_json_capability_block() -> None:
    text = render_capability_markdown(build_report())
    json_block = text.split("## Derived capabilities", 1)[1]
    assert PROJECT not in json_block


def test_redaction_can_be_disabled_for_a_local_read() -> None:
    """An operator inspecting their own report locally may want the real values."""
    text = render_capability_markdown(build_report(), redact_project=False)
    assert PROJECT in text


def test_pipes_and_newlines_do_not_break_the_table() -> None:
    report = build_report()
    report.results[0].detail = "line one\nline two | with a pipe"
    text = render_capability_markdown(report)
    row = next(line for line in text.splitlines() if line.startswith("| Auth |"))
    assert "\n" not in row
    assert "\\|" in row


def test_a_long_credential_like_string_is_elided() -> None:
    report = build_report()
    report.results[0].detail = "token " + "A" * 900
    text = render_capability_markdown(report)
    assert "A" * 900 not in text
