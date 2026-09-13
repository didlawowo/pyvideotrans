# -*- coding: utf-8 -*-
"""
pyVideoTrans — WebUI (FastAPI + HTMX) en français.

Serveur web léger qui expose les 4 modes du desktop dans le navigateur :
  - vtv : traduction vidéo complète (ASR → traduction → TTS → montage)
  - sts : traduction de sous-titres (SRT)
  - stt : transcription seule
  - tts : doublage / voix off depuis un SRT

Temps réel via Server-Sent-Events (SSE) branché sur le TaskStore mémoire.
Les tâches lourdes s'exécutent une à la fois dans un thread (pas de batch),
en réutilisant les classes de tâches upstream (TranslateSrt, SpeechToText,
DubbingSrt, TransCreate) exactement comme le fait cli.py.

Usage :
    python server.py [--host 127.0.0.1 --port 9090]
    MCP_AUTH_TOKEN requis ; voir docs/interface-mcp.md.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import uuid as uuidlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Langue : français pour toute la chaîne (logs backend via tr())
# ---------------------------------------------------------------------------
os.environ.setdefault("PYVIDEOTRANS_LANG", "fr_FR")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from videotrans.configure import config  # noqa: E402

config.init_run()

from videotrans.configure.config import ROOT_DIR, TEMP_DIR  # noqa: E402
from videotrans import recognition, translator, tts  # noqa: E402

from server_state import store  # noqa: E402
from server_bridge import install_bridge  # noqa: E402
from server_models import TaskRequest, input_path  # noqa: E402
from server_auth import AuthMiddleware, browser_cookie  # noqa: E402
from server_mcp import build_mcp  # noqa: E402

install_bridge()

# ---------------------------------------------------------------------------
# FastAPI + templates (dépendances de l'extra "web")
# ---------------------------------------------------------------------------
try:
    from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
    from fastapi.responses import (
        FileResponse,
        HTMLResponse,
        StreamingResponse,
        RedirectResponse,
    )
    from fastapi.staticfiles import StaticFiles
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError as e:
    raise RuntimeError(
        "Dépendances WebUI manquantes : installer requirements-web.lock"
    ) from e

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"
UPLOAD_DIR = Path(os.getenv("PYVIDEOTRANS_INPUT_DIR", str(Path(ROOT_DIR) / "uploads")))
OUTPUT_DIR = Path(os.getenv("PYVIDEOTRANS_OUTPUT_DIR", str(Path(ROOT_DIR) / "output")))
MAX_UPLOAD_BYTES = int(os.getenv("PYVIDEOTRANS_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
_task_lock = threading.Lock()
AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "").strip()

# Local services are configured by the operator, never by untrusted tool arguments.
for env_name, param_name in {
    "PYVIDEOTRANS_LLM_URL": "localllm_api",
    "PYVIDEOTRANS_LLM_MODEL": "localllm_model",
    "PYVIDEOTRANS_WHISPER_URL": "openairecognapi_url",
    "PYVIDEOTRANS_WHISPER_MODEL": "openairecognapi_model",
}.items():
    if env_name in os.environ:
        config.params[param_name] = os.environ[env_name]
for prefix in ("localllm", "openairecognapi"):
    if not config.params.get(prefix + "_key"):
        config.params[prefix + "_key"] = "no-key"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

template_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

mcp = build_mcp(sys.modules[__name__])
mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    if not AUTH_TOKEN:
        raise RuntimeError("MCP_AUTH_TOKEN est obligatoire")
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="pyVideoTrans WebUI + MCP", version="1.1.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware, token=AUTH_TOKEN)
# Route the ASGI handler directly: no /mcp -> /mcp/ redirect or lost lifespan.
from starlette.routing import Route  # noqa: E402

handler = mcp_app.routes[0].app
app.router.routes.extend([Route("/mcp", handler), Route("/mcp/", handler)])
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Listes de choix pour peupler l'UI
# ---------------------------------------------------------------------------
def _lang_display_list() -> list[dict[str, str]]:
    try:
        return [
            {"code": code, "name": name}
            for code, name in translator.LANGNAME_DICT.items()
            if code != "-"
        ]
    except Exception:
        return []


def _lang_code_from_display(display: Optional[str]) -> str:
    if not display or display in ("-", "No", "auto", "auto-détection", "Automatique"):
        return "auto"
    try:
        for code, name in translator.LANGNAME_DICT.items():
            if name == display:
                return code
    except Exception:
        pass
    if display in translator.LANGNAME_DICT:
        return display
    raise ValueError("Langue inconnue")


def _recogn_names() -> List[str]:
    try:
        return list(recognition.RECOGN_NAME_LIST)
    except Exception:
        return []


def _translate_names() -> List[str]:
    try:
        return list(translator.TRANSLASTE_NAME_LIST)
    except Exception:
        return []


def _tts_names() -> List[str]:
    try:
        return list(tts.TTS_NAME_LIST)
    except Exception:
        return []


def _faster_models() -> List[str]:
    try:
        from videotrans.configure.contants import FASTER_MODELS_DICT

        return list(FASTER_MODELS_DICT.keys())
    except Exception:
        return ["large-v3-turbo"]


def _normalize_basename(basename: str) -> str:
    return re.sub(r"[\s. #*?!:\"]", "-", basename)


def _index_of(display: Optional[str], names: List[str]) -> int:
    if not display:
        return 0
    if display.isdigit() and 0 <= int(display) < len(names):
        return int(display)
    try:
        return names.index(display)
    except ValueError:
        raise ValueError(f"Canal inconnu : {display}") from None


# ---------------------------------------------------------------------------
# Endpoints API (JSON) — réutilisables par un futur MCP
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return '<html lang="fr"><meta charset="utf-8"><title>Connexion — pyVideoTrans</title><h1>pyVideoTrans</h1><form method="post"><label>Jeton d’accès <input type="password" name="token" required autocomplete="current-password"></label><button>Se connecter</button></form></html>'


@app.post("/login")
def login(request: Request, token: str = Form(...)):
    if not hmac.compare_digest(token.encode(), AUTH_TOKEN.encode()):
        raise HTTPException(401, "Jeton incorrect")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "pvt_session",
        browser_cookie(AUTH_TOKEN),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=28800,
    )
    return response


@app.get("/api/inputs")
def api_inputs():
    return {
        "files": [
            {"file_id": p.name, "name": p.name, "size": p.stat().st_size}
            for p in sorted(UPLOAD_DIR.iterdir())
            if p.is_file() and not p.is_symlink()
        ]
    }


@app.get("/api/meta")
def api_meta() -> Dict[str, Any]:
    return {
        "app": "pyvideotrans",
        "version": "4.12",
        "lang": "fr",
        "modes": ["vtv", "sts", "stt", "tts"],
    }


@app.get("/api/options")
def api_options() -> Dict[str, Any]:
    return {
        "recogn": _recogn_names(),
        "translate": _translate_names(),
        "tts": _tts_names(),
        "models": _faster_models(),
        "languages": _lang_display_list(),
    }


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Upload borné ; retourne un identifiant relatif utilisable par HTTP et MCP."""
    filename = Path(file.filename or "fichier").name
    file_id = uuidlib.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{file_id}_{filename}"
    total = 0
    try:
        with open(dest, "xb") as f:
            while chunk := await file.read(1 << 20):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Fichier trop volumineux")
                f.write(chunk)
        if total == 0:
            raise HTTPException(400, "Fichier vide")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {"file_id": dest.name, "name": filename}


@app.post("/api/tasks")
def api_task_create(request: TaskRequest) -> Dict[str, Any]:
    """Crée et lance une tâche. -> {"uuid": "...", "status": "running"}"""
    payload = request.model_dump()
    mode = payload.get("mode")
    if mode not in ("vtv", "sts", "stt", "tts"):
        raise HTTPException(status_code=400, detail=f"mode invalide: {mode}")

    try:
        file_path = input_path(UPLOAD_DIR, request.file_id)
        _lang_code_from_display(request.source_language)
        target = _lang_code_from_display(request.target_language)
        for value, names in (
            (request.recogn, _recogn_names()),
            (request.translate, _translate_names()),
            (request.tts, _tts_names()),
        ):
            _index_of(value, names)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    payload["file_path"] = str(file_path)
    if mode in ("sts", "tts") and file_path.suffix.lower() != ".srt":
        raise HTTPException(400, "Un fichier SRT est requis pour ce mode")
    if mode == "tts" and request.voice_role in ("", "No"):
        raise HTTPException(status_code=400, detail="voice_role requis pour tts")
    if mode in ("sts", "vtv", "tts") and target == "auto":
        raise HTTPException(status_code=400, detail="target_language requis")

    if not _task_lock.acquire(blocking=False):
        raise HTTPException(409, "Une tâche est déjà en cours")
    try:
        uid = store.create(
            mode=mode,
            name=file_path.name,
            source_language=request.source_language,
            target_language=request.target_language,
        )
        store.set_status(uid, "running")
        thread = threading.Thread(
            target=_run_task, args=(uid, payload), name=f"task-{uid[:8]}", daemon=True
        )
        thread.start()
    except Exception:
        _task_lock.release()
        raise
    return {"uuid": uid, "status": "running"}


@app.get("/api/tasks/{uid}")
def api_task_get(uid: str) -> Dict[str, Any]:
    snap = store.snapshot(uid)
    if snap is None:
        raise HTTPException(status_code=404, detail="tâche inconnue")
    return snap


@app.get("/api/tasks/{uid}/events")
async def api_task_events(uid: str, request: Request):
    """SSE : flux d'événements temps réel pour une tâche."""
    if store.snapshot(uid) is None:
        raise HTTPException(status_code=404, detail="tâche inconnue")

    async def event_stream():
        try:
            snap = store.snapshot(uid)
            if snap:
                yield f"data: {json.dumps({'type': 'snapshot', 'task': snap}, ensure_ascii=False)}\n\n"
            last_status = snap.get("status") if snap else None
            cursor = max(0, int(request.headers.get("last-event-id", "0")))
            while True:
                if await request.is_disconnected():
                    break
                batch = store.events_since(uid, cursor)
                cursor = batch["cursor"]
                events = batch["events"]
                for ev in events:
                    yield f"id: {ev['id']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                snap = store.snapshot(uid)
                if snap and snap["status"] != last_status:
                    yield f"data: {json.dumps({'type': 'status', 'status': snap['status']}, ensure_ascii=False)}\n\n"
                    last_status = snap["status"]
                if snap and snap["status"] in ("done", "error", "stopped"):
                    yield f"data: {json.dumps({'type': 'end', 'task': snap}, ensure_ascii=False)}\n\n"
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(1.0)
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/tasks")
def api_tasks() -> Dict[str, Any]:
    return {
        "tasks": [
            {
                "uuid": t["uuid"],
                "status": t["status"],
                "mode": t["mode"],
                "name": t["name"],
            }
            for t in store.history()
        ]
    }


@app.get("/api/outputs")
def api_outputs() -> Dict[str, Any]:
    return {"files": _collect_outputs()}


@app.get("/api/outputs/{path:path}")
def api_outputs_download(path: str):
    f = (OUTPUT_DIR / path).resolve()
    if not f.is_relative_to(OUTPUT_DIR.resolve()) or not f.is_file():
        raise HTTPException(status_code=404, detail="fichier introuvable")
    return FileResponse(f, filename=f.name)


def read_subtitles(uid: str, filename: str):
    snap = api_task_get(uid)
    if snap["status"] != "done":
        raise ValueError("La tâche n'est pas terminée")
    if not any(item["name"] == filename for item in snap["outputs"]):
        raise ValueError("Résultat inconnu pour cette tâche")
    path = input_path(OUTPUT_DIR / uid, filename)
    if path.suffix.lower() not in (".srt", ".txt") or path.stat().st_size > 1024 * 1024:
        raise ValueError(
            "Télécharger ce résultat par HTTP (texte trop volumineux ou format binaire)"
        )
    return {"name": filename, "text": path.read_text(encoding="utf-8-sig")}


# ---------------------------------------------------------------------------
# Page principale (HTML + HTMX)
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    html = template_env.get_template("index.html").render(
        options={
            "recogn": _recogn_names(),
            "translate": _translate_names(),
            "tts": _tts_names(),
            "models": _faster_models(),
            "languages": _lang_display_list(),
        },
    )
    return html


# ---------------------------------------------------------------------------
# Exécution des tâches (réplique de cli.py en mode thread)
# ---------------------------------------------------------------------------
def _run_task(uid: str, payload: Dict[str, Any]) -> None:
    mode = payload.get("mode")
    store.append_log(uid, f"Préparation de la tâche ({mode})...")
    try:
        config.app_cfg.current_status = "ing"
        from videotrans.util import tools
        from videotrans.util.gpus import getset_gpu

        getset_gpu()

        # --- common params (réplique de build_common_params de cli.py)
        fp = Path(payload["file_path"]).absolute().as_posix()
        _file_obj = tools.format_video(fp)
        _nospace = _normalize_basename(_file_obj["basename"])
        _target_dir = str(OUTPUT_DIR / uid)
        _cache_folder = f"{TEMP_DIR}/{uid}"
        Path(_cache_folder).mkdir(parents=True, exist_ok=True)
        Path(_target_dir).mkdir(parents=True, exist_ok=True)
        common = {
            "name": payload["file_path"],
            "cache_folder": _cache_folder,
            "target_dir": _target_dir,
            **{
                k: _file_obj[k]
                for k in ("uuid", "dirname", "basename", "noextname", "ext")
            },
            "uuid": uid,
        }

        source_code = _lang_code_from_display(payload.get("source_language"))
        target_code = _lang_code_from_display(payload.get("target_language"))
        translate_idx = _index_of(payload.get("translate"), _translate_names())
        recogn_idx = _index_of(payload.get("recogn"), _recogn_names())
        tts_idx = _index_of(payload.get("tts"), _tts_names())
        model = payload.get("model") or "large-v3-turbo"

        # consiste à reprendre le mapping exact de cli.py
        if mode == "sts":
            common.update(
                {
                    "source_language_code": source_code,
                    "target_language_code": target_code,
                    "translate_type": translate_idx,
                }
            )
            from videotrans.task.taskcfg import TaskCfgSTS
            from videotrans.task.translate_srt import TranslateSrt

            trk = TranslateSrt(cfg=TaskCfgSTS(**common), out_format=0)
            trk.prepare()
            trk.trans()
            trk.task_done()

        elif mode == "stt":
            common.update(
                {
                    "source_language_code": source_code,
                    "recogn_type": recogn_idx,
                    "detect_language": source_code,
                    "model_name": model,
                    "is_cuda": False,
                    "remove_noise": bool(payload.get("remove_noise", False)),
                    "enable_diariz": False,
                    "nums_diariz": -1,
                    "rephrase": False,
                    "fix_punc": int(payload.get("fix_punc", 0)),
                }
            )
            from videotrans.task.taskcfg import TaskCfgSTT
            from videotrans.task.speech2text import SpeechToText

            trk = SpeechToText(cfg=TaskCfgSTT(**common), out_format="srt")
            trk.prepare()
            trk.recogn()
            trk.diariz()
            trk.task_done()

        elif mode == "tts":
            common.update(
                {
                    "target_language_code": target_code,
                    "tts_type": tts_idx,
                    "voice_role": payload.get("voice_role"),
                    "voice_rate": "+0%",
                    "volume": str(payload.get("volume", "+0%")),
                    "pitch": "+0Hz",
                    "is_cuda": False,
                    "voice_autorate": False,
                    "align_sub_audio": True,
                }
            )
            from videotrans.task.taskcfg import TaskCfgTTS
            from videotrans.task.dubbing import DubbingSrt

            trk = DubbingSrt(cfg=TaskCfgTTS(**common), out_ext="wav")
            trk.prepare()
            trk.dubbing()
            trk.align()
            trk.task_done()

        elif mode == "vtv":
            common.update(
                {
                    "source_language_code": source_code,
                    "target_language_code": target_code,
                    "translate_type": translate_idx,
                    "recogn_type": recogn_idx,
                    "detect_language": source_code,
                    "model_name": model,
                    "is_cuda": False,
                    "remove_noise": False,
                    "enable_diariz": False,
                    "nums_diariz": -1,
                    "rephrase": False,
                    "fix_punc": 0,
                    "tts_type": tts_idx,
                    "voice_role": payload.get("voice_role") or "No",
                    "voice_rate": "+0%",
                    "volume": "+0%",
                    "pitch": "+0Hz",
                    "voice_autorate": False,
                    "video_autorate": False,
                    "align_sub_audio": True,
                    "is_separate": False,
                    "recogn2pass": False,
                    "subtitle_type": int(payload.get("subtitle_type", 2)),
                    "embed_bgm": True,
                    "loop_backaudio": 0,
                    "backaudio_volume": 0.8,
                    "background_music": "",
                    "clear_cache": True,
                    "app_mode": "biaozhun",
                }
            )
            from videotrans.task.taskcfg import TaskCfgVTT
            from videotrans.task.trans_create import TransCreate

            trk = TransCreate(cfg=TaskCfgVTT(**common))
            trk.prepare()
            trk.recogn()
            trk.diariz()
            trk.trans()
            trk.dubbing()
            trk.align()
            trk.recogn2pass()
            trk.assembling()
            trk.task_done()

        if store.snapshot(uid)["status"] == "running":
            store.append_log(uid, "✅ Tâche terminée.")
            store.set_outputs(uid, _collect_outputs(OUTPUT_DIR / uid))
            store.set_status(uid, "done")
    except Exception as exc:
        store.append_log(uid, f"❌ Erreur: {exc}", level="error")
        store.set_error(uid, str(exc))
    finally:
        config.app_cfg.current_status = "stop"
        _task_lock.release()


def _collect_outputs(directory=None) -> List[Dict[str, Any]]:
    files = []
    if not OUTPUT_DIR.exists():
        return files
    for p in sorted((directory or OUTPUT_DIR).rglob("*")):
        if not p.is_file() or not p.resolve().is_relative_to(OUTPUT_DIR.resolve()):
            continue
        if p.suffix.lower() not in (
            ".mp4",
            ".mkv",
            ".srt",
            ".txt",
            ".wav",
            ".mp3",
            ".json",
        ):
            continue
        try:
            rel = p.relative_to(OUTPUT_DIR)
        except ValueError:
            continue
        files.append(
            {
                "url": f"/api/outputs/{rel.as_posix()}",
                "name": p.name,
                "size": p.stat().st_size,
            }
        )
    files.reverse()
    return files


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="pyVideoTrans WebUI (FastAPI)")
    parser.add_argument(
        "--host", type=str, default=os.getenv("PYVIDEOTRANS_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("PYVIDEOTRANS_PORT", "9090"))
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        app, host=args.host, port=args.port, access_log=False, log_level="warning"
    )


if __name__ == "__main__":
    main()
