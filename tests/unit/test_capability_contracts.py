from __future__ import annotations

import subprocess
import sys

import pytest

from omni_homevlog.errors import OmniVlogError
from omni_homevlog.providers.capability_probe import (
    ProbeReport,
    ProbeResult,
    ProbeStatus,
    _capabilities_from,
)
from omni_homevlog.schemas import ProviderCapabilities
from omni_homevlog.storage.database import Database
from omni_homevlog.storage.locking import job_lock


def test_http_success_without_growth_is_not_native_chain():
    report = ProbeReport(
        provider="vertex", project="p", model="m", started_at="2026-09-22T00:00:00Z"
    )
    report.add(ProbeResult(name="previous_interaction_id", status=ProbeStatus.PASS))
    report.add(ProbeResult(name="continuation grows the film", status=ProbeStatus.FAIL))
    caps = _capabilities_from(report)
    assert not caps.stateful_previous_interaction_id
    assert caps.measured_chain_s is None


def test_free_probe_preserves_measurement_and_real_failure_replaces_it(tmp_path):
    db = Database(tmp_path / "probe.db")
    caps = ProviderCapabilities(
        provider="vertex",
        project="p",
        model="m",
        location="global",
        t2v=True,
        stateful_previous_interaction_id=True,
        measured_chain_s=9.024,
        max_total_chain_s=9,
        evidence={
            "previous_interaction_id": {"status": "PASS", "at": "old"},
            "T2V 3s 360p": {"status": "PASS", "at": "old"},
        },
    )
    db.save_probe(caps)
    free = ProviderCapabilities(
        provider="vertex",
        project="p",
        model="m",
        location="global",
        evidence={"previous_interaction_id": {"status": "SKIPPED", "at": "new"}},
    )
    db.save_probe(free)
    restored = db.latest_probe("vertex", "p", "m")
    assert restored.stateful_previous_interaction_id
    assert restored.measured_chain_s == 9.024
    assert restored.evidence["previous_interaction_id"]["at"] == "old"
    assert db.latest_probe("vertex", "other", "m") is None
    failed = ProviderCapabilities(
        provider="vertex",
        project="p",
        model="m",
        location="global",
        evidence={
            "previous_interaction_id": {"status": "FAIL", "at": "later"},
            "continuation grows the film": {"status": "FAIL"},
        },
    )
    db.save_probe(failed)
    assert not db.latest_probe("vertex", "p", "m").stateful_previous_interaction_id
    assert db.latest_probe("vertex", "p", "m").measured_chain_s is None


def test_job_lock_excludes_another_process(tmp_path):
    path = tmp_path / "job.lock"
    with job_lock(path), job_lock(path):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(path),
            ],
            capture_output=True,
        )
        assert result.returncode != 0
    with job_lock(path):
        pass


def test_job_lock_excludes_another_thread(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "job.lock"

    def contender():
        with job_lock(path):
            return True

    with job_lock(path), ThreadPoolExecutor() as pool, pytest.raises(OmniVlogError):
        pool.submit(contender).result()
