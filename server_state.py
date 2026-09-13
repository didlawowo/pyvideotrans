"""État mémoire borné et snapshots détachés pour HTTP et MCP (un seul processus)."""

import copy
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

TERMINAL = {"done", "error", "stopped"}


class TaskStore:
    def __init__(self, max_tasks=100):
        self._lock = threading.Lock()
        self._tasks = {}
        self._events = {}
        self._sequence = {}
        self._max_tasks = max_tasks

    def create(self, mode, name="", source_language="", target_language=""):
        with self._lock:
            if len(self._tasks) >= self._max_tasks:
                victim = next(
                    (
                        key
                        for key, task in self._tasks.items()
                        if task["status"] in TERMINAL
                    ),
                    None,
                )
                if victim is None:
                    raise ValueError("Trop de tâches actives")
                for mapping in (self._tasks, self._events, self._sequence):
                    del mapping[victim]
            uid = uuid.uuid4().hex
            self._tasks[uid] = dict(
                uuid=uid,
                mode=mode,
                name=name,
                source_language=source_language,
                target_language=target_language,
                status="pending",
                logs=[],
                outputs=[],
                error="",
                created_at=time.time(),
                started_at=None,
                finished_at=None,
            )
            self._events[uid] = deque(maxlen=2000)
            self._sequence[uid] = 0
            return uid

    def _event(self, uid, **event):
        self._sequence[uid] += 1
        self._events[uid].append({"id": self._sequence[uid], **event})

    def snapshot(self, uid):
        with self._lock:
            return copy.deepcopy(self._tasks.get(uid))

    def history(self, limit=50):
        with self._lock:
            return [
                copy.deepcopy(
                    {key: value for key, value in task.items() if key != "logs"}
                )
                for task in list(self._tasks.values())[::-1][:limit]
            ]

    def set_status(self, uid, status):
        if status not in {"pending", "running"} | TERMINAL:
            raise ValueError("Statut invalide")
        with self._lock:
            task = self._tasks.get(uid)
            if not task or task["status"] in TERMINAL:
                return
            task["status"] = status
            if status == "running" and task["started_at"] is None:
                task["started_at"] = time.time()
            if status in TERMINAL:
                task["finished_at"] = time.time()
            self._event(uid, type="status", status=status)

    def append_log(self, uid, text, level="info"):
        with self._lock:
            task = self._tasks.get(uid)
            if not task or task["status"] in TERMINAL:
                return
            item = dict(
                time=datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S"),
                level=level,
                text=str(text),
            )
            task["logs"].append(item)
            del task["logs"][:-2000]
            self._event(uid, type="log", **item)

    def set_outputs(self, uid, outputs):
        with self._lock:
            task = self._tasks.get(uid)
            if task and task["status"] not in TERMINAL:
                task["outputs"] = copy.deepcopy(outputs)
                self._event(uid, type="outputs", outputs=copy.deepcopy(outputs))

    def set_error(self, uid, message):
        with self._lock:
            task = self._tasks.get(uid)
            if task and task["status"] not in TERMINAL:
                task.update(error=str(message), status="error", finished_at=time.time())
                self._event(uid, type="error", text=str(message))

    def events_since(self, uid, cursor=0):
        with self._lock:
            return {
                "cursor": self._sequence.get(uid, 0),
                "events": copy.deepcopy(
                    [item for item in self._events.get(uid, ()) if item["id"] > cursor]
                ),
            }


store = TaskStore()
