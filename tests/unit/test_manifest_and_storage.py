"""§21.1: manifest idempotency and atomic writes.

§5 requires each transition to be written atomically to both the manifest and the
database. The test that matters most here is the crash-safety one: a half-written
manifest must be impossible.
"""

from __future__ import annotations

import json

import pytest

from omni_homevlog.errors import JobNotFoundError, StateTransitionError
from omni_homevlog.schemas import JobState, Manifest, utc_now_iso
from omni_homevlog.storage.local import (
    JobPaths,
    LocalStore,
    StorageError,
    atomic_write_json,
    make_job_id,
    validate_job_id,
)
from omni_homevlog.storage.manifest import ManifestStore


@pytest.fixture
def store(tmp_path, manifest: Manifest) -> ManifestStore:
    """A store holding a freshly-created job, so forward transitions are legal.

    The shared `manifest` fixture sits at PLAN_READY, which is already past the
    early states; these tests need a job that can still walk them.
    """
    fresh = manifest.model_copy(update={"state": JobState.CREATED, "state_history": []})
    root = LocalStore(tmp_path / "data").ensure()
    paths = root.job(fresh.job_id).ensure()
    manifest_store = ManifestStore(paths)
    manifest_store.create(fresh)
    return manifest_store


# ── job id safety ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../escape", "..", ".hidden", "a/b", "", "x" * 80])
def test_unsafe_job_ids_are_rejected(bad: str) -> None:
    """Job ids become directory names, so traversal must be impossible."""
    with pytest.raises(StorageError):
        validate_job_id(bad)


def test_generated_job_ids_are_sortable_and_safe() -> None:
    first = make_job_id()
    second = make_job_id()
    assert first != second
    validate_job_id(first)
    assert first.startswith("job-")


# ── atomic writes ──────────────────────────────────────────────────────────


def test_atomic_write_leaves_no_partial_file(tmp_path) -> None:
    target = tmp_path / "out.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_replaces_an_existing_file(tmp_path) -> None:
    target = tmp_path / "out.json"
    atomic_write_json(target, {"version": 1})
    atomic_write_json(target, {"version": 2})
    assert json.loads(target.read_text()) == {"version": 2}


def test_write_is_atomic_under_a_simulated_crash(tmp_path, monkeypatch) -> None:
    """If the write dies mid-flush, the previous contents must survive intact."""
    target = tmp_path / "out.json"
    atomic_write_json(target, {"good": True})

    real_replace = __import__("os").replace

    def explode(src, dst):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr("os.replace", explode)
    with pytest.raises(OSError):
        atomic_write_json(target, {"good": False})

    monkeypatch.setattr("os.replace", real_replace)
    assert json.loads(target.read_text()) == {"good": True}


# ── state transitions persist atomically ───────────────────────────────────


def test_transition_persists_to_both_stores(store: ManifestStore) -> None:
    store.transition(target=JobState.REFERENCES_VALIDATED)

    assert store.load().state is JobState.REFERENCES_VALIDATED
    row = store.db.get_job(store.paths.job_id)
    assert row is not None
    assert row["state"] == "REFERENCES_VALIDATED"


def test_transition_appends_history(store: ManifestStore) -> None:
    store.transition(target=JobState.REFERENCES_VALIDATED, note="first")
    store.transition(target=JobState.PLAN_READY, note="second")

    history = store.load().state_history
    assert [h["to"] for h in history] == ["REFERENCES_VALIDATED", "PLAN_READY"]
    assert history[0]["note"] == "first"


def test_illegal_transition_raises_and_changes_nothing(store: ManifestStore) -> None:
    before = store.load()
    with pytest.raises(StateTransitionError):
        store.transition(target=JobState.COMPLETE)  # CREATED cannot jump to COMPLETE

    after = store.load()
    assert after.state == before.state
    assert after.state_history == before.state_history


def test_repeated_save_is_idempotent(store: ManifestStore) -> None:
    manifest = store.load()
    store.save(manifest)
    store.save(manifest)
    reloaded = store.load()
    assert reloaded.job_id == manifest.job_id
    assert reloaded.state == manifest.state


def test_manifest_round_trips_through_json(store: ManifestStore) -> None:
    original = store.load()
    raw = json.loads(store.paths.manifest_path.read_text())
    assert Manifest.model_validate(raw).job_id == original.job_id


def test_missing_manifest_raises_job_not_found(tmp_path, manifest: Manifest) -> None:
    paths = LocalStore(tmp_path / "empty").job("job-20260921-000000-ffffff")
    with pytest.raises(JobNotFoundError):
        ManifestStore(paths).load()


def test_load_or_none_tolerates_a_corrupt_manifest(tmp_path, manifest: Manifest) -> None:
    root = LocalStore(tmp_path / "data").ensure()
    paths = root.job(manifest.job_id).ensure()
    paths.manifest_path.write_text("{ this is not json")
    assert ManifestStore(paths).load_or_none() is None


def test_rebuild_from_manifest_repairs_the_index(tmp_path, manifest: Manifest) -> None:
    """The manifest is authoritative; the database is derived and repairable."""
    root = LocalStore(tmp_path / "data").ensure()
    paths = root.job(manifest.job_id).ensure()
    store = ManifestStore(paths)
    store.create(manifest)

    store.db.delete_job(manifest.job_id)
    assert store.db.get_job(manifest.job_id) is None

    store.db.rebuild_from_manifest(store.load())
    assert store.db.get_job(manifest.job_id) is not None


def test_note_and_error_append_without_disturbing_state(store: ManifestStore) -> None:
    store.transition(target=JobState.REFERENCES_VALIDATED)
    store.append_note("something happened")
    store.append_error("something failed")

    manifest = store.load()
    assert manifest.state is JobState.REFERENCES_VALIDATED
    assert manifest.notes == ["something happened"]
    assert manifest.errors == ["something failed"]


def test_mutate_rejects_unknown_fields(store: ManifestStore) -> None:
    with pytest.raises(KeyError):
        store.mutate(not_a_real_field=1)


def test_export_summary_is_serialisable(store: ManifestStore) -> None:
    summary = store.export_summary()
    json.dumps(summary)  # must not raise
    assert summary["job_id"] == store.paths.job_id


# ─ paths ────────────────────────────────────────────────────────────────────


def test_job_paths_layout_matches_the_plan(tmp_path) -> None:
    paths = JobPaths(tmp_path, "job-20260921-120000-aaa111")
    assert paths.manifest_path.parent.name == "final"
    assert paths.project_spec_path.parent.name == "plans"
    assert paths.segment_dir(2).name == "segment_02"
    assert paths.attempt_path(1, 0).name == "attempt_00_raw.mp4"
    assert paths.review_path(1, 0).name == "segment_01_attempt_00.json"


def test_relpath_round_trips(tmp_path) -> None:
    paths = JobPaths(tmp_path, "job-20260921-120000-aaa111").ensure()
    target = paths.attempt_path(0, 0)
    # `ensure()` cannot know how many segments a job will have, so segment
    # directories are created by the writer. That is what the provider does too.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    rel = paths.relpath(target)
    assert paths.resolve_relpath(rel) == target
    assert rel == "renders/segment_00/attempt_00_raw.mp4"


def test_delete_removes_the_job_directory(tmp_path) -> None:
    paths = JobPaths(tmp_path, "job-20260921-120000-aaa111").ensure()
    (paths.root / "marker.txt").write_text("x")
    paths.delete()
    assert not paths.root.exists()


def test_utc_now_iso_is_sortable_and_zulu() -> None:
    stamp = utc_now_iso()
    assert stamp.endswith("Z")
    assert stamp > "2020-01-01T00:00:00Z"
