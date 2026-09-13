# -*- coding: utf-8 -*-
"""
Bridge web pour la WebUI (server.py).

Le desktop route les messages d'avancement via `videotrans.configure._helpers
.push_queue` → `SignalHub` (PySide6/Qt). En mode web on remplace ce point
d'entrée par une fonction qui écrit dans le `TaskStore` mémoire.

`BaseCon.signal()` (configure/base.py:31) appelle `push_queue(uuid, SignMsg)`
— donc le patch de `push_queue` dans les modules qui l'importent suffit à
capter tous les messages de progression/téléchargement/d'erreur des tâches,
sans toucher au code du cœur applicatif.
"""

from typing import Any, Dict

import videotrans.configure._helpers as _helpers
import videotrans.configure.base as _base
import videotrans.configure.config as _config
import videotrans.util.help_misc as _help_misc

from server_state import store

# Caches de messages par uuid pour les tâches qui n'écrivent pas dans le store
# (messages globaux sans uuid, etc.) — on les ignore.
_LEVEL_MAP: Dict[str, str] = {
    "logs": "info",
    "error": "error",
    "warning": "warning",
    "info": "info",
    "debug": "debug",
}


def _web_push_queue(uuid: str, msg: Any) -> None:
    """Remplacement de push_queue : route les messages vers TaskStore."""
    if not uuid:
        return
    # SignMsg : dataclass avec attributs type/text
    text = getattr(msg, "text", "") or ""
    mtype = getattr(msg, "type", "logs") or "logs"
    if not text:
        return
    if mtype == "error":
        store.append_log(uuid, text, level="error")
        store.set_error(uuid, text)
    else:
        store.append_log(uuid, text, level=_LEVEL_MAP.get(mtype, "info"))


def install_bridge() -> None:
    """Active le mode web : app_cfg.exec_mode='web' + patch de push_queue."""
    _config.app_cfg.exec_mode = "web"
    # _helpers définit push_queue ; on mute aussi les autres points d'entrée
    # qui le relaient (config.py l'importe, base.py et help_misc.py aussi).
    _helpers.push_queue = _web_push_queue
    _config.push_queue = _web_push_queue
    _base.push_queue = _web_push_queue
    _help_misc.push_queue = _web_push_queue
    # send_notification (notifications desktop) désactivé en mode web
    try:
        from videotrans.util import _ffmpeg_misc

        _ffmpeg_misc.send_notification = lambda title, message: None
    except Exception:
        pass
    try:
        from videotrans.util import tools

        tools.send_notification = lambda title, message: None
    except Exception:
        pass


def is_web_mode() -> bool:
    try:
        return _config.app_cfg.exec_mode == "web"
    except Exception:
        return False
