from concurrent.futures import ThreadPoolExecutor

import pytest

from server_state import TaskStore


def test_snapshots_and_inputs_are_detached():
    store = TaskStore()
    uid = store.create("stt")
    source = [{"name": "test.srt"}]
    store.set_outputs(uid, source)
    source[0]["name"] = "changed"
    snapshot = store.snapshot(uid)
    snapshot["outputs"].clear()
    store.history()[0]["outputs"].clear()
    assert store.snapshot(uid)["outputs"] == [{"name": "test.srt"}]


@pytest.mark.parametrize("terminal", ["done", "error", "stopped"])
def test_terminal_state_cannot_be_overwritten(terminal):
    store = TaskStore()
    uid = store.create("stt")
    store.set_status(uid, "running")
    store.set_status(uid, terminal)
    before = store.snapshot(uid)
    store.set_status(uid, "done")
    store.set_error(uid, "late")
    store.append_log(uid, "late")
    store.set_outputs(uid, [{"name": "late"}])
    assert store.snapshot(uid) == before
    assert before["finished_at"] >= before["started_at"]


def test_independent_readers_bounded_buffers_and_cursors():
    store = TaskStore()
    uid = store.create("stt")
    for i in range(2100):
        store.append_log(uid, str(i))
    first = store.events_since(uid)
    assert first == store.events_since(uid)
    assert len(first["events"]) == len(store.snapshot(uid)["logs"]) == 2000
    assert first["events"][0]["id"] == 101
    first["events"].clear()
    assert len(store.events_since(uid)["events"]) == 2000
    assert store.events_since(uid, first["cursor"])["events"] == []
    store.append_log(uid, "new")
    assert store.events_since(uid, first["cursor"])["events"][0]["text"] == "new"


def test_capacity_only_evicts_terminal_tasks():
    store = TaskStore(max_tasks=1)
    uid = store.create("stt")
    with pytest.raises(ValueError):
        store.create("stt")
    store.set_status(uid, "done")
    other = store.create("stt")
    assert store.snapshot(uid) is None
    assert store.events_since(uid) == {"cursor": 0, "events": []}
    assert store.snapshot(other)["status"] == "pending"


def test_concurrent_logging_preserves_unique_events():
    store = TaskStore()
    uid = store.create("stt")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: store.append_log(uid, str(n)), range(400)))
    events = store.events_since(uid)["events"]
    assert len(events) == 400
    assert len({item["id"] for item in events}) == 400
