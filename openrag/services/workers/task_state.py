from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import ray
from core.utils.text import MAX_ERROR_TEXT_CHARS, truncate_error_text

try:
    from core.config import load_config as _load_config

    _cfg = _load_config()
    _POOL_SIZE: int = _cfg.ray.indexer.pool_size
    _MAX_TASKS_PER_WORKER: int = _cfg.ray.indexer.max_tasks_per_worker
except (ImportError, AttributeError) as _cfg_err:
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "Could not load ray config for TaskStateManager pool info: %s — using defaults", _cfg_err
    )
    _POOL_SIZE = 1
    _MAX_TASKS_PER_WORKER = 1


# This actor is created with ``lifetime="detached"`` (see ``bootstrap.py``), so it
# outlives the API process and used to be insert-only: every file ever dispatched
# left a permanent ``TaskInfo`` (plus a full traceback on failure) until the actor
# OOM-ed (issue #660). Postgres now holds the durable record, which frees this
# actor to be what its callers actually need — a hot cache of recent tasks.
#
# Only *terminal* tasks are evictable: an in-flight task still owns the
# ``object_ref`` that ``cancel_task`` needs, and that ref is not serializable, so
# it cannot live anywhere but here. In-flight tasks are self-limiting (the pool
# has bounded capacity and every task eventually settles), terminal ones are not.
#
# Both bounds apply. The cap is the hard memory guarantee; the TTL keeps a quiet
# deployment from serving hours-old state out of the cache. Reads that miss fall
# back to Postgres (see ``WorkerDispatcher.get_task_state`` / ``JobService``).
_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_MAX_TERMINAL_TASKS = 2000
_TERMINAL_TTL_SECONDS = 3600.0
_MAX_ERROR_CHARS = MAX_ERROR_TEXT_CHARS


@dataclass
class TaskInfo:
    state: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    object_ref: ray.ObjectRef | None = None


@ray.remote(concurrency_groups={"set": 1000, "get": 1000, "queue_info": 1000})
class TaskStateManager:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskInfo] = {}
        self.user_index: dict[int, set[str]] = {}
        # task_id -> monotonic timestamp of the terminal transition, in
        # insertion order so eviction is FIFO (oldest settled task first).
        self.terminal_at: OrderedDict[str, float] = OrderedDict()
        self.lock = asyncio.Lock()

    async def _ensure_task(self, task_id: str) -> TaskInfo:
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskInfo()
        return self.tasks[task_id]

    def _mark_terminal(self, task_id: str, state: str | None) -> None:
        """Record (or clear) a task's terminal transition, then evict.

        Called with ``self.lock`` held, from every state write.
        """
        if state in _TERMINAL_STATES:
            self.terminal_at[task_id] = time.monotonic()
            self.terminal_at.move_to_end(task_id)
            self._evict_terminal()
        else:
            # A task that leaves a terminal state (a re-dispatch reusing the id)
            # must stop being a candidate for eviction.
            self.terminal_at.pop(task_id, None)

    def _evict_terminal(self) -> None:
        """Drop terminal tasks that are over the cap or past the TTL."""
        now = time.monotonic()
        while self.terminal_at:
            task_id, settled_at = next(iter(self.terminal_at.items()))
            over_cap = len(self.terminal_at) > _MAX_TERMINAL_TASKS
            expired = now - settled_at > _TERMINAL_TTL_SECONDS
            if not (over_cap or expired):
                # FIFO: the head is the oldest, so nothing behind it can qualify.
                break
            self.terminal_at.popitem(last=False)
            self._forget(task_id)

    def _forget(self, task_id: str) -> None:
        info = self.tasks.pop(task_id, None)
        if info is None:
            return
        user_id = info.details.get("user_id")
        task_ids = self.user_index.get(user_id)
        if task_ids is None:
            return
        task_ids.discard(task_id)
        if not task_ids:
            del self.user_index[user_id]

    @ray.method(concurrency_group="set")
    async def set_state(self, task_id: str, state: str) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.state = state
            self._mark_terminal(task_id, state)

    @ray.method(concurrency_group="set")
    async def set_error(self, task_id: str, tb_str: str) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.error = truncate_error_text(tb_str, _MAX_ERROR_CHARS)

    @ray.method(concurrency_group="set")
    async def set_failed_if_not_cancelled(self, task_id: str, tb_str: str) -> bool:
        """Atomically set state to FAILED and record the traceback, unless already CANCELLED."""
        async with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state == "CANCELLED":
                return False
            info.state = "FAILED"
            info.error = truncate_error_text(tb_str, _MAX_ERROR_CHARS)
            self._mark_terminal(task_id, "FAILED")
            return True

    @ray.method(concurrency_group="set")
    async def set_details(
        self,
        task_id: str,
        *,
        file_id: str,
        partition: int,
        metadata: dict,
        user_id: int,
    ) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.details = {
                "file_id": file_id,
                "partition": partition,
                "metadata": metadata,
                "user_id": user_id,
            }
            self.user_index.setdefault(user_id, set()).add(task_id)

    @ray.method(concurrency_group="set")
    async def set_object_ref(self, task_id: str, object_ref: ray.ObjectRef) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.object_ref = object_ref

    @ray.method(concurrency_group="get")
    async def get_state(self, task_id: str) -> str | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.state if info else None

    @ray.method(concurrency_group="get")
    async def get_error(self, task_id: str) -> str | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.error if info else None

    @ray.method(concurrency_group="get")
    async def get_details(self, task_id: str) -> dict | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.details if info else None

    @ray.method(concurrency_group="get")
    async def get_object_ref(self, task_id: str) -> ray.ObjectRef | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.object_ref if info else None

    @ray.method(concurrency_group="queue_info")
    async def get_all_states(self) -> dict[str, str | None]:
        async with self.lock:
            return {tid: info.state for tid, info in self.tasks.items()}

    @ray.method(concurrency_group="queue_info")
    async def get_all_info(self) -> dict[str, dict]:
        async with self.lock:
            return {
                task_id: {
                    "state": info.state,
                    "error": info.error,
                    "details": info.details,
                }
                for task_id, info in self.tasks.items()
            }

    @ray.method(concurrency_group="queue_info")
    async def get_all_user_info(self, user_id: int) -> dict[str, dict]:
        async with self.lock:
            task_ids = self.user_index.get(user_id, set())
            return {
                tid: {
                    "state": self.tasks[tid].state,
                    "error": self.tasks[tid].error,
                    "details": self.tasks[tid].details,
                }
                for tid in task_ids
                if tid in self.tasks
            }

    @ray.method(concurrency_group="queue_info")
    async def get_pool_info(self) -> dict[str, int]:
        return {
            "pool_size": _POOL_SIZE,
            "max_tasks_per_worker": _MAX_TASKS_PER_WORKER,
            "total_capacity": _POOL_SIZE * _MAX_TASKS_PER_WORKER,
        }

    @ray.method(concurrency_group="queue_info")
    async def get_user_pending_task_count(self, user_id: int) -> int:
        async with self.lock:
            task_ids = self.user_index.get(user_id, set())
            pending_states = {"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"}
            return sum(1 for tid in task_ids if (info := self.tasks.get(tid)) and info.state in pending_states)


__all__ = ["TaskInfo", "TaskStateManager"]
