"""MemTrace delete/purge hygiene, background diffing and graph pruning.

Covers the follow-up fixes for the merged tracing feature:

- deleting markdown redacts the entry's text from the export (the node
  stays, its content becomes a tombstone);
- ``on_delete`` drops the scope's own graphs (raw request messages
  included) and redacts cross-session copies of its entries;
- ``on_delete_by_ids`` redacts single memories in every graph of the
  app/project;
- ``on_purge`` removes every graph of the purged scope, on disk too;
- ``background`` sync mode keeps the diff off the request path and
  ``wait_idle`` drains it;
- exports prune graphs down to ``EVEROS_MEMTRACE_MAX_GRAPH_NODES``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from everos.infra.memtrace.tracker import _REDACTED, MemTraceTracker
from everos.memory.search.dto import (
    FilterNode,
    SearchAtomicFactItem,
    SearchData,
    SearchEpisodeItem,
    SearchMethod,
    SearchRequest,
    SearchResponse,
)


@pytest.fixture(autouse=True)
def _memtrace_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracing is opt-in; these tests exercise the enabled tracker."""
    monkeypatch.setenv("EVEROS_MEMTRACE_ENABLED", "1")


async def _empty_memcells(ids: list[str]) -> list[Any]:
    return []


memcell_repo = SimpleNamespace(find_by_ids=_empty_memcells)


@pytest.fixture(autouse=True)
def _inject_memcell_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    original_init = MemTraceTracker.__init__

    def init(tracker: MemTraceTracker) -> None:
        original_init(tracker)
        tracker.set_memcell_repo(memcell_repo)

    monkeypatch.setattr(MemTraceTracker, "__init__", init)


_TS = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

_ENTRY_MD = """---
user_id: {owner}
---
<!-- entry:{eid} -->
**owner_id**: {owner}
**session_id**: {session}
**timestamp**: 2026-01-01T00:00:00+00:00
**sender_ids**: [{owner}]
### Content
{body}
<!-- /entry:{eid} -->
"""


def _write_episode(
    tmp_path: Path,
    *,
    owner: str = "alice",
    eid: str = "ep_1",
    session: str = "sess_a",
    body: str = "EP-MARKER alice likes tea",
) -> Path:
    path = tmp_path / "users" / owner / "episodes" / f"{eid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _ENTRY_MD.format(owner=owner, eid=eid, session=session, body=body),
        encoding="utf-8",
    )
    return path


def _req(owner: str = "alice", session: str | None = None) -> SearchRequest:
    options: dict[str, Any] = {
        "user_id": owner,
        "query": "q",
        "method": SearchMethod.HYBRID,
        "top_k": 5,
    }
    if session is not None:
        options["filters"] = FilterNode(session_id=session)
    return SearchRequest(**options)


def _episode_item(owner: str, eid: str, text: str) -> SearchEpisodeItem:
    return SearchEpisodeItem(
        id=eid,
        user_id=owner,
        session_id="sess_a",
        timestamp=_TS,
        sender_ids=[owner],
        subject="s",
        summary="s",
        episode=text,
        type="Conversation",
        score=0.9,
        atomic_facts=[
            SearchAtomicFactItem(id=f"{eid}_f1", content=f"fact {eid}", score=0.9)
        ],
    )


def _resp(*hits: tuple[str, str, str]) -> SearchResponse:
    return SearchResponse(
        request_id="r" * 32,
        data=SearchData(
            episodes=[_episode_item(owner, eid, text) for owner, eid, text in hits]
        ),
    )


def _payload(
    *contents: str, owner: str = "alice", session: str = "sess_a"
) -> dict[str, Any]:
    return {
        "session_id": session,
        "app_id": "default",
        "project_id": "default",
        "messages": [
            {
                "sender_id": owner,
                "role": "user",
                "content": content,
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
            for content in contents
        ],
    }


def _grep_export(out_dir: Path, needle: str) -> list[str]:
    return [
        path.name
        for path in out_dir.glob("*.json")
        if needle in path.read_text(encoding="utf-8")
    ]


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MemTraceTracker:
    """An inline-mode tracker exporting into a per-test directory."""
    monkeypatch.setenv("EVEROS_MEMTRACE_DIR", str(tmp_path / "memtrace"))
    return MemTraceTracker()


async def test_md_deletion_redacts_entry_text(
    tracker: MemTraceTracker, tmp_path: Path
) -> None:
    """The probe scenario: a deleted md must not keep its text in the export."""
    md = _write_episode(tmp_path)
    await tracker.on_search(_req(), _resp())
    assert _grep_export(tracker.out_dir, "EP-MARKER")

    md.unlink()
    await tracker.on_search(_req(), _resp())

    assert not _grep_export(tracker.out_dir, "EP-MARKER")
    assert _grep_export(tracker.out_dir, _REDACTED)


async def test_on_delete_session_drops_graph_with_raw_messages(
    tracker: MemTraceTracker,
) -> None:
    """Session delete removes the graph holding the raw request messages."""
    await tracker.on_memorize(
        _payload("MSG-MARKER hello"), is_final=False, defer_extraction=True, result=None
    )
    assert _grep_export(tracker.out_dir, "MSG-MARKER")

    await tracker.on_delete(app_id="default", project_id="default", session_id="sess_a")

    assert not list(tracker.out_dir.glob("*.json"))
    assert not _grep_export(tracker.out_dir, "MSG-MARKER")


async def test_on_delete_owner_drops_only_that_owner(
    tracker: MemTraceTracker,
) -> None:
    """Owner delete clears every session graph of the owner, keeping others."""
    await tracker.on_memorize(
        _payload("ALICE-MARKER", owner="alice", session="sess_a"),
        is_final=False,
        defer_extraction=True,
        result=None,
    )
    await tracker.on_memorize(
        _payload("BOB-MARKER", owner="bob", session="sess_b"),
        is_final=False,
        defer_extraction=True,
        result=None,
    )
    assert _grep_export(tracker.out_dir, "ALICE-MARKER")
    assert _grep_export(tracker.out_dir, "BOB-MARKER")

    await tracker.on_delete(app_id="default", project_id="default", user_id="alice")

    assert not _grep_export(tracker.out_dir, "ALICE-MARKER")
    assert _grep_export(tracker.out_dir, "BOB-MARKER")


async def test_on_delete_redacts_cross_session_copies(
    tracker: MemTraceTracker, tmp_path: Path
) -> None:
    """A copy of the deleted entry in another session's graph is redacted."""
    _write_episode(tmp_path, body="EP-MARKER shared across sessions")
    await tracker.on_search(_req(session="sess_a"), _resp())
    # A search from another session copies the canonical node over.
    await tracker.on_search(
        _req(session="sess_b"),
        _resp(("alice", "ep_1", "EP-MARKER shared across sessions")),
    )
    graphs = sorted(tracker.out_dir.glob("*.json"))
    assert len(graphs) == 2
    assert len(_grep_export(tracker.out_dir, "EP-MARKER")) == 2

    await tracker.on_delete(app_id="default", project_id="default", session_id="sess_a")

    # sess_a's graph is gone; sess_b's copy lost the text but keeps the node.
    assert len(_grep_export(tracker.out_dir, "EP-MARKER")) == 0
    assert _grep_export(tracker.out_dir, _REDACTED)


async def test_on_delete_by_ids_redacts_every_copy(
    tracker: MemTraceTracker, tmp_path: Path
) -> None:
    """``/delete_by_ids`` scrubs canonical nodes and cross-session copies."""
    _write_episode(tmp_path, body="EP-MARKER deleted by id")
    await tracker.on_search(_req(session="sess_a"), _resp())
    await tracker.on_search(
        _req(session="sess_b"), _resp(("alice", "ep_1", "EP-MARKER deleted by id"))
    )
    assert len(_grep_export(tracker.out_dir, "EP-MARKER")) == 2

    await tracker.on_delete_by_ids(app_id="default", project_id="default", ids=["ep_1"])

    assert not _grep_export(tracker.out_dir, "EP-MARKER")
    assert len(_grep_export(tracker.out_dir, _REDACTED)) == 2


async def test_on_purge_removes_scope_graphs(
    tracker: MemTraceTracker, tmp_path: Path
) -> None:
    """Purge drops every graph of the app/project, on disk and in memory."""
    _write_episode(tmp_path, body="EP-MARKER purged")
    await tracker.on_memorize(
        _payload("MSG-MARKER purged"),
        is_final=False,
        defer_extraction=True,
        result=None,
    )
    await tracker.on_search(_req(), _resp())
    assert _grep_export(tracker.out_dir, "MSG-MARKER")
    assert _grep_export(tracker.out_dir, "EP-MARKER")

    await tracker.on_purge(app_id="default", project_id="default")

    assert not list(tracker.out_dir.glob("*.json"))
    assert not tracker._graphs
    assert not _grep_export(tracker.out_dir, "MSG-MARKER")
    assert not _grep_export(tracker.out_dir, "EP-MARKER")


async def test_on_purge_covers_graphs_never_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fresh process purges graphs a previous process exported."""
    monkeypatch.setenv("EVEROS_MEMTRACE_DIR", str(tmp_path / "memtrace"))
    first = MemTraceTracker()
    _write_episode(tmp_path, body="EP-MARKER from a previous process")
    await first.on_search(_req(), _resp())
    assert _grep_export(first.out_dir, "EP-MARKER")

    restarted = MemTraceTracker()
    assert not restarted._graphs  # nothing loaded yet
    await restarted.on_purge(app_id="default", project_id="default")

    assert not list(restarted.out_dir.glob("*.json"))


async def test_background_sync_keeps_diff_off_the_request_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``background`` mode defers the scan; ``wait_idle`` applies it."""
    monkeypatch.setenv("EVEROS_MEMTRACE_DIR", str(tmp_path / "memtrace"))
    monkeypatch.setenv("EVEROS_MEMTRACE_SYNC_MODE", "background")
    tracker = MemTraceTracker()
    _write_episode(tmp_path)

    # A burst of concurrent requests coalesces into one queued worker.
    await asyncio.gather(*(tracker.on_search(_req(), _resp()) for _ in range(20)))
    await tracker.wait_idle()

    assert tracker._first_scan_done
    assert _grep_export(tracker.out_dir, "EP-MARKER")
    # Drained: no pending triggers and no live worker.
    assert not tracker._pending_triggers
    assert tracker._sync_task is None


async def test_sync_now_drains_background_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shutdown sync applies everything the background worker queued."""
    monkeypatch.setenv("EVEROS_MEMTRACE_DIR", str(tmp_path / "memtrace"))
    monkeypatch.setenv("EVEROS_MEMTRACE_SYNC_MODE", "background")
    tracker = MemTraceTracker()
    _write_episode(tmp_path)
    await tracker.on_search(_req(), _resp())

    await tracker.sync_now()

    assert _grep_export(tracker.out_dir, "EP-MARKER")


async def test_export_prunes_graph_to_node_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Long sessions stop growing: exports stay within the node cap."""
    monkeypatch.setenv("EVEROS_MEMTRACE_DIR", str(tmp_path / "memtrace"))
    monkeypatch.setenv("EVEROS_MEMTRACE_MAX_GRAPH_NODES", "30")
    tracker = MemTraceTracker()
    for _ in range(40):
        await tracker.on_search(_req(), _resp())
        await tracker.on_memorize(
            _payload("hello"), is_final=False, defer_extraction=True, result=None
        )

    for path in tracker.out_dir.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["data"]["nodes"]) <= 30
        retrieval_sessions = [
            session
            for session in data["data"]["sessions"]
            if session.get("category") == "memory_retrieval"
        ]
        # The oldest retrieval sessions went first; recent history survives.
        assert len(retrieval_sessions) < 40
