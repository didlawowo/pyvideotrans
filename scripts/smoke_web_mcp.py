"""Test HTTP/MCP réel ; AUDIO optionnel lance une vraie transcription CPU."""

import os
from pathlib import Path
import time

import httpx


def main():
    url = os.getenv("WEB_URL", "http://127.0.0.1:9090").rstrip("/")
    token = os.environ["MCP_AUTH_TOKEN"]
    with httpx.Client(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=60
    ) as client:
        client.get("/healthz").raise_for_status()
        page = client.get("/")
        page.raise_for_status()
        assert "Transcription" in page.text
        serial = 0

        def rpc(method, params, path="/mcp"):
            nonlocal serial
            serial += 1
            response = client.post(
                path,
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": serial,
                    "method": method,
                    "params": params,
                },
            )
            response.raise_for_status()
            result = response.json()
            if "error" in result:
                raise RuntimeError(result["error"])
            return result["result"]

        def tool(name, arguments=None):
            result = rpc("tools/call", {"name": name, "arguments": arguments or {}})
            if result.get("isError"):
                raise RuntimeError(result)
            return result["structuredContent"]

        for path in ("/mcp", "/mcp/"):
            result = rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pyvideotrans-smoke", "version": "1"},
                },
                path,
            )
            assert result["serverInfo"]["name"] == "pyvideotrans"
        assert len(rpc("tools/list", {})["tools"]) == 6
        assert rpc("resources/read", {"uri": "pyvideotrans://options"})["contents"]
        tool("get_options")
        tool("list_inputs")
        tool("list_tasks")
        print(
            "PASS: page française, handshake sans redirection, six outils et resource MCP",
            flush=True,
        )

        if not os.getenv("AUDIO"):
            return
        audio = Path(os.environ["AUDIO"])
        with audio.open("rb") as stream:
            response = client.post("/api/upload", files={"file": (audio.name, stream)})
        response.raise_for_status()
        task = tool(
            "start_task",
            {
                "request": {
                    "file_id": response.json()["file_id"],
                    "mode": "stt",
                    "source_language": "en",
                    "model": "tiny",
                    "recogn": "0",
                }
            },
        )
        uid = task["uuid"]
        print(f"Transcription réelle lancée : {uid}", flush=True)
        deadline = time.monotonic() + int(os.getenv("TIMEOUT", "600"))
        while time.monotonic() < deadline:
            task = tool("get_task", {"task_id": uid})
            if task["status"] in ("done", "error", "stopped"):
                break
            time.sleep(2)
        assert task["status"] == "done", task
        subtitles = next(
            item for item in task["outputs"] if item["name"].endswith(".srt")
        )
        result = tool("read_subtitles", {"task_id": uid, "filename": subtitles["name"]})
        assert "-->" in result["text"] and len(result["text"].strip()) > 30
        download = client.get(subtitles["url"])
        download.raise_for_status()
        assert download.text.lstrip("\ufeff") == result["text"]
        print(
            "PASS: transcription CPU complète, lecture du SRT par MCP et téléchargement HTTP",
            flush=True,
        )
        print(result["text"], flush=True)


if __name__ == "__main__":
    main()
