from __future__ import annotations

import base64
import json
import os

from flask import Flask, jsonify, redirect, request
from flask_sock import Sock

from word_constructor.app import (
    public_base_url,
    _TB_DL_TIMEOUT,
    _TB_WS_TIMEOUT,
    _is_expired,
    _read_meta,
    _session_template_path,
    _tb_ws_register,
    word_constructor,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("ADMIN_SESSION_SECRET", os.environ.get("SECRET_KEY", "dev-admin-session-secret"))
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.environ.get("MAX_CONTENT_LENGTH", str(32 * 1024 * 1024))
    )
    app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}

    app.register_blueprint(word_constructor, url_prefix="/services/word-constructor")
    sock = Sock(app)

    @app.get("/")
    def index():
        return redirect("/services/word-constructor/")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "word-constructor"})

    @sock.route("/services/word-constructor/api/template-builder/<session_id>/ws")
    def template_builder_ws(ws, session_id):
        meta = _read_meta(session_id)
        if meta is None or meta.get("type") != "template_builder":
            ws.close(1008, "Not found")
            return
        if _is_expired(meta):
            ws.close(1008, "Expired")
            return

        entry = _tb_ws_register(session_id)
        ws.send(json.dumps({"type": "connected", "session_id": session_id}))

        def build_ready_payload() -> dict:
            payload = {
                "type": "template_ready",
                "session_id": session_id,
                "filename": meta.get("filename", "template.docx"),
                "download_url": (
                    f"{public_base_url(request)}/services/word-constructor/"
                    f"api/template-builder/{session_id}/download"
                ),
            }
            path = _session_template_path(session_id)
            if path.exists():
                raw = path.read_bytes()
                payload["content_base64"] = base64.b64encode(raw).decode("ascii")
                payload["size_bytes"] = len(raw)
            return payload

        current_meta = _read_meta(session_id)
        if current_meta and current_meta.get("status") == "ready":
            payload = entry.get("payload") or build_ready_payload()
            ws.send(json.dumps(payload))
            entry["download_event"].wait(timeout=_TB_DL_TIMEOUT)
            ws.close(1000, "Downloaded")
            return

        if entry["event"].wait(timeout=_TB_WS_TIMEOUT):
            payload = entry.get("payload") or build_ready_payload()
            ws.send(json.dumps(payload))
            entry["download_event"].wait(timeout=_TB_DL_TIMEOUT)
            ws.close(1000, "Downloaded")
            return

        ws.send(json.dumps({"type": "timeout", "session_id": session_id}))
        ws.close(1000, "Timeout")

    return app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
