"""``/delete`` and ``/delete_by_ids`` notify the MemTrace tracker."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from everos.service.memory import MemoryService


class _Tracker:
    enabled = True

    def __init__(self) -> None:
        self.deleted: dict[str, Any] | None = None
        self.deleted_ids: dict[str, Any] | None = None

    async def on_delete(self, **kwargs: Any) -> None:
        self.deleted = kwargs

    async def on_delete_by_ids(self, **kwargs: Any) -> None:
        self.deleted_ids = kwargs


class _Extractions:
    @staticmethod
    def supervise(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        return asyncio.ensure_future(coro)


def _memory(tracker: _Tracker | None) -> MemoryService:
    memory = MemoryService.__new__(MemoryService)
    memory._deleter = SimpleNamespace(
        delete=AsyncMock(return_value=SimpleNamespace(filters=[], count=1)),
        delete_by_ids=AsyncMock(return_value=2),
    )
    memory._extractions = _Extractions()
    memory._input_log = None
    memory._memtrace_tracker = tracker
    return memory


async def test_delete_notifies_tracker() -> None:
    tracker = _Tracker()
    memory = _memory(tracker)

    await memory.delete(
        app_id="a", project_id="p", user_id="u", agent_id=None, session_id=None
    )

    assert tracker.deleted == {
        "app_id": "a",
        "project_id": "p",
        "user_id": "u",
        "agent_id": None,
        "session_id": None,
    }


async def test_delete_by_ids_notifies_tracker() -> None:
    tracker = _Tracker()
    memory = _memory(tracker)

    deleted = await memory.delete_by_ids(
        app_id="a",
        project_id="p",
        memory_type="episode",
        ids=["ep_1", "ep_2"],
        reason_code=None,
        note=None,
    )

    assert deleted == 2
    assert tracker.deleted_ids == {
        "app_id": "a",
        "project_id": "p",
        "ids": ["ep_1", "ep_2"],
    }


async def test_delete_without_tracker_is_a_no_op() -> None:
    memory = _memory(None)

    result = await memory.delete(
        app_id="a", project_id="p", user_id="u", agent_id=None, session_id=None
    )

    assert result.count == 1
