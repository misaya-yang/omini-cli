"""Local, nonblocking job locks shared by CLI commands and the orchestrator."""

from __future__ import annotations

import fcntl
import functools
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from omni_homevlog.errors import OmniVlogError

_local = threading.local()


@contextmanager
def job_lock(path: Path) -> Iterator[None]:
    key = str(path.resolve())
    held: set[str] = getattr(_local, "held", set())
    if key in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OmniVlogError(
                "Another process is operating on this job; no request was sent."
            ) from exc
        _local.held = held | {key}
        try:
            yield
        finally:
            _local.held = held
            fcntl.flock(handle, fcntl.LOCK_UN)


def locked_command[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from omni_homevlog.config import get_settings
        from omni_homevlog.storage.local import validate_job_id

        job_id = str(kwargs.get("job_id") or args[0])
        validate_job_id(job_id)
        with job_lock(get_settings().data_dir() / "locks" / f"{job_id}.lock"):
            return fn(*args, **kwargs)

    # Typer must resolve annotations in the original module, not this one.
    import inspect
    import typing

    hints = typing.get_type_hints(fn, include_extras=True)
    signature = inspect.signature(fn)
    resolved = signature.replace(
        parameters=[
            p.replace(annotation=hints.get(n, p.annotation))
            for n, p in signature.parameters.items()
        ]
    )
    cast_wrapper: Any = wrapped
    cast_wrapper.__signature__ = resolved
    return wrapped
