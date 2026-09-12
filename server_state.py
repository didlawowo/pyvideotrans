# -*- coding: utf-8 -*-
"""Store mémoire thread-safe pour la WebUI (server.py)."""
import threading
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_STATUS = ("pending", "running", "done", "error", "stopped")


@dataclass
class TaskRecord:
    uuid: str
    mode: str  # vtv | sts | stt | tts
    name: str = ""
    source_language: str = ""
    target_language: str = ""
    status: str = "pending"
    logs: List[Dict[str, Any]] = field(default_factory=list)
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: str = ""

    def to_dict(self, with_logs: bool = True) -> Dict[str, Any]:
        d = {
            "uuid": self.uuid,
            "mode": self.mode,
            "name": self.name,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
        if with_logs:
            d["logs"] = self.logs
        return d


class TaskStore:
    """Singleton mémoire : garde l'état de chaque tâche + file d'événements SSE.

    Les tâches lourdes s'exécutent une à la fois dans un thread dédié
    (voir server.py). Les messages produits par `BaseCon.signal()` arrivent
    ici via le bridge (server_bridge.py) → `record()`.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: Dict[str, TaskRecord] = {}
        self._events: Dict[str, List[Dict[str, Any]]] = {}
        self._watchers: Dict[str, int] = {}
        self._cvs: Dict[str, threading.Condition] = {}
        self._generation = 0

    # ------------------------------------------------------------------
    # Accès
    # ------------------------------------------------------------------
    def create(self, mode: str, name: str = "", source_language: str = "", target_language: str = "") -> str:
        uid = uuidlib.uuid4().hex
        rec = TaskRecord(
            uuid=uid,
            mode=mode,
            name=name,
            source_language=source_language,
            target_language=target_language,
        )
        with self._lock:
            self._tasks[uid] = rec
            self._events[uid] = []
            self._watchers[uid] = 0
            self._cvs[uid] = threading.Condition(self._lock)
            self._generation += 1
        return uid

    def get(self, uid: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(uid)

    def list(self) -> List[TaskRecord]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda r: r.created_at, reverse=True)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def set_status(self, uid: str, status: str):
        if status not in _STATUS:
            raise ValueError(f"statut invalide: {status}")
        with self._lock:
            rec = self._tasks.get(uid)
            if not rec:
                return
            rec.status = status
            if status == "running" and rec.started_at is None:
                rec.started_at = time.time()
            if status in ("done", "error", "stopped") and rec.finished_at is None:
                rec.finished_at = time.time()
            self._generation += 1
            self._notify(uid)

    def append_log(self, uid: str, text: str, level: str = "info"):
        with self._lock:
            rec = self._tasks.get(uid)
            if not rec:
                return
            ts = time.strftime("%H:%M:%S")
            rec.logs.append({"time": ts, "level": level, "text": text})
            if len(rec.logs) > 2000:
                rec.logs = rec.logs[-2000:]
            self._events[uid].append({"type": "log", "time": ts, "level": level, "text": text})
            self._generation += 1
            self._notify(uid)

    def set_outputs(self, uid: str, outputs: List[Dict[str, Any]]):
        with self._lock:
            rec = self._tasks.get(uid)
            if not rec:
                return
            rec.outputs = outputs
            self._events[uid].append({"type": "outputs", "outputs": outputs})
            self._generation += 1
            self._notify(uid)

    def set_error(self, uid: str, message: str):
        with self._lock:
            rec = self._tasks.get(uid)
            if not rec:
                return
            rec.error = message
            rec.status = "error"
            if rec.finished_at is None:
                rec.finished_at = time.time()
            self._events[uid].append({"type": "error", "text": message})
            self._generation += 1
            self._notify(uid)

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------
    def wait_next(self, uid: str, generation: int, timeout: float = 25.0) -> int:
        """Blocage jusqu'à une nouvelle génération pour `uid`.

        Retourne la nouvelle génération, ou `generation` si timeout.
        Le client compare pour décider d'émettre un événement SSE.
        """
        with self._lock:
            cv = self._cvs.get(uid)
            if cv is None:
                return generation
            cv.wait(timeout=timeout)
            return self._generation

    def drain_events(self, uid: str) -> List[Dict[str, Any]]:
        """Vide et renvoie les événements accumulés pour `uid`."""
        with self._lock:
            events = list(self._events.get(uid, []))
            if self._events.get(uid):
                self._events[uid] = []
            return events

    def snapshot(self, uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._tasks.get(uid)
            return rec.to_dict() if rec else None

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return [t.to_dict(with_logs=False) for t in sorted(
                self._tasks.values(), key=lambda r: r.created_at, reverse=True
            )][:limit]

    def register_watcher(self, uid: str):
        with self._lock:
            self._watchers[uid] = self._watchers.get(uid, 0) + 1

    def unregister_watcher(self, uid: str):
        with self._lock:
            self._watchers[uid] = max(0, self._watchers.get(uid, 0) - 1)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "tasks": len(self._tasks),
                "watchers": len(self._watchers),
                "generation": self._generation,
            }

    def _notify(self, uid: str):
        cv = self._cvs.get(uid)
        if cv is not None:
            cv.notify_all()


store = TaskStore()


def main():
    uid = store.create("sts", name="demo.srt")
    store.set_status(uid, "running")
    store.append_log(uid, "bonjour")
    store.set_status(uid, "done")
    store.set_outputs(uid, [{"url": "/api/outputs/x.srt", "name": "x.srt"}])
    print(store.snapshot(uid))
    print(store.history())


if __name__ == "__main__":
    main()