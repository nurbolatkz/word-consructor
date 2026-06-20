from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, abort, g, jsonify, request

from word_constructor.app import (
    _authenticate_api_client,
    _record_client_usage,
    _safe_b64decode,
    public_base_url,
)


sign_document = Blueprint("sign_document", __name__)

SIGN_STORAGE_DIR = Path(os.environ.get("SIGN_DOCUMENT_STORAGE_DIR", "/tmp/kazuni_sign_document"))
SIGN_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
SIGN_TTL_SECONDS = max(int(os.environ.get("SIGN_DOCUMENT_SESSION_TTL_SECONDS", str(35 * 60))), 60)
MAX_SIGN_DOCUMENT_BYTES = int(os.environ.get("SIGN_DOCUMENT_MAX_BYTES", str(20 * 1024 * 1024)))

XML_TYPES = {"xml", "text/xml", "application/xml"}
PDF_TYPES = {"pdf", "application/pdf"}


def _utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).replace(microsecond=0).isoformat()


def _request_dir(request_id: str) -> Path:
    return SIGN_STORAGE_DIR / request_id


def _meta_path(request_id: str) -> Path:
    return _request_dir(request_id) / "meta.json"


def _document_path(request_id: str) -> Path:
    return _request_dir(request_id) / "document.bin"


def _signed_path(request_id: str) -> Path:
    return _request_dir(request_id) / "signed.bin"


def _read_meta(request_id: str) -> dict[str, Any] | None:
    path = _meta_path(request_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _write_meta(request_id: str, meta: dict[str, Any]) -> None:
    path = _meta_path(request_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _is_expired(meta: dict[str, Any]) -> bool:
    return time.time() > float(meta.get("expires_at") or 0)


def _public_1c_path(request_id: str, suffix: str) -> str:
    return f"/sign_document/api/1c/requests/{request_id}/{suffix.lstrip('/')}"


def _check_request_id(request_id: str) -> None:
    if len(request_id) > 80 or not all(c.isalnum() or c in "-_" for c in request_id):
        abort(404)


def _normalize_document_type(raw_type: str, filename: str) -> str:
    value = (raw_type or "").strip().lower()
    if value in XML_TYPES:
        return "xml"
    if value in PDF_TYPES:
        return "pdf"
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".xml":
        return "xml"
    if suffix == ".pdf":
        return "pdf"
    raise ValueError("document_type must be 'xml' or 'pdf'")


def _decode_request_document(payload: dict[str, Any]) -> tuple[str, str, bytes]:
    filename = str(payload.get("filename") or "document").strip() or "document"
    document_type = _normalize_document_type(
        str(payload.get("document_type") or payload.get("content_type") or ""),
        filename,
    )
    content_base64 = payload.get("document_base64", payload.get("content_base64"))
    if not isinstance(content_base64, str) or not content_base64.strip():
        raise ValueError("Missing 'document_base64' or 'content_base64'")
    try:
        document_bytes = _safe_b64decode(content_base64)
    except Exception as exc:
        raise ValueError(f"Invalid base64 document: {exc}") from exc
    if not document_bytes:
        raise ValueError("Decoded document is empty")
    if len(document_bytes) > MAX_SIGN_DOCUMENT_BYTES:
        raise ValueError(f"Document is too large. Limit is {MAX_SIGN_DOCUMENT_BYTES} bytes")
    if document_type == "xml":
        try:
            document_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("XML document must be UTF-8 encoded") from exc
        if not filename.lower().endswith(".xml"):
            filename = f"{Path(filename).stem or 'document'}.xml"
    elif document_type == "pdf" and not filename.lower().endswith(".pdf"):
        filename = f"{Path(filename).stem or 'document'}.pdf"
    return filename, document_type, document_bytes


def _auth_1c_request() -> tuple[dict[str, Any] | None, Any | None]:
    client, error = _authenticate_api_client()
    if error is not None:
        return None, error
    g.api_client_id = client.get("id") if client else None
    g.api_client_name = client.get("name") if client else None
    return client, None


def _load_1c_meta(request_id: str) -> tuple[dict[str, Any] | None, Any | None]:
    _check_request_id(request_id)
    client, error = _auth_1c_request()
    if error is not None:
        return None, error
    meta = _read_meta(request_id)
    if meta is None:
        return None, (jsonify({"status": "not_found", "id": request_id}), 404)
    if meta.get("client_id") and client and meta.get("client_id") != client.get("id"):
        return None, (jsonify({"error": "Token is not allowed for this signing request"}), 403)
    if _is_expired(meta):
        meta["status"] = "expired"
        _write_meta(request_id, meta)
        return None, (jsonify(_status_payload(meta)), 410)
    return meta, None


def _status_payload(meta: dict[str, Any]) -> dict[str, Any]:
    request_id = str(meta.get("id") or "")
    payload = {
        "id": request_id,
        "status": meta.get("status", "pending"),
        "document_type": meta.get("document_type"),
        "filename": meta.get("filename"),
        "created_at": meta.get("created_at"),
        "expires_at": meta.get("expires_at_iso"),
        "signed_at": meta.get("signed_at"),
    }
    if meta.get("status") == "signed":
        payload["result_url"] = _public_1c_path(request_id, "result")
        payload["signed_filename"] = meta.get("signed_filename")
        payload["signed_content_type"] = meta.get("signed_content_type")
    if meta.get("status") == "error":
        payload["error"] = meta.get("error") or "Signing failed"
    return payload


def _cleanup_loop() -> None:
    while True:
        time.sleep(60)
        now = time.time()
        for path in SIGN_STORAGE_DIR.iterdir():
            if not path.is_dir():
                continue
            meta = _read_meta(path.name)
            try:
                if meta and now > float(meta.get("expires_at") or 0):
                    shutil.rmtree(path, ignore_errors=True)
                elif not meta and (now - path.stat().st_mtime) > SIGN_TTL_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass


threading.Thread(target=_cleanup_loop, daemon=True).start()


@sign_document.before_request
def _require_token_for_1c_api():
    is_create_alias = request.method == "POST" and request.path.rstrip("/") == "/sign_document"
    if not is_create_alias and not request.path.startswith("/sign_document/api/1c/"):
        return None
    client, error = _auth_1c_request()
    if error is not None:
        return error
    g.api_client_id = client.get("id") if client else None
    return None


@sign_document.after_request
def _record_stats(response):
    client_id = getattr(g, "api_client_id", None)
    if client_id:
        _record_client_usage(client_id, response)
    return response


@sign_document.post("/")
@sign_document.post("/api/1c/create")
@sign_document.post("/api/1c/requests")
def create_sign_request():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected JSON body"}), 400
    try:
        filename, document_type, document_bytes = _decode_request_document(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    request_id = secrets.token_urlsafe(24)
    sdir = _request_dir(request_id)
    sdir.mkdir(parents=True, exist_ok=True)
    expires_at = time.time() + int(payload.get("ttl_seconds") or SIGN_TTL_SECONDS)
    expires_at = min(expires_at, time.time() + max(SIGN_TTL_SECONDS, 60 * 60))
    meta = {
        "id": request_id,
        "type": "sign_document",
        "status": "pending",
        "filename": filename,
        "document_type": document_type,
        "content_type": "application/pdf" if document_type == "pdf" else "application/xml",
        "created_at": _utc_iso(),
        "expires_at": expires_at,
        "expires_at_iso": _utc_iso(expires_at),
        "client_id": getattr(g, "api_client_id", None),
        "description": str(payload.get("description") or ""),
    }
    _document_path(request_id).write_bytes(document_bytes)
    _write_meta(request_id, meta)

    base_url = public_base_url(request)
    sign_path = f"/sign_document/{request_id}"
    return jsonify({
        "id": request_id,
        "status": "pending",
        "sign_url": f"{base_url}{sign_path}",
        "sign_path": sign_path,
        "status_url": _public_1c_path(request_id, "status"),
        "result_url": _public_1c_path(request_id, "result"),
        "expires_at": meta["expires_at_iso"],
    }), 201


@sign_document.get("/api/1c/requests/<request_id>/status")
def sign_request_status(request_id: str):
    meta, error = _load_1c_meta(request_id)
    if error is not None:
        return error
    return jsonify(_status_payload(meta))


@sign_document.get("/api/1c/requests/<request_id>/result")
def sign_request_result(request_id: str):
    meta, error = _load_1c_meta(request_id)
    if error is not None:
        return error
    if meta.get("status") != "signed" or not _signed_path(request_id).exists():
        return jsonify(_status_payload(meta)), 202
    signed_bytes = _signed_path(request_id).read_bytes()
    return jsonify({
        **_status_payload(meta),
        "signed_document_base64": base64.b64encode(signed_bytes).decode("ascii"),
    })


@sign_document.get("/api/browser/requests/<request_id>/document")
def browser_document(request_id: str):
    _check_request_id(request_id)
    meta = _read_meta(request_id)
    if meta is None:
        return jsonify({"error": "Signing request not found"}), 404
    if _is_expired(meta):
        return jsonify({"error": "Signing request expired"}), 410
    document_bytes = _document_path(request_id).read_bytes()
    return jsonify({
        "id": request_id,
        "status": meta.get("status"),
        "filename": meta.get("filename"),
        "document_type": meta.get("document_type"),
        "content_type": meta.get("content_type"),
        "document_base64": base64.b64encode(document_bytes).decode("ascii"),
        "document_text": document_bytes.decode("utf-8") if meta.get("document_type") == "xml" else None,
        "expires_at": meta.get("expires_at_iso"),
        "description": meta.get("description") or "",
    })


@sign_document.post("/api/browser/requests/<request_id>/signed")
def browser_signed_document(request_id: str):
    _check_request_id(request_id)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected JSON body"}), 400
    meta = _read_meta(request_id)
    if meta is None:
        return jsonify({"error": "Signing request not found"}), 404
    if _is_expired(meta):
        return jsonify({"error": "Signing request expired"}), 410

    signed_text = payload.get("signed_document")
    signed_base64 = payload.get("signed_document_base64") or payload.get("signature_base64")
    if isinstance(signed_text, str) and signed_text.strip():
        signed_bytes = signed_text.encode("utf-8")
    elif isinstance(signed_base64, str) and signed_base64.strip():
        try:
            signed_bytes = _safe_b64decode(signed_base64)
        except Exception as exc:
            return jsonify({"error": f"Invalid signed_document_base64: {exc}"}), 400
    else:
        return jsonify({"error": "Missing signed document"}), 400
    if not signed_bytes:
        return jsonify({"error": "Signed document is empty"}), 400

    signed_filename = str(payload.get("signed_filename") or "")
    if not signed_filename:
        if meta.get("document_type") == "xml":
            signed_filename = f"{Path(str(meta.get('filename') or 'document.xml')).stem}-signed.xml"
        else:
            signed_filename = f"{Path(str(meta.get('filename') or 'document.pdf')).stem}.p7s"

    _signed_path(request_id).write_bytes(signed_bytes)
    meta["status"] = "signed"
    meta["signed_at"] = _utc_iso()
    meta["signed_filename"] = signed_filename
    meta["signed_content_type"] = str(
        payload.get("signed_content_type")
        or ("application/xml" if meta.get("document_type") == "xml" else "application/pkcs7-signature")
    )
    _write_meta(request_id, meta)
    return jsonify(_status_payload(meta))


@sign_document.post("/api/browser/requests/<request_id>/error")
def browser_sign_error(request_id: str):
    _check_request_id(request_id)
    payload = request.get_json(silent=True) or {}
    meta = _read_meta(request_id)
    if meta is None:
        return jsonify({"error": "Signing request not found"}), 404
    meta["status"] = "error"
    meta["error"] = str(payload.get("error") or "Signing failed")
    meta["signed_at"] = _utc_iso()
    _write_meta(request_id, meta)
    return jsonify(_status_payload(meta))


@sign_document.get("/<request_id>")
def signing_page(request_id: str):
    _check_request_id(request_id)
    meta = _read_meta(request_id)
    if meta is None:
        abort(404)
    if _is_expired(meta):
        return Response(_page_html(request_id, expired=True), mimetype="text/html; charset=utf-8", status=410)
    return Response(_page_html(request_id), mimetype="text/html; charset=utf-8")


def _page_html(request_id: str, expired: bool = False) -> str:
    bootstrap = json.dumps({"requestId": request_id, "expired": expired}, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Подписание документа</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Arial, Helvetica, sans-serif;
      background: #f5f6f8;
      color: #1f2933;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      box-sizing: border-box;
    }}
    main {{
      width: min(720px, 100%);
      background: #fff;
      border: 1px solid #d9dee7;
      border-radius: 8px;
      padding: 24px;
      box-sizing: border-box;
      box-shadow: 0 10px 30px rgba(20, 35, 55, 0.08);
    }}
    h1 {{ margin: 0 0 14px; font-size: 24px; }}
    dl {{ display: grid; grid-template-columns: 140px 1fr; gap: 8px 16px; margin: 18px 0; }}
    dt {{ color: #596575; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    button {{
      min-height: 42px;
      border: 0;
      border-radius: 6px;
      background: #1565c0;
      color: white;
      font-size: 16px;
      padding: 0 18px;
      cursor: pointer;
    }}
    button:disabled {{ background: #9aa6b2; cursor: default; }}
    .status {{ margin-top: 16px; line-height: 1.45; }}
    .error {{ color: #b42318; }}
    .ok {{ color: #047857; }}
  </style>
</head>
<body>
  <main>
    <h1>Подписание документа</h1>
    <dl>
      <dt>Файл</dt><dd id="filename">...</dd>
      <dt>Тип</dt><dd id="doctype">...</dd>
      <dt>Статус</dt><dd id="state">...</dd>
      <dt>Действует до</dt><dd id="expires">...</dd>
    </dl>
    <button id="signBtn" type="button">Подписать через NCALayer</button>
    <div id="message" class="status"></div>
  </main>
  <script>
    const BOOTSTRAP = {bootstrap};
    const signBtn = document.getElementById("signBtn");
    const messageEl = document.getElementById("message");
    let documentPayload = null;

    function setMessage(text, cls = "") {{
      messageEl.className = "status " + cls;
      messageEl.textContent = text || "";
    }}

    function setDisabled(disabled) {{
      signBtn.disabled = disabled;
    }}

    async function loadDocument() {{
      if (BOOTSTRAP.expired) {{
        document.getElementById("state").textContent = "expired";
        setDisabled(true);
        setMessage("Срок действия ссылки истек.", "error");
        return;
      }}
      const response = await fetch(`/sign_document/api/browser/requests/${{BOOTSTRAP.requestId}}/document`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось загрузить документ");
      documentPayload = data;
      document.getElementById("filename").textContent = data.filename || "";
      document.getElementById("doctype").textContent = data.document_type || "";
      document.getElementById("state").textContent = data.status || "";
      document.getElementById("expires").textContent = data.expires_at || "";
      if (data.status === "signed") {{
        setDisabled(true);
        setMessage("Документ уже подписан.", "ok");
      }}
    }}

    function connectNCALayer() {{
      const urls = [
        "wss://127.0.0.1:13579/",
        "wss://localhost:13579/",
        "ws://127.0.0.1:13579/",
        "ws://localhost:13579/"
      ];
      return new Promise((resolve, reject) => {{
        let index = 0;
        let lastError = null;
        function tryNext() {{
          if (index >= urls.length) {{
            reject(lastError || new Error("NCALayer недоступен"));
            return;
          }}
          const ws = new WebSocket(urls[index++]);
          const timer = setTimeout(() => {{
            try {{ ws.close(); }} catch (e) {{}}
            tryNext();
          }}, 1500);
          ws.onopen = () => {{
            clearTimeout(timer);
            resolve(ws);
          }};
          ws.onerror = () => {{
            clearTimeout(timer);
            lastError = new Error("Не удалось подключиться к NCALayer");
            tryNext();
          }};
        }}
        tryNext();
      }});
    }}

    function ncaCall(ws, method, args) {{
      return new Promise((resolve, reject) => {{
        ws.onmessage = (event) => {{
          let response;
          try {{ response = JSON.parse(event.data); }} catch (e) {{ reject(e); return; }}
          if (response.code && response.code !== "200") {{
            reject(new Error(response.message || response.responseObject || "Ошибка NCALayer"));
            return;
          }}
          if (response.status && response.status !== true && response.status !== "true") {{
            reject(new Error(response.message || "Операция NCALayer не выполнена"));
            return;
          }}
          resolve(response.responseObject || response.result || response.data || response);
        }};
        ws.onerror = () => reject(new Error("Соединение с NCALayer прервано"));
        ws.send(JSON.stringify({{
          module: "kz.gov.pki.knca.commonUtils",
          method,
          args
        }}));
      }});
    }}

    async function signWithNCALayer(payload) {{
      const ws = await connectNCALayer();
      try {{
        if (payload.document_type === "xml") {{
          const signedXml = await ncaCall(ws, "signXml", [
            "PKCS12",
            "SIGNATURE",
            payload.document_text || atob(payload.document_base64),
            "",
            ""
          ]);
          return {{
            signed_document: String(signedXml),
            signed_filename: (payload.filename || "document.xml").replace(/\\.xml$/i, "") + "-signed.xml",
            signed_content_type: "application/xml"
          }};
        }}
        const cms = await ncaCall(ws, "createCAdESFromBase64", [
          "PKCS12",
          payload.document_base64,
          "SIGNATURE",
          false
        ]);
        return {{
          signed_document_base64: String(cms),
          signed_filename: (payload.filename || "document.pdf").replace(/\\.pdf$/i, "") + ".p7s",
          signed_content_type: "application/pkcs7-signature"
        }};
      }} finally {{
        try {{ ws.close(); }} catch (e) {{}}
      }}
    }}

    async function submitSigned(result) {{
      const response = await fetch(`/sign_document/api/browser/requests/${{BOOTSTRAP.requestId}}/signed`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(result)
      }});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось сохранить подпись");
      return data;
    }}

    signBtn.addEventListener("click", async () => {{
      if (!documentPayload) return;
      setDisabled(true);
      setMessage("Ожидание NCALayer...");
      try {{
        const signed = await signWithNCALayer(documentPayload);
        await submitSigned(signed);
        document.getElementById("state").textContent = "signed";
        setMessage("Документ подписан. Можно закрыть это окно.", "ok");
      }} catch (err) {{
        setDisabled(false);
        setMessage(err.message || String(err), "error");
        fetch(`/sign_document/api/browser/requests/${{BOOTSTRAP.requestId}}/error`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ error: err.message || String(err) }})
        }}).catch(() => {{}});
      }}
    }});

    loadDocument().catch((err) => {{
      setDisabled(true);
      setMessage(err.message || String(err), "error");
    }});
  </script>
</body>
</html>"""
