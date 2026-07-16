"""Bounding of the in-memory :class:`TaskStateManager` hot cache (issue #660).

The actor is ``lifetime="detached"`` and used to be insert-only, so every
dispatched file leaked a ``TaskInfo`` (plus a full traceback on failure) until
OOM. Postgres is now the durable record; this cache only has to keep recent
entries.
"""

from __future__ import annotations

import pytest
from services.workers import task_state as task_state_module

_ACTOR = task_state_module.TaskStateManager.__ray_metadata__.modified_class


def _manager():
    return _ACTOR()


async def _dispatch(mgr, task_id: str, user_id: int = 1) -> None:
    await mgr.set_state(task_id, "QUEUED")
    await mgr.set_details(task_id, file_id=f"f-{task_id}", partition="p", metadata={}, user_id=user_id)


async def test_terminal_tasks_are_evicted_beyond_the_cap(monkeypatch):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 3)
    mgr = _manager()

    for i in range(6):
        await _dispatch(mgr, f"t{i}")
        await mgr.set_state(f"t{i}", "COMPLETED")

    assert len(mgr.tasks) == 3
    # oldest evicted first (FIFO)
    assert set(mgr.tasks) == {"t3", "t4", "t5"}
    assert await mgr.get_state("t0") is None


async def test_eviction_drops_the_user_index_entry_too(monkeypatch):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 1)
    mgr = _manager()

    await _dispatch(mgr, "t0", user_id=7)
    await mgr.set_state("t0", "COMPLETED")
    await _dispatch(mgr, "t1", user_id=7)
    await mgr.set_state("t1", "COMPLETED")

    assert list(mgr.tasks) == ["t1"]
    assert mgr.user_index.get(7) == {"t1"}


async def test_terminal_tasks_are_evicted_once_older_than_the_ttl(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(task_state_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(task_state_module, "_TERMINAL_TTL_SECONDS", 10.0)
    mgr = _manager()

    await _dispatch(mgr, "old")
    await mgr.set_state("old", "COMPLETED")

    clock["now"] = 100.0
    await _dispatch(mgr, "new")
    await mgr.set_state("new", "FAILED")

    assert "old" not in mgr.tasks
    assert "new" in mgr.tasks


async def test_in_flight_tasks_are_never_evicted(monkeypatch):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 1)
    mgr = _manager()

    await _dispatch(mgr, "running")
    await mgr.set_state("running", "SERIALIZING")
    for i in range(5):
        await _dispatch(mgr, f"done{i}")
        await mgr.set_state(f"done{i}", "COMPLETED")

    assert await mgr.get_state("running") == "SERIALIZING"


async def test_cancelled_tasks_are_evictable(monkeypatch):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 0)
    mgr = _manager()

    await _dispatch(mgr, "t0")
    await mgr.set_state("t0", "CANCELLED")

    assert mgr.tasks == {}


async def test_set_error_truncates_long_tracebacks():
    mgr = _manager()
    await _dispatch(mgr, "t0")

    await mgr.set_error("t0", "x" * 100_000)

    stored = await mgr.get_error("t0")
    assert len(stored) <= task_state_module._MAX_ERROR_CHARS + 100
    assert "truncated" in stored


async def test_set_failed_if_not_cancelled_truncates_and_reports_terminality():
    mgr = _manager()
    await _dispatch(mgr, "t0")

    assert await mgr.set_failed_if_not_cancelled("t0", "y" * 100_000) is True
    assert await mgr.get_state("t0") == "FAILED"
    assert len(await mgr.get_error("t0")) <= task_state_module._MAX_ERROR_CHARS + 100


async def test_set_failed_if_not_cancelled_keeps_cancelled_state():
    mgr = _manager()
    await _dispatch(mgr, "t0")
    await mgr.set_state("t0", "CANCELLED")

    assert await mgr.set_failed_if_not_cancelled("t0", "boom") is False
    assert await mgr.get_state("t0") == "CANCELLED"


@pytest.mark.parametrize("state", ["COMPLETED", "FAILED", "CANCELLED"])
async def test_failing_after_eviction_does_not_resurrect_the_task(monkeypatch, state):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 0)
    mgr = _manager()

    await _dispatch(mgr, "t0")
    await mgr.set_state("t0", state)

    assert await mgr.set_failed_if_not_cancelled("t0", "late") is False
    assert mgr.tasks == {}
