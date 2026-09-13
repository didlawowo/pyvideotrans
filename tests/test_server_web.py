import importlib
import threading

import pytest
from fastapi.testclient import TestClient

from server_state import TaskStore


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "test-only-not-a-production-secret")
    monkeypatch.setenv("PYVIDEOTRANS_INPUT_DIR", str(tmp_path / "inputs"))
    monkeypatch.setenv("PYVIDEOTRANS_OUTPUT_DIR", str(tmp_path / "outputs"))
    import server

    server = importlib.reload(server)
    monkeypatch.setattr(server, "store", TaskStore())
    monkeypatch.setattr(server, "_task_lock", threading.Lock())
    return server


@pytest.fixture
def client(service):
    with TestClient(service.app) as client:
        client.headers["Authorization"] = "Bearer test-only-not-a-production-secret"
        yield client


def test_auth_fail_closed(service):
    from server_auth import AuthMiddleware

    with pytest.raises(ValueError):
        AuthMiddleware(service.app, "")
    with TestClient(service.app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/tasks").status_code == 401
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/login", data={"token": "wrong"}).status_code == 401
        assert (
            client.post(
                "/login", data={"token": "test-only-not-a-production-secret"}
            ).status_code
            == 200
        )
        assert client.get("/api/tasks").status_code == 200
        assert client.post("/mcp", json={}).status_code == 401
        assert (
            client.post(
                "/api/tasks", json={}, headers={"Origin": "https://foreign.invalid"}
            ).status_code
            == 401
        )


def rpc(client, method, params=None, path="/mcp"):
    return client.post(
        path,
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_mcp_handshake_tools_and_resource(client, path):
    init = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
        path,
    )
    assert init.status_code == 200
    assert init.json()["result"]["serverInfo"]["name"] == "pyvideotrans"
    names = {
        item["name"]
        for item in rpc(client, "tools/list", path=path).json()["result"]["tools"]
    }
    assert names == {
        "list_inputs",
        "get_options",
        "start_task",
        "get_task",
        "list_tasks",
        "read_subtitles",
    }
    for name in ("list_inputs", "get_options", "list_tasks"):
        assert (
            not rpc(client, "tools/call", {"name": name, "arguments": {}}, path)
            .json()["result"]
            .get("isError")
        )
    assert (
        "contents"
        in rpc(
            client, "resources/read", {"uri": "pyvideotrans://options"}, path
        ).json()["result"]
    )


def test_upload_validation_and_single_active_task(client, service, monkeypatch):
    upload = client.post("/api/upload", files={"file": ("sample.wav", b"audio")}).json()
    assert "path" not in upload
    assert client.get("/api/inputs").json()["files"][0]["file_id"] == upload["file_id"]
    for file_id in ("../sample.wav", "/etc/passwd", "missing.wav"):
        assert client.post("/api/tasks", json={"file_id": file_id}).status_code == 400
    assert (
        client.post(
            "/api/tasks", json={"file_id": upload["file_id"], "recogn": "bogus"}
        ).status_code
        == 400
    )
    started = threading.Event()
    release = threading.Event()

    def runner(uid, payload):
        started.set()
        release.wait(5)
        service._task_lock.release()

    monkeypatch.setattr(service, "_run_task", runner)
    try:
        task = client.post("/api/tasks", json={"file_id": upload["file_id"]})
        assert task.status_code == 200
        assert started.wait(2)
        assert (
            client.post("/api/tasks", json={"file_id": upload["file_id"]}).status_code
            == 409
        )
        result = rpc(
            client,
            "tools/call",
            {"name": "get_task", "arguments": {"task_id": task.json()["uuid"]}},
        )
        assert result.json()["result"]["structuredContent"]["status"] == "running"
    finally:
        release.set()


def test_paths_upload_limit_and_subtitle_scope(client, service, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 3)
    assert (
        client.post("/api/upload", files={"file": ("large.wav", b"1234")}).status_code
        == 413
    )
    assert list(service.UPLOAD_DIR.iterdir()) == []
    assert (
        client.post("/api/upload", files={"file": ("empty.wav", b"")}).status_code
        == 400
    )
    outside = tmp_path / "secret.srt"
    outside.write_text("private")
    (service.UPLOAD_DIR / "link.srt").symlink_to(outside)
    assert client.post("/api/tasks", json={"file_id": "link.srt"}).status_code == 400
    (service.OUTPUT_DIR / "link.srt").symlink_to(outside)
    assert client.get("/api/outputs/link.srt").status_code == 404
    uid = service.store.create("stt")
    directory = service.OUTPUT_DIR / uid
    directory.mkdir()
    (directory / "result.srt").write_text("Bonjour")
    service.store.set_outputs(uid, service._collect_outputs(directory))
    service.store.set_status(uid, "done")
    assert service.read_subtitles(uid, "result.srt")["text"] == "Bonjour"
    with pytest.raises(ValueError):
        service.read_subtitles(uid, "../secret.srt")
    response = rpc(
        client,
        "tools/call",
        {
            "name": "read_subtitles",
            "arguments": {"task_id": uid, "filename": "result.srt"},
        },
    )
    assert response.json()["result"]["structuredContent"]["text"] == "Bonjour"


def test_bridge_routes_to_actual_task_uuid(service, monkeypatch):
    import server_bridge
    from types import SimpleNamespace

    monkeypatch.setattr(server_bridge, "store", service.store)
    uid = service.store.create("stt")
    server_bridge._web_push_queue(uid, SimpleNamespace(text="failure", type="error"))
    assert service.store.snapshot(uid)["status"] == "error"
    service.store.set_status(uid, "done")
    assert service.store.snapshot(uid)["status"] == "error"


def test_sse_readers_and_resume_cursor(client, service):
    uid = service.store.create("stt")
    service.store.append_log(uid, "premier")
    service.store.append_log(uid, "second")
    service.store.set_status(uid, "done")
    path = f"/api/tasks/{uid}/events"
    first = client.get(path).text
    second = client.get(path).text
    assert first == second
    assert "id: 1\n" in first and "id: 2\n" in first
    resumed = client.get(path, headers={"Last-Event-ID": "1"}).text
    assert "id: 1\n" not in resumed and "id: 2\n" in resumed
    assert '"type": "end"' in resumed


@pytest.fixture
def browser(service):
    """Client authentifié par le COOKIE (pas de Bearer) : seul ce chemin passe
    par le contrôle d'origine, qu'un Bearer valide court-circuite."""
    with TestClient(service.app) as client:
        assert (
            client.post(
                "/login", data={"token": "test-only-not-a-production-secret"}
            ).status_code
            == 200
        )
        yield client


def test_browser_origin_is_matched_on_host_not_scheme(browser):
    """Derrière un proxy qui termine TLS, base_url reste en http alors que le
    navigateur annonce Origin: https — la mutation doit rester acceptée."""
    accepted = browser.post(
        "/api/upload",
        files={"file": ("sample.wav", b"audio")},
        headers={"Origin": "https://testserver"},
    )
    assert accepted.status_code == 200
    for origin in ("https://autre.invalid", "http://testserver:9999", "null"):
        assert (
            browser.post(
                "/api/upload",
                files={"file": ("sample.wav", b"audio")},
                headers={"Origin": origin},
            ).status_code
            == 401
        )


def test_session_cookie_is_not_derived_from_the_bearer(monkeypatch):
    import hashlib
    import hmac
    import secrets

    import server_auth

    token = "test-only-not-a-production-secret"
    legacy = hmac.new(
        token.encode(), b"pyvideotrans-browser-v1", hashlib.sha256
    ).hexdigest()
    assert server_auth.browser_cookie(token) != legacy
    before = server_auth.browser_cookie(token)
    monkeypatch.setattr(server_auth, "_SESSION_SECRET", secrets.token_bytes(32))
    assert server_auth.browser_cookie(token) != before


def _done_task_with(service, files):
    uid = service.store.create("stt")
    directory = service.OUTPUT_DIR / uid
    for relative, text in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    service.store.set_outputs(uid, service._collect_outputs(directory))
    service.store.set_status(uid, "done")
    return uid


def test_read_subtitles_reaches_nested_outputs(service):
    uid = _done_task_with(service, {"sub/result.srt": "Bonjour"})
    assert service.read_subtitles(uid, "result.srt")["text"] == "Bonjour"


def test_read_subtitles_refuses_ambiguous_names(service):
    uid = _done_task_with(service, {"a/result.srt": "A", "b/result.srt": "B"})
    with pytest.raises(ValueError):
        service.read_subtitles(uid, "result.srt")


def test_read_subtitles_stays_inside_the_task_directory(service):
    other = _done_task_with(service, {"result.srt": "autre tache"})
    uid = service.store.create("stt")
    service.store.set_outputs(
        uid,
        [{"url": f"/api/outputs/{other}/result.srt", "name": "result.srt", "size": 1}],
    )
    service.store.set_status(uid, "done")
    with pytest.raises(ValueError):
        service.read_subtitles(uid, "result.srt")


def test_sse_tolerates_a_garbage_last_event_id(client, service):
    uid = service.store.create("stt")
    service.store.append_log(uid, "premier")
    service.store.set_status(uid, "done")
    body = client.get(
        f"/api/tasks/{uid}/events", headers={"Last-Event-ID": "pas-un-entier"}
    ).text
    assert "id: 1\n" in body and '"type": "end"' in body
