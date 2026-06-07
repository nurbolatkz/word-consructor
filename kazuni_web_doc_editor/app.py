from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
import base64
import io
import time
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.utils import secure_filename

from pdt_stamper.app import pdf_stamper
from word_constructor.app import fill_docx, word_constructor

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
PREVIEWS_DIR = DATA_DIR / "previews"
METADATA_PATH = DATA_DIR / "documents.json"
STATELESS_FILES_DIR = Path("/tmp/kazuni_doc_editor_stateless")

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
STATELESS_FILES_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.register_blueprint(pdf_stamper, url_prefix="/services/pdf-stamper")
app.register_blueprint(word_constructor, url_prefix="/services/word-constructor")

# flask-sock must be attached to the main app (not blueprints) for Werkzeug 3.x
from flask_sock import Sock as _Sock
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}  # keepalive ping every 25 s
_sock = _Sock(app)

@_sock.route("/services/word-constructor/api/template-builder/<session_id>/ws")
def _template_builder_ws(ws, session_id):
    from word_constructor.app import (
        _tb_ws_register, _TB_WS_TIMEOUT, _TB_DL_TIMEOUT,
        _read_meta, _is_expired, _session_template_path,
    )
    import json as _json, base64 as _b64
    meta = _read_meta(session_id)
    if meta is None or meta.get("type") != "template_builder":
        ws.close(1008, "Not found")
        return
    if _is_expired(meta):
        ws.close(1008, "Expired")
        return

    entry = _tb_ws_register(session_id)
    ws.send(_json.dumps({"type": "connected", "session_id": session_id}))

    def _build_payload():
        """Rebuild the template_ready payload from disk (used on reconnect after restart)."""
        p = {
            "type": "template_ready",
            "session_id": session_id,
            "filename": meta.get("filename", "template.docx"),
            "download_url": f"/services/word-constructor/api/template-builder/{session_id}/download",
        }
        try:
            path = _session_template_path(session_id)
            if path.exists():
                raw = path.read_bytes()
                try:
                except Exception:
                p["content_base64"] = _b64.b64encode(raw).decode("ascii")
                p["size_bytes"] = len(raw)
        except Exception:
        return p

    # Check if already ready (1C reconnected after «Отправить в 1С» was clicked)
    current_meta = _read_meta(session_id)
    if current_meta and current_meta.get("status") == "ready":
        payload = entry.get("payload") or _build_payload()
        ws.send(_json.dumps(payload))
        entry["download_event"].wait(timeout=_TB_DL_TIMEOUT)
        ws.close(1000, "Downloaded")
        return

    # Wait for the user to click «Отправить в 1С»
    if entry["event"].wait(timeout=_TB_WS_TIMEOUT):
        payload = entry.get("payload") or _build_payload()
        ws.send(_json.dumps(payload))
        entry["download_event"].wait(timeout=_TB_DL_TIMEOUT)
        ws.close(1000, "Downloaded")
    else:
        ws.send(_json.dumps({"type": "timeout", "session_id": session_id}))
        ws.close(1000, "Timeout")
_LOCK = threading.Lock()
_BRIDGE_LOCK = threading.Lock()
STATELESS_CONVERT_TIMEOUT_SECONDS = 45
STATELESS_MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024
STATELESS_FILE_TTL_SECONDS = 10 * 60
DEFAULT_DOCUMENT_TTL_SECONDS = max(int(os.environ.get("DOCUMENT_TTL_SECONDS", str(30 * 60))), 0)
ENABLE_DOCUMENT_LISTING = os.environ.get("ENABLE_DOCUMENT_LISTING", "0") == "1"
BRIDGE_TTL_SECONDS = 30 * 60
BRIDGE_WAIT_TIMEOUT_SECONDS = 30 * 60
_BRIDGE_SESSIONS: dict[str, dict[str, Any]] = {}


@dataclass
class DocumentRecord:
    id: str
    original_name: str
    stored_name: str
    uploaded_at: str
    size_bytes: int
    mime_type: str
    preview_status: str
    expires_at: str | None = None
    preview_updated_at: str | None = None
    last_error: str | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def expired_document_ids(documents: dict[str, DocumentRecord]) -> list[str]:
    now = utc_now()
    expired_ids: list[str] = []
    for document in documents.values():
        expires_at = parse_iso_datetime(document.expires_at)
        if expires_at and expires_at <= now:
            expired_ids.append(document.id)
    return expired_ids


def delete_document_artifacts(document: DocumentRecord) -> None:
    source_path = document_file_path(document)
    preview_path = preview_file_path(document)
    if source_path.exists():
        source_path.unlink()
    if preview_path.exists():
        preview_path.unlink()


def prune_expired_documents(documents: dict[str, DocumentRecord]) -> dict[str, DocumentRecord]:
    expired_ids = expired_document_ids(documents)
    if not expired_ids:
        return documents

    for document_id in expired_ids:
        document = documents.pop(document_id, None)
        if document:
            delete_document_artifacts(document)

    save_documents(documents)
    return documents


def load_documents() -> dict[str, DocumentRecord]:
    if not METADATA_PATH.exists():
        return {}

    raw = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    documents = {item["id"]: DocumentRecord(**item) for item in raw}
    return prune_expired_documents(documents)


def save_documents(documents: dict[str, DocumentRecord]) -> None:
    payload = [asdict(doc) for doc in sorted(documents.values(), key=lambda item: item.uploaded_at, reverse=True)]
    METADATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def send_file_compat(*args, **kwargs):
    kwargs.setdefault("etag", False)
    kwargs.setdefault("conditional", False)
    return send_file(*args, **kwargs)


def send_from_directory_compat(*args, **kwargs):
    kwargs.setdefault("etag", False)
    kwargs.setdefault("conditional", False)
    return send_from_directory(*args, **kwargs)


def sanitize_base64_payload(value: str) -> str:
    cleaned = (value or "").strip()
    if ";base64," in cleaned:
        cleaned = cleaned.split(";base64,", 1)[1]
    cleaned = "".join(cleaned.split())
    if not cleaned:
        raise ValueError("Base64 payload is empty")
    return cleaned


def decode_base64_payload(value: str) -> bytes:
    return base64.b64decode(sanitize_base64_payload(value), validate=True)


def requested_filename(default_name: str = "document.docx") -> str:
    if "file" in request.files:
        incoming = request.files["file"]
        cleaned = secure_filename(incoming.filename or default_name)
        if cleaned:
            return cleaned

    payload = request.get_json(silent=True) or {}
    cleaned = secure_filename(payload.get("filename") or payload.get("name") or default_name)
    if not cleaned:
        raise ValueError("Filename is invalid")
    return cleaned


def request_file_bytes() -> tuple[str, bytes]:
    if "file" in request.files:
        upload = request.files["file"]
        filename = secure_filename(upload.filename or "document.docx")
        if not filename:
            raise ValueError("Filename is invalid")
        content = upload.read()
    else:
        payload = request.get_json(silent=True) or {}
        filename = secure_filename(payload.get("filename") or payload.get("name") or "document.docx")
        if not filename:
            raise ValueError("Filename is invalid")
        content_base64 = payload.get("content_base64") or payload.get("base64") or payload.get("content")
        if not content_base64:
            raise ValueError("Missing file payload")
        content = decode_base64_payload(content_base64)

    if not content:
        raise ValueError("File is empty")
    if len(content) > STATELESS_MAX_FILE_SIZE_BYTES:
        raise ValueError(f"File is too large; limit is {STATELESS_MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB")

    return filename, content


def normalized_extension(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def prune_stateless_temp_files() -> None:
    cutoff = time.time() - STATELESS_FILE_TTL_SECONDS
    for path in STATELESS_FILES_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue


def create_stateless_temp_file(filename: str, content: bytes) -> tuple[str, Path]:
    prune_stateless_temp_files()
    token = uuid.uuid4().hex
    stored_name = f"{token}__{secure_filename(filename) or 'document.bin'}"
    path = STATELESS_FILES_DIR / stored_name
    path.write_bytes(content)
    return token, path


def stateless_temp_file_path(token: str) -> Path | None:
    prune_stateless_temp_files()
    matches = list(STATELESS_FILES_DIR.glob(f"{token}__*"))
    return matches[0] if matches else None


def delete_stateless_temp_file(token: str) -> None:
    path = stateless_temp_file_path(token)
    if path and path.exists():
        path.unlink()


def default_document_ttl_seconds() -> int | None:
    if DEFAULT_DOCUMENT_TTL_SECONDS <= 0:
        return None
    return DEFAULT_DOCUMENT_TTL_SECONDS


def stateless_convert_bytes(filename: str, content: bytes, target_format: str) -> tuple[bytes, str, str]:
    source_extension = normalized_extension(filename)
    target_extension = target_format.lower().lstrip(".")

    if source_extension not in {"doc", "docx", "pdf"}:
        raise ValueError("Supported source formats: .doc, .docx, .pdf")
    if target_extension not in {"pdf", "doc", "docx"}:
        raise ValueError("Supported target formats: pdf, doc, docx")
    if source_extension in {"doc", "docx"} and target_extension != "pdf":
        raise ValueError("DOC/DOCX can only be converted to PDF")
    if source_extension == "pdf" and target_extension not in {"doc", "docx"}:
        raise ValueError("PDF can only be converted to DOC or DOCX")

    token, _ = create_stateless_temp_file(filename, content)
    payload = {
        "async": False,
        "filetype": source_extension,
        "key": uuid.uuid4().hex,
        "outputtype": target_extension,
        "title": secure_filename(filename) or f"input.{source_extension}",
        "url": build_internal_stateless_file_url(token),
    }
    jwt_token = jwt.encode(payload, onlyoffice_jwt_secret(), algorithm="HS256")

    try:
        try:
            result = post_json(
                onlyoffice_convert_url(),
                {**payload, "token": jwt_token},
                timeout=STATELESS_CONVERT_TIMEOUT_SECONDS,
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
        except TimeoutError as exc:
            raise TimeoutError(f"Conversion exceeded {STATELESS_CONVERT_TIMEOUT_SECONDS} seconds") from exc
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(body or f"OnlyOffice HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"OnlyOffice service unavailable: {exc.reason}") from exc

        if result.get("error"):
            raise RuntimeError(result.get("message") or str(result.get("error")))
        if not result.get("endConvert") or not result.get("fileUrl"):
            percent = result.get("percent")
            raise RuntimeError(f"OnlyOffice conversion did not finish{f' ({percent}%)' if percent is not None else ''}")

        converted_url, host_header = normalize_callback_download_url(result["fileUrl"])
        converted_bytes = download_remote_file(converted_url, host_header=host_header)
        output_name = f"{Path(filename).stem}.{target_extension}"
        mime_type = mimetypes.guess_type(output_name)[0] or "application/octet-stream"
        return converted_bytes, output_name, mime_type
    finally:
        delete_stateless_temp_file(token)


def create_document_from_bytes(filename: str, content: bytes, *, expires_in_seconds: int | None = None) -> DocumentRecord:
    original_name = secure_filename(filename or "document.docx")
    if not original_name:
        raise ValueError("Filename is invalid")

    document_id = uuid.uuid4().hex
    stored_name = f"{document_id}_{original_name}"
    destination = UPLOADS_DIR / stored_name
    destination.write_bytes(content)

    document = DocumentRecord(
        id=document_id,
        original_name=original_name,
        stored_name=stored_name,
        uploaded_at=utc_now_iso(),
        size_bytes=destination.stat().st_size,
        mime_type=detect_mime_type(destination),
        preview_status="pending",
        expires_at=(utc_now() + timedelta(seconds=expires_in_seconds)).isoformat() if expires_in_seconds else None,
    )

    with _LOCK:
        documents = load_documents()
        documents[document.id] = document
        save_documents(documents)

    return document


def get_document_or_404(document_id: str) -> DocumentRecord:
    documents = load_documents()
    document = documents.get(document_id)
    if not document:
        abort(404, description="Document not found")
    expires_at = parse_iso_datetime(document.expires_at)
    if expires_at and expires_at <= utc_now():
        delete_document_artifacts(document)
        with _LOCK:
            documents = load_documents()
            documents.pop(document_id, None)
            save_documents(documents)
        abort(404, description="Document expired")
    return document


def update_document(document: DocumentRecord) -> None:
    with _LOCK:
        documents = load_documents()
        documents[document.id] = document
        save_documents(documents)


def document_file_path(document: DocumentRecord) -> Path:
    return UPLOADS_DIR / document.stored_name


def preview_file_path(document: DocumentRecord) -> Path:
    return PREVIEWS_DIR / f"{document.id}.pdf"


def libreoffice_binary() -> str:
    configured = os.environ.get("LIBREOFFICE_BIN")
    if configured:
        return configured

    for candidate in ("libreoffice", "soffice"):
        if shutil.which(candidate):
            return candidate

    raise RuntimeError("LibreOffice is not installed or not available in PATH")


def libreoffice_gui_available() -> bool:
    if os.environ.get("ENABLE_LIBREOFFICE_GUI") == "1":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def onlyoffice_internal_base_url() -> str:
    return os.environ.get("ONLYOFFICE_INTERNAL_BASE_URL", "http://host.docker.internal:8016").rstrip("/")


def onlyoffice_service_base_url() -> str:
    return os.environ.get("ONLYOFFICE_SERVICE_BASE_URL", "http://127.0.0.1:8020").rstrip("/")


def onlyoffice_jwt_secret() -> str:
    return os.environ.get("ONLYOFFICE_JWT_SECRET", "kazuni-onlyoffice-secret")


def document_version_key(document: DocumentRecord) -> str:
    path = document_file_path(document)
    stat = path.stat()
    return f"{document.id}-{int(stat.st_mtime)}-{stat.st_size}"


def clear_preview(document: DocumentRecord) -> None:
    preview_path = preview_file_path(document)
    if preview_path.exists():
        preview_path.unlink()


def build_internal_document_url(document_id: str, suffix: str) -> str:
    return f"{onlyoffice_internal_base_url()}/api/documents/{document_id}/{suffix.lstrip('/')}"


def onlyoffice_document_type(extension: str) -> str:
    word_types = {"doc", "docx", "odt", "rtf", "txt", "html", "htm"}
    cell_types = {"xls", "xlsx", "ods", "csv"}
    slide_types = {"ppt", "pptx", "odp"}

    if extension in cell_types:
        return "cell"
    if extension in slide_types:
        return "slide"
    if extension in word_types:
        return "word"
    return "word"


def download_remote_file(url: str, host_header: str | None = None) -> bytes:
    request = Request(url, headers={"User-Agent": "kazuni-doc-editor/1.0"})
    if host_header:
        request.add_header("Host", host_header)

    with urlopen(request, timeout=60) as response:
        return response.read()


def normalize_callback_download_url(url: str) -> tuple[str, str | None]:
    parsed = urlparse(url)
    if parsed.hostname in {"127.0.0.1", "localhost"}:
        return url, None

    rewritten = parsed._replace(
        scheme="http",
        netloc="127.0.0.1" if parsed.port is None else f"127.0.0.1:{parsed.port}",
    )
    return urlunparse(rewritten), parsed.netloc


def onlyoffice_convert_url() -> str:
    return f"{onlyoffice_service_base_url()}/ConvertService.ashx"


def build_internal_stateless_file_url(token: str) -> str:
    return f"{onlyoffice_internal_base_url()}/api/stateless-files/{token}"


def onlyoffice_command_urls(key: str) -> list[str]:
    base = onlyoffice_service_base_url()
    return [
        f"{base}/command?shardkey={key}",
        f"{base}/coauthoring/CommandService.ashx",
    ]


def post_json(url: str, payload: dict[str, Any], *, timeout: int, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "kazuni-doc-editor/1.0",
    }
    if headers:
        request_headers.update(headers)

    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers=request_headers, method="POST")
    with urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw or "{}")


def onlyoffice_force_save_document(document: DocumentRecord) -> dict[str, Any]:
    key = document_version_key(document)
    token = jwt.encode({"c": "forcesave", "key": key}, onlyoffice_jwt_secret(), algorithm="HS256")
    payload = {"token": token}
    last_error: Exception | None = None

    for url in onlyoffice_command_urls(key):
        try:
            return post_json(url, payload, timeout=20)
        except (TimeoutError, HTTPError, URLError, ValueError) as exc:
            last_error = exc
            continue

    raise RuntimeError(f"Cannot reach ONLYOFFICE command service: {last_error}")


def wait_for_document_write(document: DocumentRecord, previous_mtime_ns: int | None, timeout_seconds: float = 10.0) -> bool:
    path = document_file_path(document)
    deadline = time.time() + timeout_seconds
    baseline = int(previous_mtime_ns or 0)

    while time.time() < deadline:
      if path.exists():
          try:
              if path.stat().st_mtime_ns > baseline:
                  return True
          except FileNotFoundError:
              pass
      time.sleep(0.25)
    return False


def prune_bridge_sessions() -> None:
    now = time.time()
    with _BRIDGE_LOCK:
        stale_ids = [
            bridge_id
            for bridge_id, bridge in _BRIDGE_SESSIONS.items()
            if now > float(bridge.get("expires_at_ts", 0))
        ]
        for bridge_id in stale_ids:
            _BRIDGE_SESSIONS.pop(bridge_id, None)


def create_bridge_session(document: DocumentRecord) -> dict[str, Any]:
    prune_bridge_sessions()
    bridge_id = uuid.uuid4().hex
    bridge = {
        "id": bridge_id,
        "document_id": document.id,
        "event": threading.Event(),
        "created_at_ts": time.time(),
        "expires_at_ts": time.time() + BRIDGE_TTL_SECONDS,
        "message": None,
    }
    with _BRIDGE_LOCK:
        _BRIDGE_SESSIONS[bridge_id] = bridge
    return bridge


def get_bridge_session(bridge_id: str) -> dict[str, Any] | None:
    prune_bridge_sessions()
    with _BRIDGE_LOCK:
        bridge = _BRIDGE_SESSIONS.get(bridge_id)
    if not bridge:
        return None
    if time.time() > float(bridge.get("expires_at_ts", 0)):
        with _BRIDGE_LOCK:
            _BRIDGE_SESSIONS.pop(bridge_id, None)
        return None
    return bridge


def complete_bridge_session(bridge_id: str, payload: dict[str, Any]) -> None:
    bridge = get_bridge_session(bridge_id)
    if not bridge:
        return
    bridge["message"] = payload
    bridge["event"].set()


def generate_preview(document: DocumentRecord) -> Path:
    source_path = document_file_path(document)
    if not source_path.exists():
        raise FileNotFoundError("Uploaded file is missing")

    target_path = preview_file_path(document)

    if source_path.suffix.lower() == ".pdf":
        shutil.copyfile(source_path, target_path)
        document.preview_status = "ready"
        document.preview_updated_at = utc_now_iso()
        document.last_error = None
        update_document(document)
        return target_path

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        temp_source = temp_path / source_path.name
        shutil.copy2(source_path, temp_source)

        cmd = [
            libreoffice_binary(),
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(temp_path),
            str(temp_source),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        converted_path = temp_path / f"{temp_source.stem}.pdf"

        if completed.returncode != 0 or not converted_path.exists():
            stderr = completed.stderr.strip() or completed.stdout.strip() or "Unknown LibreOffice conversion error"
            raise RuntimeError(stderr)

        shutil.copyfile(converted_path, target_path)

    document.preview_status = "ready"
    document.preview_updated_at = utc_now_iso()
    document.last_error = None
    update_document(document)
    return target_path


def serialize_document(document: DocumentRecord) -> dict[str, Any]:
    source_path = document_file_path(document)
    preview_path = preview_file_path(document)
    return {
        "id": document.id,
        "original_name": document.original_name,
        "uploaded_at": document.uploaded_at,
        "size_bytes": document.size_bytes,
        "mime_type": document.mime_type,
        "preview_status": document.preview_status,
        "preview_updated_at": document.preview_updated_at,
        "last_error": document.last_error,
        "expires_at": document.expires_at,
        "can_open_in_libreoffice": libreoffice_gui_available(),
        "browser_edit_url": f"/documents/{document.id}/edit",
        "embedded_edit_url": f"/documents/{document.id}/embedded",
        "embed_preview_url": f"/embed/preview/{document.id}",
        "download_url": f"/api/documents/{document.id}/download",
        "detail_url": f"/api/documents/{document.id}",
        "preview_url": f"/documents/{document.id}/embedded" if preview_path.exists() or source_path.exists() else None,
        "pdf_preview_url": f"/api/documents/{document.id}/preview" if preview_path.exists() or source_path.exists() else None,
    }


@app.route("/")
def index() -> str:
    services = [
        {
            "title": "Doc Editor",
            "summary": "Browser-based editing with document upload, OnlyOffice session, and PDF preview in one workspace.",
            "href": "/services/doc-editor",
            "badge": "Editor",
        },
        {
            "title": "Auto PDF Stamper",
            "summary": "Upload a PDF, place a stamp image on selected pages, and download the stamped result.",
            "href": "/services/pdf-stamper",
            "badge": "PDF",
        },
        {
            "title": "Конструктор приказов",
            "summary": "1С передаёт шаблон Word с плейсхолдерами и параметры. Пользователь видит автозаполненный документ, редактирует перетаскиванием и скачивает готовый файл.",
            "href": "/services/word-constructor",
            "badge": "Word",
        },
    ]
    return render_template("home.html", services=services)


@app.route("/services/doc-editor")
def doc_editor_home() -> str:
    return render_template("index.html", documents=[])


@app.route("/embed/preview", methods=["GET"], strict_slashes=False)
def embedded_preview():
    return render_template("preview_embed.html")


@app.route("/embed/preview/<document_id>", methods=["GET"], strict_slashes=False)
def embedded_preview_document(document_id: str):
    document = get_document_or_404(document_id)
    return render_template("embedded_document.html", document=serialize_document(document))


@app.route("/documents/<document_id>/embedded", methods=["GET"])
def embedded_document(document_id: str):
    document = get_document_or_404(document_id)
    return render_template("embedded_document.html", document=serialize_document(document))


@app.route("/health", methods=["GET"])
def healthcheck():
    return jsonify({"status": "ok", "libreoffice_gui_available": libreoffice_gui_available()})


@app.route("/api/stateless-files/<token>", methods=["GET"])
def stateless_file_download(token: str):
    path = stateless_temp_file_path(token)
    if not path or not path.exists():
        abort(404, description="Temporary file not found")

    original_name = path.name.split("__", 1)[1] if "__" in path.name else path.name
    return send_file_compat(
        path,
        as_attachment=False,
        download_name=original_name,
        mimetype=detect_mime_type(path),
        max_age=0,
    )


@app.route("/api/documents", methods=["GET"])
def list_documents():
    if not ENABLE_DOCUMENT_LISTING:
        return jsonify([])
    documents = load_documents()
    return jsonify([serialize_document(item) for item in documents.values()])


@app.route("/api/documents", methods=["POST"])
def upload_document():
    if "file" not in request.files:
        return jsonify({"error": "No file field in request"}), 400

    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"error": "Empty filename"}), 400

    original_name = secure_filename(upload.filename)
    if not original_name:
        return jsonify({"error": "Filename is invalid"}), 400

    content = upload.read()
    document = create_document_from_bytes(
        original_name,
        content,
        expires_in_seconds=default_document_ttl_seconds(),
    )

    return jsonify(serialize_document(document)), 201


@app.route("/api/embedded-preview", methods=["POST"])
def embedded_preview_upload():
    payload = request.get_json(silent=True) or {}
    filename = payload.get("filename") or payload.get("name") or "document.docx"
    content_base64 = payload.get("content_base64") or payload.get("base64") or payload.get("content")

    if not content_base64:
        return jsonify({"error": "Missing content_base64"}), 400

    try:
        content = base64.b64decode(sanitize_base64_payload(content_base64), validate=True)
    except Exception:
        return jsonify({"error": "Invalid base64 payload"}), 400

    try:
        document = create_document_from_bytes(
            filename,
            content,
            expires_in_seconds=default_document_ttl_seconds(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return (
        jsonify(
            {
                "document_id": document.id,
                "preview_url": f"/documents/{document.id}/embedded",
                "pdf_preview_url": f"/api/documents/{document.id}/preview",
                "detail_url": f"/api/documents/{document.id}",
            }
        ),
        201,
    )


@app.route("/api/embedded-editor", methods=["POST"])
def embedded_editor_upload():
    payload = request.get_json(silent=True) or {}
    filename = payload.get("filename") or payload.get("name") or "document.docx"
    content_base64 = payload.get("content_base64") or payload.get("base64") or payload.get("content")

    if not content_base64:
        return jsonify({"error": "Missing content_base64"}), 400

    try:
        content = base64.b64decode(sanitize_base64_payload(content_base64), validate=True)
    except Exception:
        return jsonify({"error": "Invalid base64 payload"}), 400

    try:
        document = create_document_from_bytes(
            filename,
            content,
            expires_in_seconds=default_document_ttl_seconds(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return (
        jsonify(
            {
                "document_id": document.id,
                "edit_url": f"/documents/{document.id}/edit?embed=1",
                "preview_url": f"/documents/{document.id}/embedded",
                "detail_url": f"/api/documents/{document.id}",
            }
        ),
        201,
    )


@app.route("/api/1c/documents", methods=["POST"])
@app.route("/services/doc-editor/api/1c/documents", methods=["POST"])
@app.route("/services/word-constructor/api/1c/documents", methods=["POST"])
def upload_document_for_1c():
    payload = request.get_json(silent=True) or {}
    filename = payload.get("filename") or payload.get("name") or "document.docx"
    content_base64 = payload.get("content_base64") or payload.get("base64") or payload.get("content")

    if not content_base64:
        return jsonify({"error": "Missing content_base64"}), 400

    try:
        content = base64.b64decode(sanitize_base64_payload(content_base64), validate=True)
    except Exception:
        return jsonify({"error": "Invalid base64 payload"}), 400

    try:
        document = create_document_from_bytes(filename, content, expires_in_seconds=30 * 60)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return (
        jsonify(
            {
                "document_id": document.id,
                "expires_at": document.expires_at,
                "embed_preview_url": f"/embed/preview/{document.id}",
                "edit_url": f"/documents/{document.id}/edit?embed=1",
                "detail_url": f"/api/documents/{document.id}",
            }
        ),
        201,
    )


@app.route("/api/1c/documents/bridge", methods=["POST"])
@app.route("/services/doc-editor/api/1c/documents/bridge", methods=["POST"])
@app.route("/services/word-constructor/api/1c/documents/bridge", methods=["POST"])
@app.route("/services/word-constructor/api/bridge/create", methods=["POST"])
def upload_document_for_1c_bridge():
    filename = "document.docx"
    content: bytes | None = None

    def parse_fill_params(raw_params: Any) -> tuple[dict[str, str], dict[str, list]]:
        if not isinstance(raw_params, dict):
            raise ValueError("Field 'params' must be a JSON object")

        string_params: dict[str, str] = {}
        table_params: dict[str, list] = {}
        for key, value in raw_params.items():
            if isinstance(value, list):
                table_params[str(key)] = [
                    [str(cell) for cell in row]
                    for row in value
                    if isinstance(row, list)
                ]
            else:
                string_params[str(key)] = str(value)
        return string_params, table_params

    if request.files:
        template_file = request.files.get("template") or request.files.get("document")
        if not template_file:
            return jsonify({"error": "Missing 'template' or 'document' file field"}), 400

        template_bytes = template_file.read()
        if not template_bytes:
            return jsonify({"error": "Uploaded file is empty"}), 400

        filename = template_file.filename or filename

        raw_params = request.form.get("params", "").strip()
        if raw_params:
            try:
                parsed = json.loads(raw_params)
            except json.JSONDecodeError as exc:
                return jsonify({"error": f"Field 'params' is not valid JSON: {exc}"}), 400

            try:
                string_params, table_params = parse_fill_params(parsed)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

            try:
                content = fill_docx(template_bytes, string_params, table_params)
            except Exception as exc:
                return jsonify({"error": f"Cannot fill DOCX template: {exc}"}), 400
        else:
            content = template_bytes
    else:
        payload = request.get_json(silent=True) or {}
        filename = payload.get("filename") or payload.get("name") or filename
        content_base64 = payload.get("content_base64") or payload.get("base64") or payload.get("content")
        parsed_params = payload.get("params")

        if not content_base64:
            return jsonify({"error": "Missing content_base64"}), 400

        try:
            content = base64.b64decode(sanitize_base64_payload(content_base64), validate=True)
        except Exception:
            return jsonify({"error": "Invalid base64 payload"}), 400

        if parsed_params is not None:
            try:
                string_params, table_params = parse_fill_params(parsed_params)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            try:
                content = fill_docx(content, string_params, table_params)
            except Exception as exc:
                return jsonify({"error": f"Cannot fill DOCX template: {exc}"}), 400

    try:
        document = create_document_from_bytes(filename, content, expires_in_seconds=BRIDGE_TTL_SECONDS)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    bridge = create_bridge_session(document)
    scheme = "wss" if request.headers.get("X-Forwarded-Proto", request.scheme) == "https" else "ws"
    host = request.host

    return (
        jsonify(
            {
                "document_id": document.id,
                "bridge_id": bridge["id"],
                "expires_at": document.expires_at,
                "browser_edit_url": f"/documents/{document.id}/edit?bridge_id={bridge['id']}",
                "websocket_url": f"{scheme}://{host}/services/word-constructor/bridge/ws/{bridge['id']}",
                "download_url": f"/api/documents/{document.id}/download",
            }
        ),
        201,
    )


@app.route("/api/convert", methods=["POST"])
def convert_document_stateless():
    target_format = (request.args.get("target") or request.form.get("target") or (request.get_json(silent=True) or {}).get("target_format") or "").strip().lower()
    if not target_format:
        return jsonify({"error": "Missing target format", "supported_targets": ["pdf", "doc", "docx"]}), 400

    try:
        filename, content = request_file_bytes()
        converted_bytes, output_name, mime_type = stateless_convert_bytes(filename, content, target_format)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except TimeoutError as exc:
        return jsonify({"error": "Conversion timeout", "details": str(exc)}), 504
    except RuntimeError as exc:
        return jsonify({"error": "Conversion failed", "details": str(exc)}), 422

    return send_file_compat(
        io.BytesIO(converted_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=output_name,
    )


@app.route("/api/convert/doc-to-pdf", methods=["POST"])
def convert_doc_to_pdf():
    try:
        filename, content = request_file_bytes()
        converted_bytes, output_name, mime_type = stateless_convert_bytes(filename, content, "pdf")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except TimeoutError as exc:
        return jsonify({"error": "Conversion timeout", "details": str(exc)}), 504
    except RuntimeError as exc:
        return jsonify({"error": "Conversion failed", "details": str(exc)}), 422

    return send_file_compat(
        io.BytesIO(converted_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=output_name,
    )


@app.route("/api/convert/pdf-to-docx", methods=["POST"])
def convert_pdf_to_docx():
    try:
        filename, content = request_file_bytes()
        converted_bytes, output_name, mime_type = stateless_convert_bytes(filename, content, "docx")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except TimeoutError as exc:
        return jsonify({"error": "Conversion timeout", "details": str(exc)}), 504
    except RuntimeError as exc:
        return jsonify({"error": "Conversion failed", "details": str(exc)}), 422

    return send_file_compat(
        io.BytesIO(converted_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=output_name,
    )


@app.route("/api/convert/pdf-to-doc", methods=["POST"])
def convert_pdf_to_doc():
    try:
        filename, content = request_file_bytes()
        converted_bytes, output_name, mime_type = stateless_convert_bytes(filename, content, "doc")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except TimeoutError as exc:
        return jsonify({"error": "Conversion timeout", "details": str(exc)}), 504
    except RuntimeError as exc:
        return jsonify({"error": "Conversion failed", "details": str(exc)}), 422

    return send_file_compat(
        io.BytesIO(converted_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=output_name,
    )


@app.route("/api/documents/<document_id>", methods=["GET"])
def get_document(document_id: str):
    document = get_document_or_404(document_id)
    return jsonify(serialize_document(document))


@app.route("/api/documents/<document_id>/download", methods=["GET"])
def download_document(document_id: str):
    document = get_document_or_404(document_id)
    path = document_file_path(document)
    if not path.exists():
        abort(404, description="Stored file not found")

    return send_file_compat(path, as_attachment=True, download_name=document.original_name)


@app.route("/api/documents/<document_id>/onlyoffice/file", methods=["GET"])
def onlyoffice_file(document_id: str):
    document = get_document_or_404(document_id)
    path = document_file_path(document)
    if not path.exists():
        abort(404, description="Stored file not found")

    return send_file_compat(path, as_attachment=False, download_name=document.original_name, mimetype=document.mime_type)


@app.route("/api/documents/<document_id>/onlyoffice/config", methods=["GET"])
def onlyoffice_config(document_id: str):
    document = get_document_or_404(document_id)
    source_path = document_file_path(document)
    if not source_path.exists():
        abort(404, description="Stored file not found")

    extension = source_path.suffix.lower().lstrip(".")
    file_url = build_internal_document_url(document.id, "onlyoffice/file")
    callback_url = build_internal_document_url(document.id, "onlyoffice/callback")

    payload = {
        "document": {
            "fileType": extension,
            "key": document_version_key(document),
            "title": document.original_name,
            "url": file_url,
        },
        "documentType": onlyoffice_document_type(extension),
        "editorConfig": {
            "callbackUrl": callback_url,
            "lang": "ru",
            "customization": {
                "autosave": True,
                "compactHeader": False,
                "forcesave": True,
                "uiTheme": "theme-white",
            },
            "mode": "edit",
            "user": {
                "id": "guest",
                "name": "Guest",
            },
        },
        "height": "100%",
        "type": "desktop",
        "width": "100%",
    }
    payload["token"] = jwt.encode(payload, onlyoffice_jwt_secret(), algorithm="HS256")
    return jsonify(payload)


@app.route("/api/documents/<document_id>/onlyoffice/callback", methods=["POST"])
def onlyoffice_callback(document_id: str):
    document = get_document_or_404(document_id)
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")

    if status not in {2, 3, 6, 7}:
        return jsonify({"error": 0})

    if status in {2, 6}:
        download_url = payload.get("url")
        if not download_url:
            return jsonify({"error": 1, "message": "Missing file URL"})

        normalized_url, host_header = normalize_callback_download_url(download_url)
        content = download_remote_file(normalized_url, host_header=host_header)
        destination = document_file_path(document)
        destination.write_bytes(content)

        document.size_bytes = destination.stat().st_size
        document.mime_type = detect_mime_type(destination)
        document.preview_status = "pending"
        document.preview_updated_at = None
        document.last_error = None
        clear_preview(document)
        update_document(document)

    return jsonify({"error": 0})


@app.route("/api/1c/bridge/<bridge_id>/complete", methods=["POST"])
@app.route("/services/doc-editor/api/1c/bridge/<bridge_id>/complete", methods=["POST"])
@app.route("/services/word-constructor/api/1c/bridge/<bridge_id>/complete", methods=["POST"])
@app.route("/services/word-constructor/api/bridge/<bridge_id>/complete", methods=["POST"])
def complete_1c_bridge(bridge_id: str):
    bridge = get_bridge_session(bridge_id)
    if not bridge:
        return jsonify({"error": "Bridge session not found or expired"}), 404

    document = get_document_or_404(str(bridge["document_id"]))
    path = document_file_path(document)
    previous_mtime_ns = path.stat().st_mtime_ns if path.exists() else None

    try:
        result = onlyoffice_force_save_document(document)
    except Exception as exc:
        return jsonify({"error": f"Force save failed: {exc}"}), 502

    error_code = int(result.get("error", 0) or 0)
    if error_code not in {0, 4}:
        return jsonify({"error": "ONLYOFFICE rejected force save", "details": result}), 502

    if error_code == 0:
        saved = wait_for_document_write(document, previous_mtime_ns, timeout_seconds=12.0)
        if not saved:
            return jsonify({"error": "Timed out waiting for saved document"}), 504

    content = document_file_path(document).read_bytes()
    payload = {
        "type": "document_ready",
        "bridge_id": bridge_id,
        "document_id": document.id,
        "filename": document.original_name,
        "download_url": f"/api/documents/{document.id}/download",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "size_bytes": len(content),
    }
    complete_bridge_session(bridge_id, payload)
    return jsonify({"ok": True, "result": result, "size_bytes": len(content)})


@app.route("/ws/1c-bridge/<bridge_id>", methods=["GET"])
@app.route("/services/word-constructor/ws/1c-bridge/<bridge_id>", methods=["GET"])
@app.route("/services/word-constructor/bridge/ws/<bridge_id>", methods=["GET"])
def websocket_1c_bridge(bridge_id: str):
    bridge = get_bridge_session(bridge_id)
    if not bridge:
        abort(404, description="Bridge session not found or expired")

    try:
        from simple_websocket import ConnectionClosed, Server
    except Exception:
        abort(500, description="simple-websocket is not installed")

    ws = Server.accept(request.environ)
    try:
        ws.send(json.dumps({"type": "bridge_connected", "bridge_id": bridge_id}))
        if not bridge["event"].wait(timeout=BRIDGE_WAIT_TIMEOUT_SECONDS):
            ws.send(json.dumps({"type": "bridge_timeout", "bridge_id": bridge_id}))
            return ""

        message = bridge.get("message") or {"type": "bridge_error", "error": "No payload"}
        ws.send(json.dumps(message))
        return ""
    except ConnectionClosed:
        return ""
    finally:
        try:
            ws.close()
        except Exception:


@app.route("/documents/<document_id>/edit", methods=["GET"])
def browser_edit(document_id: str):
    document = get_document_or_404(document_id)
    embed_mode = request.args.get("embed") == "1"
    return render_template("editor.html", document=serialize_document(document), embed_mode=embed_mode)


@app.route("/api/documents/<document_id>/preview", methods=["GET"])
def preview_document(document_id: str):
    document = get_document_or_404(document_id)
    target_path = preview_file_path(document)

    try:
        if not target_path.exists():
            generate_preview(document)
    except Exception as exc:
        document.preview_status = "error"
        document.last_error = str(exc)
        update_document(document)
        return jsonify({"error": "Preview generation failed", "details": str(exc)}), 500

    return send_file_compat(target_path, mimetype="application/pdf", download_name=f"{Path(document.original_name).stem}.pdf")


@app.route("/api/documents/<document_id>/refresh", methods=["POST"])
def refresh_preview(document_id: str):
    document = get_document_or_404(document_id)

    try:
        generate_preview(document)
    except Exception as exc:
        document.preview_status = "error"
        document.last_error = str(exc)
        update_document(document)
        return jsonify({"error": "Preview refresh failed", "details": str(exc)}), 500

    return jsonify(serialize_document(document))


@app.route("/api/documents/<document_id>/open-in-libreoffice", methods=["POST"])
def open_in_libreoffice(document_id: str):
    document = get_document_or_404(document_id)
    source_path = document_file_path(document)

    if not source_path.exists():
        abort(404, description="Stored file not found")

    if not libreoffice_gui_available():
        return (
            jsonify(
                {
                    "error": "LibreOffice GUI is not available",
                    "details": "This deployment is running on a headless server, so it can generate previews but cannot open a visible LibreOffice window.",
                }
            ),
            409,
        )

    cmd = [libreoffice_binary(), str(source_path)]
    completed = subprocess.Popen(cmd)

    return jsonify(
        {
            "status": "opened",
            "document_id": document.id,
            "pid": completed.pid,
            "path": str(source_path),
        }
    )


@app.route("/uploads/<path:filename>", methods=["GET"])
def serve_upload(filename: str):
    return send_from_directory_compat(UPLOADS_DIR, filename)


@app.route("/services/word-constructor/api/1c/converter/word-base64-to-pdf/", methods=["POST"])
def word_base64_to_pdf():
    """Convert a base64-encoded DOCX to PDF and return raw PDF bytes.

    Request JSON:
        filename      – original file name (used only for extension check)
        content_base64 – standard or URL-safe base64 of the DOCX file
    Response:
        200 application/pdf – raw PDF bytes
        400 – bad request (missing fields, bad base64, wrong extension)
        500 – conversion failed
    """
    body = request.get_json(force=True, silent=True) or {}
    filename = body.get("filename", "")
    content_b64 = body.get("content_base64", "")

    if not content_b64:
        return jsonify({"error": "content_base64 is required"}), 400

    # Normalise base64 (same tolerances as 1C helper)
    b64 = "".join(content_b64.split())
    b64 = b64.replace("-", "+").replace("_", "/")
    b64 += "=" * (-len(b64) % 4)
    try:
        docx_bytes = base64.b64decode(b64)
    except Exception as exc:
        return jsonify({"error": f"Invalid base64: {exc}"}), 400

    safe_name = secure_filename(filename) if filename else "document.docx"
    if not safe_name:
        safe_name = "document.docx"
    if Path(safe_name).suffix.lower() not in (".docx", ".doc", ".odt", ".rtf"):
        safe_name = Path(safe_name).stem + ".docx"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / safe_name
        src.write_bytes(docx_bytes)

        cmd = [
            libreoffice_binary(),
            "--headless",
            "--convert-to", "pdf",
            "--outdir", str(tmp_path),
            str(src),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=STATELESS_CONVERT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Conversion timed out"}), 500

        pdf_path = tmp_path / (src.stem + ".pdf")
        if result.returncode != 0 or not pdf_path.exists():
            err = (result.stderr or result.stdout or "Unknown LibreOffice error").strip()
            return jsonify({"error": err}), 500

        pdf_bytes = pdf_path.read_bytes()

    return app.response_class(
        response=pdf_bytes,
        status=200,
        mimetype="application/pdf",
    )


@app.errorhandler(404)
def handle_not_found(error):
    return jsonify({"error": str(error)}), 404


@app.errorhandler(500)
def handle_server_error(error):
    return jsonify({"error": "Internal server error", "details": str(error)}), 500


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
