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
                p["content_base64"] = _b64.b64encode(raw).decode("ascii")
                p["size_bytes"] = len(raw)
        except Exception:
            pass
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


@app.route("/api/convert/pdf-merge", methods=["POST"])
def pdf_merge():
    """Merge two PDF documents into one.

    Accepts JSON body:
        {
            "filename":  "result.pdf",          // optional output name
            "files": [
                {"filename": "first.pdf",  "content_base64": "<base64>"},
                {"filename": "second.pdf", "content_base64": "<base64>"}
            ]
        }

    Returns: 200 application/pdf – merged PDF bytes
    """
    try:
        from pypdf import PdfWriter, PdfReader
    except ImportError:
        return jsonify({"error": "pypdf library not installed"}), 500

    raw_body = request.get_data(cache=True)
    body = request.get_json(force=True, silent=True) or {}
    files = body.get("files")
    if not files or not isinstance(files, list) or len(files) < 2:
        app.logger.warning(
            "[pdf-merge] 400 bad request: files=%r body_bytes=%d keys=%s",
            type(files).__name__, len(raw_body), list(body.keys()),
        )
        return jsonify({"error": "Provide at least 2 files in the 'files' array"}), 400

    output_name = body.get("filename") or "merged.pdf"
    if not output_name.lower().endswith(".pdf"):
        output_name = Path(output_name).stem + ".pdf"

    writer = PdfWriter()
    for i, entry in enumerate(files):
        b64 = entry.get("content_base64") or ""
        if not b64:
            app.logger.warning(
                "[pdf-merge] 400 files[%d].content_base64 empty/null: "
                "entry_keys=%s b64_type=%s body_bytes=%d file_count=%d",
                i, list(entry.keys()), type(entry.get("content_base64")).__name__,
                len(raw_body), len(files),
            )
            return jsonify({"error": f"files[{i}].content_base64 is required"}), 400
        # Normalise base64 (handle URL-safe variant and whitespace)
        b64 = "".join(b64.split()).replace("-", "+").replace("_", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            pdf_bytes = base64.b64decode(b64)
        except Exception as exc:
            app.logger.warning("[pdf-merge] 400 files[%d] bad base64: %s", i, exc)
            return jsonify({"error": f"files[{i}]: invalid base64 – {exc}"}), 400
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for page in reader.pages:
                writer.add_page(page)
        except Exception as exc:
            name = entry.get("filename") or f"file[{i}]"
            app.logger.warning("[pdf-merge] 422 could not read PDF '%s': %s", name, exc)
            return jsonify({"error": f"Could not read PDF '{name}': {exc}"}), 422

    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)

    return send_file_compat(
        buf,
        mimetype="application/pdf",
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
    _scheme = "https" if request.headers.get("X-Forwarded-Proto", request.scheme) == "https" else "http"
    _ai_plugin_url = f"{_scheme}://{request.host}/api/ai-plugin/config.json"
    payload["editorConfig"]["plugins"] = {
        "pluginsData": [_ai_plugin_url],
        "autostart":   [_AI_PLUGIN_GUID],
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
            pass


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


# ═══════════════════════════════════════════════════════════════════════════
# AI Batch Document Processor  —  POST /api/ai/apply-prompts
# ═══════════════════════════════════════════════════════════════════════════

def _para_get_text(para, TAG_R, TAG_T):
    """
    Concatenate ALL run texts in a paragraph, including runs nested inside
    w:ins, w:hyperlink, w:del, or any other wrapper element.
    Using iter() instead of direct children loop prevents missing nested runs
    (e.g. 1C-substituted values wrapped in w:ins tracked-change elements).
    """
    return "".join(
        (t.text or "")
        for r in para.iter(TAG_R)
        for t in r
        if t.tag == TAG_T
    )


def _para_set_text(para, new_text, TAG_R, TAG_T, TAG_RPR, PRESERVE):
    """
    Replace paragraph text with new_text using the first-run strategy:
    - Put the entire corrected text into the FIRST run found anywhere in the
      paragraph (including inside w:ins / w:hyperlink wrappers).
    - Clear (empty) w:t text in all subsequent runs — do NOT remove runs,
      so their w:rPr formatting elements stay in place.

    This is intentional: we lose per-word italic/underline but guarantee
    word order is preserved. The previous approach of removing runs left
    nested runs (from 1C tracked-change insertions) untouched, causing
    their text to appear appended at the end of the paragraph.
    """
    from lxml import etree as _ET

    all_runs = list(para.iter(TAG_R))

    if not all_runs:
        # Paragraph has no runs at all — create a bare one as direct child
        r = _ET.SubElement(para, TAG_R)
        t_el = _ET.SubElement(r, TAG_T)
        t_el.text = new_text or ""
        if new_text and (new_text[0] == " " or new_text[-1] == " "):
            t_el.set(PRESERVE, "preserve")
        return

    # ── Write full corrected text into the first run ──────────────────────
    first = all_runs[0]
    t_el  = first.find(TAG_T)
    if t_el is None:
        t_el = _ET.SubElement(first, TAG_T)
    t_el.text = new_text or ""
    if new_text and (new_text[0] == " " or new_text[-1] == " "):
        t_el.set(PRESERVE, "preserve")
    else:
        t_el.attrib.pop(PRESERVE, None)

    # ── Clear text from every other run (keep runs for their formatting) ──
    for r in all_runs[1:]:
        for t in r:
            if t.tag == TAG_T:
                t.text = ""
                t.attrib.pop(PRESERVE, None)


def _ai_chat(messages, api_key, model, base_url, timeout=120):
    """Call an OpenAI-compatible /chat/completions endpoint."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 8192,
    }).encode("utf-8")
    req = Request(f"{base_url}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"].strip()


import re as _re_module

# Fixed system prompt — no user input interpolated here (prompt injection prevention)
_AI_SYSTEM_PROMPT = """\
You are a professional document editor working with Word documents from Kazakhstani enterprise software (1C).

You will receive:
1. A task instruction (what to do with the text)
2. A numbered list of paragraphs using the format: <<N>> paragraph text

Hard rules (never violate):
- Return EXACTLY the same number of paragraphs you received.
- Keep the same <<N>> markers, one paragraph per marker, in the same order.
- NEVER modify, translate, rename, or remove any placeholder in square brackets \
(e.g. [ПараметрыДанных...], [ParamName], [AnyBracketedToken]). Copy them verbatim.
- Preserve the original language of each paragraph (Russian stays Russian, Kazakh stays Kazakh). \
Never translate between languages unless the task explicitly says so.
- Do not invent, add, or remove facts, names, dates, numbers, or legal references.
- Fix obvious artifacts from placeholder substitution \
(e.g. merged words "ИвановымдолжностьИвановичем" → "Ивановым Иваном Ивановичем должность"), \
but only spacing/punctuation — never invent content.
- If a paragraph needs no change, return it unchanged.
- Output ONLY the numbered paragraphs. No preamble, no explanation, no markdown, no code fences.\
"""

_PLACEHOLDER_RE   = _re_module.compile(r'\[[^\[\]\n\r]{1,120}\]')
_MARKER_RE        = _re_module.compile(r'^<<(\d+)>>\s*(.*)', _re_module.MULTILINE)
_NUMBER_PREFIX_RE = _re_module.compile(r'^(\d+)[.\s]')

# Kazakh-specific characters (not present in Russian)
_KZ_CHARS = frozenset("әіңғүұқөһӘІҢҒҮҰҚӨҺ")


def _extract_placeholders(text):
    return set(_PLACEHOLDER_RE.findall(text))


# ── Bilingual helpers ─────────────────────────────────────────────────────

# Pattern that identifies the bilingual city/date line separating header from content,
# e.g. "Алматы қаласы    город Алматы" or "Нұр-Сұлтан қаласы    город Нур-Султан"
_CONTENT_BORDER_RE = _re_module.compile(r'қаласы|Нұр.Сұлтан|қ\.а\.', _re_module.IGNORECASE)

_BILINGUAL_ANALYSIS_SYSTEM = (
    "You are analyzing a bilingual Kazakh-Russian legal document from a Kazakhstani company.\n"
    "You will receive a numbered list of paragraphs from the document body (non-table).\n\n"
    "Your task:\n"
    "1. Identify paragraphs that contain Russian-language content (titles, clauses, basis lines).\n"
    "2. For each Russian paragraph, check if a corresponding Kazakh paragraph already exists\n"
    "   nearby (same meaning/structure, but in Kazakh). If yes — mark has_kz_stub=true and\n"
    "   set kz_stub_seq to its [N] number.\n"
    "3. Translate each Russian paragraph into formal Kazakhstani official Kazakh "
    "(\u0456\u0441 \u049b\u0430\u0493\u0430\u0437\u0434\u0430\u0440\u044b \u0442\u0456\u043b\u0456).\n\n"
    "Translation rules:\n"
    "- Keep ALL [placeholder] tokens exactly verbatim \u2014 never translate placeholder names.\n"
    "- Keep \u00abКазахстанско-Китайский Трубопровод\u00bb and \u00abТОО\u00bb as-is (proper names).\n"
    "- Preserve clause number prefixes: if source starts with '1.' start translation with '1.'\n"
    "- Use formal official document style.\n\n"
    "Return ONLY a JSON array, no commentary, no markdown:\n"
    "[\n"
    "  {\n"
    '    "ru_seq": 5,\n'
    '    "ru_text_preview": "first 60 chars of Russian paragraph",\n'
    '    "has_kz_stub": true,\n'
    '    "kz_stub_seq": 2,\n'
    '    "translated": "full Kazakh translation"\n'
    "  }\n"
    "]\n\n"
    "Fields:\n"
    "- ru_seq: the [N] number of the Russian paragraph\n"
    "- ru_text_preview: first 60 chars (for verification)\n"
    "- has_kz_stub: true if a Kazakh equivalent already exists in the document\n"
    "- kz_stub_seq: [N] of the existing Kazakh stub (only when has_kz_stub=true)\n"
    "- translated: the full Kazakh translation\n"
    "Do NOT include paragraphs that are already in Kazakh or are empty/trivial."
)


def _lang_label(text):
    """Classify one paragraph as 'kz', 'ru', 'empty', or 'other'."""
    stripped = text.strip()
    if not stripped:
        return 'empty'
    has_kz = any(c in _KZ_CHARS for c in stripped)
    has_cyrillic = any('\u0400' <= c <= '\u04ff' for c in stripped)
    # Short line with both KZ and Russian text = bilingual header/divider
    if has_kz and has_cyrillic and len(stripped) <= 120 and _CONTENT_BORDER_RE.search(stripped):
        return 'border'
    if has_kz:
        return 'kz'
    # Near-empty: just a clause number like "1." or "2. "
    core = stripped.rstrip('0123456789. \t')
    if not core or len(stripped) < 5:
        return 'empty'
    if has_cyrillic:
        return 'ru'
    return 'other'


def _find_content_start(para_texts):
    """
    Find the index of the first content paragraph after the document header.

    The header ends at the bilingual city/date line, e.g.:
      "Алматы қаласы    город Алматы"
      "Нұр-Сұлтан қаласы    город Нур-Султан"

    Returns the index AFTER that line, or 0 if no border found (process all).
    """
    for i, text in enumerate(para_texts):
        if _lang_label(text) == 'border':
            return i + 1
    return 0


def _get_num(text):
    """Extract leading clause number from text like '1.' or '2. text'."""
    m = _NUMBER_PREFIX_RE.match(text.strip())
    return int(m.group(1)) if m else None


def _detect_bilingual_pairs(para_texts, content_start=0):  # kept for import compat
    return []  # replaced by _run_bilingual_stage_v2


def _apply_translated_text_to_clone(cloned_para, source_text, translated_text, W):
    """
    Rewrite runs of cloned_para to contain translated_text, preserving
    run-level formatting (italic, underline, bold, font size) from the source runs.

    Strategy:
    - Build placeholder → rPr map from cloned runs (which mirror source formatting)
    - Tokenise translated_text into plain-text and [placeholder] segments
    - Replace all run-bearing children with new runs:
        plain text  → bare <w:r>
        placeholder → <w:r> with rPr copied from source run that contained it
    - Keep <w:pPr> unchanged
    """
    from lxml import etree as _ET
    from copy import deepcopy

    TAG_R   = f"{{{W}}}r"
    TAG_T   = f"{{{W}}}t"
    TAG_RPR = f"{{{W}}}rPr"
    TAG_PPR = f"{{{W}}}pPr"
    XML_SPC = "http://www.w3.org/XML/1998/namespace"
    PRESERVE = f"{{{XML_SPC}}}space"

    # ── Build placeholder → rPr map from existing (cloned) runs ──────────
    ph_fmt = {}  # placeholder_text → rPr element or None
    for r in cloned_para.iter(TAG_R):
        rpr      = r.find(TAG_RPR)
        run_text = "".join((t.text or "") for t in r if t.tag == TAG_T)
        for ph in PLACEHOLDER_RE.findall(run_text):
            if ph not in ph_fmt:
                ph_fmt[ph] = deepcopy(rpr) if rpr is not None else None

    # ── Remove all run-bearing children (keep pPr, bookmarks) ────────────
    to_remove = [
        ch for ch in list(cloned_para)
        if ch.tag != TAG_PPR and (ch.tag == TAG_R or any(True for _ in ch.iter(TAG_R)))
    ]
    for el in to_remove:
        cloned_para.remove(el)

    # ── Tokenise translated_text and build new runs ───────────────────────
    parts        = PLACEHOLDER_RE.split(translated_text)
    placeholders = PLACEHOLDER_RE.findall(translated_text)
    new_runs     = []

    for i, segment in enumerate(parts):
        if segment:
            r = _ET.Element(TAG_R)
            t = _ET.SubElement(r, TAG_T)
            t.text = segment
            if segment[0] == " " or segment[-1] == " ":
                t.set(PRESERVE, "preserve")
            new_runs.append(r)
        if i < len(placeholders):
            ph  = placeholders[i]
            rpr = ph_fmt.get(ph)
            r   = _ET.Element(TAG_R)
            if rpr is not None:
                r.append(deepcopy(rpr))
            t = _ET.SubElement(r, TAG_T)
            t.text = ph
            new_runs.append(r)

    # ── Insert after pPr ──────────────────────────────────────────────────
    ppr      = cloned_para.find(TAG_PPR)
    children = list(cloned_para)
    insert_at = (children.index(ppr) + 1) if ppr is not None else 0
    for i, run in enumerate(new_runs):
        cloned_para.insert(insert_at + i, run)


def _run_bilingual_stage_v2(root, all_paras, content_start, prompt, api_key, model, base_url):
    """
    Single AI call: let AI identify Russian paragraphs, pair them with existing
    Kazakh stubs (or flag for insertion), and provide translations.

    For each translation:
      Case A — KZ stub exists → replace stub XML with a clone of the RU paragraph
               that has translated text applied.
      Case B — No KZ stub   → insert the clone BEFORE the RU paragraph.

    Operates directly on the lxml tree (in-place). Does not return a value.
    """
    from copy import deepcopy

    W      = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    TAG_TC = f"{{{W}}}tc"

    def _get_text(p):
        return "".join((t.text or "") for t in p.iter(f"{{{W}}}t"))

    def _in_table(p):
        el = p
        while el is not None:
            if el.tag == TAG_TC:
                return True
            el = el.getparent()
        return False

    # ── Build numbered paragraph list (body only, after header) ──────────
    numbered_lines = []
    idx_map        = {}   # seq_num → index in all_paras
    seq            = 0

    for g_idx, p in enumerate(all_paras):
        if g_idx < content_start:
            continue
        if _in_table(p):
            continue
        text = _get_text(p).strip()
        if text:
            numbered_lines.append(f"[{seq}] {text}")
            idx_map[seq] = g_idx
            seq += 1

    if not numbered_lines:
        app.logger.info("[bilingual_v2] no content paragraphs found after header")
        return

    paragraph_list = "\n".join(numbered_lines)
    app.logger.info("[bilingual_v2] sending %d paragraphs to AI for analysis", seq)

    # ── Single AI call ────────────────────────────────────────────────────
    try:
        response = _ai_chat(
            [{"role": "system", "content": _BILINGUAL_ANALYSIS_SYSTEM},
             {"role": "user",   "content": paragraph_list}],
            api_key, model, base_url, timeout=180,
        )
    except Exception as exc:
        app.logger.error("[bilingual_v2] AI call failed: %s", exc)
        return

    # ── Parse JSON ────────────────────────────────────────────────────────
    try:
        raw = response.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        results = json.loads(raw)
        if not isinstance(results, list):
            raise ValueError("expected JSON array")
    except Exception as exc:
        app.logger.error(
            "[bilingual_v2] JSON parse failed: %s\nResponse snippet: %s",
            exc, response[:400],
        )
        return

    # ── Apply translations ────────────────────────────────────────────────
    translated = 0
    skipped    = 0

    for item in results:
        ru_seq      = item.get("ru_seq")
        translated_ = (item.get("translated") or "").strip()
        has_stub    = item.get("has_kz_stub", False)
        kz_stub_seq = item.get("kz_stub_seq")

        if ru_seq is None or not translated_:
            continue

        ru_g_idx = idx_map.get(ru_seq)
        if ru_g_idx is None:
            app.logger.warning("[bilingual_v2] ru_seq=%d not in idx_map", ru_seq)
            continue

        ru_para  = all_paras[ru_g_idx]
        ru_text  = _get_text(ru_para).strip()

        # Placeholder validation
        src_ph  = _extract_placeholders(ru_text)
        out_ph  = _extract_placeholders(translated_)
        missing = src_ph - out_ph
        if missing:
            app.logger.warning(
                "[bilingual_v2] ru[%d]: placeholders missing %s — skipping",
                ru_seq, missing,
            )
            skipped += 1
            continue

        # Clone the RU paragraph and apply translation
        cloned = deepcopy(ru_para)
        _apply_translated_text_to_clone(cloned, ru_text, translated_, W)

        if has_stub and kz_stub_seq is not None:
            # Case A: replace existing KZ stub
            kz_g_idx = idx_map.get(kz_stub_seq)
            if kz_g_idx is not None:
                kz_para = all_paras[kz_g_idx]
                parent  = kz_para.getparent()
                pos     = list(parent).index(kz_para)
                parent.remove(kz_para)
                parent.insert(pos, cloned)
                all_paras[kz_g_idx] = cloned   # keep reference current
                app.logger.info(
                    "[bilingual_v2] replaced kz stub [seq=%d, g=%d] with ru[seq=%d] translation",
                    kz_stub_seq, kz_g_idx, ru_seq,
                )
                translated += 1
            else:
                app.logger.warning(
                    "[bilingual_v2] kz_stub_seq=%d not in idx_map", kz_stub_seq
                )
                skipped += 1
        else:
            # Case B: insert KZ clone before the RU paragraph
            parent = ru_para.getparent()
            pos    = list(parent).index(ru_para)
            parent.insert(pos, cloned)
            app.logger.info(
                "[bilingual_v2] inserted KZ translation before ru[seq=%d, g=%d]",
                ru_seq, ru_g_idx,
            )
            translated += 1

    app.logger.info(
        "[bilingual_v2] done: translated=%d skipped=%d",
        translated, skipped,
    )


def _apply_prompt_to_texts(indexed_texts, prompt, api_key, model, base_url):
    """
    indexed_texts: list of (original_index, text) for non-empty paragraphs.
    Returns a dict {original_index: new_text}.
    Sends paragraphs in batches of 60 using <<N>> markers.
    Validates count and placeholder preservation; falls back to original on failure.
    """
    BATCH = 60
    result = {}

    for start in range(0, len(indexed_texts), BATCH):
        chunk    = indexed_texts[start:start + BATCH]
        expected = len(chunk)

        numbered = "\n".join(f"<<{k+1}>> {text}" for k, (_, text) in enumerate(chunk))
        user_msg = f"Task: {prompt}\n\nParagraphs to process:\n{numbered}"

        # ── Call AI, retry once on count mismatch ────────────────────────
        parsed = None
        for attempt in range(2):
            try:
                response = _ai_chat(
                    [{"role": "system", "content": _AI_SYSTEM_PROMPT},
                     {"role": "user",   "content": user_msg}],
                    api_key, model, base_url,
                )
            except Exception as exc:
                app.logger.error("[AI batch] attempt %d API error: %s", attempt + 1, exc)
                break

            raw_parsed = {}
            for m in _MARKER_RE.finditer(response):
                raw_parsed[int(m.group(1))] = m.group(2).strip()

            got = len(raw_parsed)
            if got == expected:
                parsed = raw_parsed
                app.logger.info(
                    "[AI batch] prompt=%r chunk=%d-%d paragraphs_in=%d paragraphs_out=%d OK",
                    prompt[:60], start + 1, start + expected, expected, got,
                )
                break
            else:
                app.logger.warning(
                    "[AI batch] attempt %d count mismatch: expected %d got %d — %s",
                    attempt + 1, expected, got,
                    "retrying" if attempt == 0 else "keeping originals for this batch",
                )

        # ── Per-paragraph placeholder validation & assignment ─────────────
        for k, (orig_idx, orig_text) in enumerate(chunk):
            ai_num = k + 1
            if parsed is None or ai_num not in parsed:
                # batch failed entirely — keep original
                result[orig_idx] = orig_text
                continue

            new_text = parsed[ai_num]
            orig_placeholders = _extract_placeholders(orig_text)
            new_placeholders  = _extract_placeholders(new_text)
            missing = orig_placeholders - new_placeholders

            if missing:
                app.logger.warning(
                    "[AI batch] para %d: placeholder(s) lost %s — keeping original",
                    orig_idx, missing,
                )
                result[orig_idx] = orig_text
                continue

            # Word-count sanity check: reject if AI changed word count by >30%
            orig_words = len(orig_text.split())
            new_words  = len(new_text.split())
            if orig_words > 3 and new_words > 0:
                ratio = new_words / orig_words
                if ratio < 0.7 or ratio > 1.3:
                    app.logger.warning(
                        "[AI batch] para %d: word count changed %.0f%% (%d→%d) — keeping original",
                        orig_idx, (ratio - 1) * 100, orig_words, new_words,
                    )
                    result[orig_idx] = orig_text
                    continue

            app.logger.debug(
                "[AI batch] para %d: words %d→%d placeholders_preserved=True",
                orig_idx, orig_words, new_words,
            )
            result[orig_idx] = new_text

    return result


def _docx_apply_ai_prompts(docx_bytes, steps, api_key, model, base_url):
    """
    Apply a sequence of pipeline steps to every paragraph in a .docx.

    steps: list of dicts, each one of:
      {"type": "text",               "prompt": "..."}  — grammar/style correction
      {"type": "translate_bilingual","prompt": "..."}  — fill empty Kazakh paragraphs

    Preserves all structure (tables, headers, lists) and character/paragraph styles.
    Returns modified .docx bytes.
    """
    from lxml import etree as _ET
    import zipfile as _zf

    W       = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    XMLSPC  = "http://www.w3.org/XML/1998/namespace"
    TAG_P   = f"{{{W}}}p"
    TAG_R   = f"{{{W}}}r"
    TAG_T   = f"{{{W}}}t"
    TAG_RPR = f"{{{W}}}rPr"
    PRESERVE = f"{{{XMLSPC}}}space"

    # ── Unpack ZIP ────────────────────────────────────────────────────────
    with _zf.ZipFile(io.BytesIO(docx_bytes)) as zin:
        names     = zin.namelist()
        zinfo_map = {i.filename: i for i in zin.infolist()}
        files     = {n: zin.read(n) for n in names}

    if "word/document.xml" not in files:
        raise ValueError("Not a valid .docx file")

    # ── Parse ─────────────────────────────────────────────────────────────
    root = _ET.fromstring(files["word/document.xml"])
    all_paras = list(root.iter(TAG_P))

    # Current text state per paragraph index
    para_texts = [_para_get_text(p, TAG_R, TAG_T) for p in all_paras]

    # Detect where the document header ends (bilingual city/date line).
    content_start = _find_content_start(para_texts)
    if content_start:
        app.logger.info("[pipeline] header ends at para %d, content starts there", content_start)

    # ── Phase 1: Text steps (accumulate corrections in para_texts) ────────
    for step in steps:
        if step.get("type", "text") != "text":
            continue
        prompt  = step.get("prompt", "")
        indexed = [(i, t) for i, t in enumerate(para_texts)
                   if t.strip() and i >= content_start]
        if not indexed:
            continue
        updates = _apply_prompt_to_texts(indexed, prompt, api_key, model, base_url)
        for orig_idx, new_text in updates.items():
            para_texts[orig_idx] = new_text

    # ── Write text corrections back into the XML tree ─────────────────────
    for para, new_text in zip(all_paras, para_texts):
        orig = _para_get_text(para, TAG_R, TAG_T)
        if orig != new_text:
            _para_set_text(para, new_text, TAG_R, TAG_T, TAG_RPR, PRESERVE)

    # ── Phase 2: Bilingual step (operates directly on XML tree) ──────────
    for step in steps:
        if step.get("type") != "translate_bilingual":
            continue
        # Re-collect after text writes (tree is now up-to-date)
        all_paras = list(root.iter(TAG_P))
        _run_bilingual_stage_v2(
            root, all_paras, content_start,
            step.get("prompt", ""), api_key, model, base_url,
        )
        break  # only one bilingual stage makes sense per document

    for step in steps:
        if step.get("type") not in ("text", "translate_bilingual"):
            app.logger.warning("[pipeline] unknown step type %r — skipping", step.get("type"))

    modified_xml = _ET.tostring(root, xml_declaration=True,
                                encoding="UTF-8", standalone=True)

    # ── Repack ZIP ────────────────────────────────────────────────────────
    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as zout:
        for name in names:
            data = modified_xml if name == "word/document.xml" else files[name]
            zout.writestr(zinfo_map[name], data)
    return buf.getvalue()


# ── In-memory task store ──────────────────────────────────────────────────
# task = {"status": "pending"|"processing"|"done"|"error",
#          "out_name": str, "result_path": Path|None, "error": str|None,
#          "created_at": float}
_AI_TASKS = {}  # type: ignore[var-annotated]  # dict[str, dict]
_AI_TASKS_LOCK = threading.Lock()

_AI_TASKS_DIR = Path(tempfile.gettempdir()) / "kazuni_ai_tasks"
_AI_TASKS_DIR.mkdir(parents=True, exist_ok=True)


def _ai_task_worker(task_id, doc_bytes, steps, out_name, api_key, model, base_url):
    """Background thread: run AI pipeline steps and update task state."""
    with _AI_TASKS_LOCK:
        _AI_TASKS[task_id]["status"] = "processing"
    try:
        result_bytes = _docx_apply_ai_prompts(doc_bytes, steps, api_key, model, base_url)
        out_path = _AI_TASKS_DIR / f"{task_id}.docx"
        out_path.write_bytes(result_bytes)
        with _AI_TASKS_LOCK:
            _AI_TASKS[task_id]["status"] = "done"
            _AI_TASKS[task_id]["result_path"] = out_path
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw).get("error", {}).get("message", raw)
        except Exception:
            msg = raw
        with _AI_TASKS_LOCK:
            _AI_TASKS[task_id]["status"] = "error"
            _AI_TASKS[task_id]["error"] = f"AI API error: {msg}"
    except Exception as e:
        with _AI_TASKS_LOCK:
            _AI_TASKS[task_id]["status"] = "error"
            _AI_TASKS[task_id]["error"] = str(e)


@app.route("/api/ai/apply-prompts", methods=["POST"])
def ai_apply_prompts():
    """
    POST application/json:
      {
        "filename":       "шаблон.docx",          // original file name
        "content_base64": "<base64 encoded docx>", // document bytes as Base64
        "prompts":        ["prompt 1", "prompt 2"] // array of prompts to apply in order
      }

    Returns immediately with {"task_id": "..."}.
    Poll GET /api/ai/tasks/<task_id> for status.
    Download via GET /api/ai/tasks/<task_id>/download when status == "done".
    """
    from presets import PRESETS

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Request body must be JSON (application/json)"}), 400

    content_b64  = body.get("content_base64")
    prompts_raw  = body.get("prompts")
    preset_key   = body.get("preset")
    filename     = body.get("filename") or "document.docx"

    if not content_b64:
        return jsonify({"error": "Field 'content_base64' is required"}), 400

    try:
        doc_bytes = base64.b64decode(content_b64)
    except Exception:
        return jsonify({"error": "Field 'content_base64' is not valid Base64"}), 400

    # ── Resolve steps ─────────────────────────────────────────────────────
    # Priority: explicit prompts array > preset > error
    # Old format (prompts: [str]) is converted to text steps for backward compat.
    steps = []

    if prompts_raw and isinstance(prompts_raw, list):
        plain = [p.strip() for p in prompts_raw if isinstance(p, str) and p.strip()]
        steps = [{"type": "text", "prompt": p} for p in plain]

    if not steps:
        if preset_key:
            if preset_key not in PRESETS:
                return jsonify({
                    "error":             f"Unknown preset '{preset_key}'",
                    "available_presets": {k: v["name"] for k, v in PRESETS.items()},
                }), 400
            preset = PRESETS[preset_key]
            if "steps" in preset:
                steps = preset["steps"]                              # new rich format
            else:
                steps = [{"type": "text", "prompt": p}              # legacy format
                         for p in preset.get("prompts", [])]
        else:
            return jsonify({
                "error":             "Provide either a non-empty 'prompts' array or a valid 'preset'",
                "available_presets": {k: v["name"] for k, v in PRESETS.items()},
            }), 400

    if not steps:
        return jsonify({"error": "No steps resolved — check prompts or preset"}), 400

    api_key  = os.environ.get("AI_API_KEY",  "").strip()
    model    = os.environ.get("AI_MODEL",    "gpt-4o-mini").strip() or "gpt-4o-mini"
    base_url = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")

    if not api_key:
        return jsonify({"error": "AI_API_KEY is not configured on the server"}), 500

    filename = secure_filename(filename)
    if not filename.lower().endswith(".docx"):
        filename += ".docx"
    stem     = filename[:-5]
    out_name = f"{stem}_ai_edited.docx"

    task_id = str(uuid.uuid4())
    with _AI_TASKS_LOCK:
        _AI_TASKS[task_id] = {
            "status":      "pending",
            "out_name":    out_name,
            "result_path": None,
            "error":       None,
            "created_at":  time.time(),
        }

    t = threading.Thread(
        target=_ai_task_worker,
        args=(task_id, doc_bytes, steps, out_name, api_key, model, base_url),
        daemon=True,
    )
    t.start()

    return jsonify({"task_id": task_id}), 202


@app.route("/api/ai/tasks/<task_id>", methods=["GET"])
def ai_task_status(task_id):
    """
    Returns {"task_id", "status", "error"}.
    status: "pending" | "processing" | "done" | "error"
    """
    with _AI_TASKS_LOCK:
        task = _AI_TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({
        "task_id": task_id,
        "status":  task["status"],
        "error":   task.get("error"),
    })


@app.route("/api/ai/tasks/<task_id>/download", methods=["GET"])
def ai_task_download(task_id):
    """Download the result .docx once status == "done"."""
    with _AI_TASKS_LOCK:
        task = _AI_TASKS.get(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    if task["status"] != "done":
        return jsonify({"error": f"Task is not done yet (status: {task['status']})"}), 409

    result_path = task["result_path"]
    if not result_path or not result_path.exists():
        return jsonify({"error": "Result file missing"}), 500

    return send_file_compat(
        io.BytesIO(result_path.read_bytes()),
        as_attachment=True,
        download_name=task["out_name"],
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ═══════════════════════════════════════════════════════════════════════════
# AI Text Assistant Plugin
# ═══════════════════════════════════════════════════════════════════════════

_AI_PLUGIN_GUID = "asc.{c4d5e6f7-a1b2-4c3d-8e9f-012345678901}"


@app.route("/api/ai-plugin/config.json")
def ai_plugin_config():
    from flask import Response as _R
    cfg = {
        "name": "AI Ассистент",
        "nameLocale": {},
        "guid": _AI_PLUGIN_GUID,
        "version": "1.0.0",
        "minVersion": "8.2.0",
        "variations": [
            {
                # Variation 0 — visual sidebar panel
                "description": "AI обработка текста документа",
                "url": "index.html",
                "initDataType": "none",
                "icons": ["icon.png"],
                "isViewer": False,
                "EditorsSupport": ["word"],
                "isVisual": True,
                "isModal": False,
                "isInsideMode": True,
                "isUpdateOleOnResize": False,
                "buttons": [],
            },
            {
                # Variation 1 — background process: context menu registration
                "description": "AI контекстное меню",
                "url": "code.html",
                "type": "background",
                "initDataType": "none",
                "icons": ["icon.png"],
                "isViewer": False,
                "EditorsSupport": ["word"],
                "isVisual": False,
                "buttons": [],
            },
        ],
    }
    return _R(json.dumps(cfg), mimetype="application/json")


@app.route("/api/ai-plugin/icon.png")
def ai_plugin_icon():
    from flask import Response as _R
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (40, 40), (67, 97, 238, 255))
    draw = ImageDraw.Draw(img)
    # Draw "AI" letters
    draw.text((5, 10), "AI", fill=(255, 255, 255, 255))
    # Small spark dots
    for cx, cy in [(30, 8), (34, 14), (28, 18)]:
        draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=(255, 220, 80, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return _R(buf.getvalue(), mimetype="image/png")


@app.route("/api/ai-plugin/index.html")
def ai_plugin_index():
    from flask import Response as _R
    html = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="/onlyoffice/sdkjs-plugins/v1/plugins.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font:13px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f7f8fa;color:#222;overflow-x:hidden}
#app{display:flex;flex-direction:column;gap:0;min-height:100vh}
.section{background:#fff;border-bottom:1px solid #e8e8e8;padding:10px 12px}
.section-title{font-size:11px;font-weight:600;color:#888;text-transform:uppercase;
               letter-spacing:.5px;margin-bottom:7px}
.row{display:flex;gap:6px;align-items:center;margin-bottom:6px}
.row:last-child{margin-bottom:0}
textarea{width:100%;border:1px solid #ddd;border-radius:5px;padding:7px 8px;
         font:inherit;resize:vertical;background:#fafafa;min-height:70px;
         color:#333;outline:none}
textarea:focus{border-color:#4361ee;background:#fff}
textarea[readonly]{background:#f2f4f8;cursor:default}
button{border:none;border-radius:5px;padding:6px 11px;font:inherit;
       cursor:pointer;transition:background .15s,opacity .15s;white-space:nowrap}
button:disabled{opacity:.45;cursor:not-allowed}
.btn-primary{background:#4361ee;color:#fff;font-weight:600}
.btn-primary:hover:not(:disabled){background:#3451d1}
.btn-secondary{background:#e9ecf5;color:#333}
.btn-secondary:hover:not(:disabled){background:#dde2f0}
.btn-success{background:#2ec4b6;color:#fff;font-weight:600}
.btn-success:hover:not(:disabled){background:#27a99d}
.actions-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.action-btn{background:#f0f2fa;color:#333;font-size:12px;padding:7px 6px;
            border-radius:6px;text-align:center;border:1px solid #e0e3f0}
.action-btn:hover:not(:disabled){background:#e0e4f5;border-color:#c5cce8}
.custom-row{display:flex;gap:6px;margin-top:7px}
.custom-row textarea{min-height:48px;flex:1}
.custom-row button{align-self:flex-end}
.toggle-row{display:flex;align-items:center;gap:8px}
.toggle-row input[type=checkbox]{width:16px;height:16px;accent-color:#4361ee;cursor:pointer}
.toggle-row label{font-size:13px;cursor:pointer;user-select:none}
.result-actions{display:flex;gap:6px;margin-top:7px}
#status{font-size:12px;padding:7px 12px;background:#fffbe6;border-bottom:1px solid #ffe58f;
        color:#7a5800;display:none}
#loading{display:none;padding:10px 12px;font-size:12px;color:#4361ee;
         background:#eef0fd;border-bottom:1px solid #c5cce8}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid #c5cce8;
         border-top-color:#4361ee;border-radius:50%;animation:spin .7s linear infinite;
         vertical-align:middle;margin-right:5px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div id="app">

  <div id="status"></div>
  <div id="loading"><span class="spinner"></span>Обрабатывается…</div>

  <!-- Selected text -->
  <div class="section">
    <div class="section-title">Выделенный текст</div>
    <textarea id="selectedText" readonly placeholder="Выделите текст в документе, затем нажмите «Получить»"></textarea>
    <div class="row" style="margin-top:6px">
      <button id="btnGet" class="btn-primary" style="flex:1">Получить выделенный текст</button>
    </div>
  </div>

  <!-- AI actions -->
  <div class="section">
    <div class="section-title">AI действия</div>
    <div class="actions-grid">
      <button class="action-btn" data-action="grammar">✓ Грамматика</button>
      <button class="action-btn" data-action="rewrite">✎ Переписать</button>
      <button class="action-btn" data-action="dedup">⊘ Убрать повторы</button>
    </div>
    <div class="custom-row">
      <textarea id="customPrompt" placeholder="Свой промпт…"></textarea>
      <button id="btnCustom" class="btn-secondary">▶ Выполнить</button>
    </div>
  </div>

  <!-- Text functions (no AI) -->
  <div class="section">
    <div class="section-title">Текстовые функции</div>
    <div class="actions-grid">
      <button class="action-btn" data-action="upper">АА ВЕРХНИЙ</button>
      <button class="action-btn" data-action="lower">аа нижний</button>
      <button class="action-btn" data-action="capitalize">Аа Первая буква</button>
    </div>
  </div>

  <!-- Auto accept -->
  <div class="section">
    <div class="toggle-row">
      <input type="checkbox" id="autoAccept" checked>
      <label for="autoAccept">Авто-принять результат</label>
    </div>
  </div>

  <!-- Result (shown when auto-accept is OFF) -->
  <div class="section" id="resultSection" style="display:none">
    <div class="section-title">Результат</div>
    <textarea id="resultText" readonly></textarea>
    <div class="result-actions">
      <button id="btnReplace" class="btn-success" style="flex:1">⇄ Заменить</button>
      <button id="btnCopy" class="btn-secondary" style="flex:1">⧉ Копировать</button>
    </div>
  </div>

</div>
<script src="/api/ai-plugin/script.js"></script>
</body>
</html>"""
    return _R(html, mimetype="text/html")


@app.route("/api/ai-plugin/script.js")
def ai_plugin_script():
    from flask import Response as _R
    js = r"""
/* AI Text Assistant – OnlyOffice sidebar plugin */
"use strict";

var _selected = "";
var _result   = "";

/* ── helpers ──────────────────────────────────────────────────────── */
function $(id){ return document.getElementById(id); }

function showStatus(msg, isError) {
  var el = $("status");
  el.textContent = msg;
  el.style.display = msg ? "block" : "none";
  el.style.background = isError ? "#fff2f0" : "#fffbe6";
  el.style.color      = isError ? "#a8071a" : "#7a5800";
  el.style.borderBottomColor = isError ? "#ffccc7" : "#ffe58f";
}

function setLoading(on) {
  $("loading").style.display = on ? "block" : "none";
  document.querySelectorAll("button").forEach(function(b){ b.disabled = on; });
}

/* ── OO plugin API wrappers ────────────────────────────────────────── */
function getSelectedText(cb) {
  window.Asc.plugin.executeMethod(
    "GetSelectedText",
    [{ Numbering: false, Math: false }],
    function(text){ cb(text || ""); }
  );
}

function replaceSelection(text) {
  window.Asc.plugin.executeMethod("PasteText", [text], function(){});
}

/* ── result handling ───────────────────────────────────────────────── */
function applyResult(result) {
  _result = result;
  showStatus("", false);
  if ($("autoAccept").checked) {
    replaceSelection(result);
    showStatus("✓ Готово — текст заменён", false);
    setTimeout(function(){ showStatus("", false); }, 2500);
    $("resultSection").style.display = "none";
  } else {
    $("resultText").value = result;
    $("resultSection").style.display = "block";
    $("resultSection").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  setLoading(false);
}

/* ── local text transforms ─────────────────────────────────────────── */
function localTransform(action) {
  if (!_selected.trim()) {
    showStatus("Сначала получите выделенный текст", true); return;
  }
  var r;
  if (action === "upper")           r = _selected.toUpperCase();
  else if (action === "lower")      r = _selected.toLowerCase();
  else if (action === "capitalize") r = _selected.charAt(0).toUpperCase() + _selected.slice(1).toLowerCase();
  else return;
  applyResult(r);
}

/* ── AI call ───────────────────────────────────────────────────────── */
function runAI(action, customPrompt) {
  if (!_selected.trim()) {
    showStatus("Сначала получите выделенный текст", true); return;
  }
  setLoading(true);
  showStatus("", false);

  fetch("/api/ai/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action:        action,
      text:          _selected,
      custom_prompt: customPrompt || "",
    }),
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(d){ throw new Error(d.error || r.status); });
    return r.json();
  })
  .then(function(d) {
    if (d.error) throw new Error(d.error);
    applyResult(d.result);
  })
  .catch(function(e) {
    showStatus("Ошибка: " + e.message, true);
    setLoading(false);
  });
}

/* ── init ──────────────────────────────────────────────────────────── */
window.Asc = window.Asc || {};
window.Asc.plugin = window.Asc.plugin || {};

window.Asc.plugin.init = function() {

  $("btnGet").onclick = function() {
    getSelectedText(function(text) {
      _selected = text;
      $("selectedText").value = text || "";
      $("resultSection").style.display = "none";
      showStatus("", false);
      if (!text) showStatus("Текст не выделен — выделите фрагмент в документе", true);
    });
  };

  document.querySelectorAll(".action-btn[data-action]").forEach(function(btn) {
    btn.onclick = function() {
      var action = btn.getAttribute("data-action");
      if (action === "upper" || action === "lower" || action === "capitalize") {
        localTransform(action);
      } else {
        runAI(action, "");
      }
    };
  });

  $("btnCustom").onclick = function() {
    var p = $("customPrompt").value.trim();
    if (!p) { showStatus("Введите промпт", true); return; }
    runAI("custom", p);
  };

  $("btnReplace").onclick = function() {
    if (_result) {
      replaceSelection(_result);
      showStatus("✓ Текст заменён", false);
      setTimeout(function(){ showStatus("", false); }, 2000);
      $("resultSection").style.display = "none";
    }
  };

  $("btnCopy").onclick = function() {
    if (!_result) return;
    var fallback = function() {
      $("resultText").select();
      try { document.execCommand("copy"); } catch(e){}
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(_result).then(function() {
        showStatus("✓ Скопировано в буфер", false);
        setTimeout(function(){ showStatus("", false); }, 2000);
      }).catch(fallback);
    } else { fallback(); }
  };
};

window.Asc.plugin.button = function() {};
"""
    return _R(js, mimetype="application/javascript")


@app.route("/api/ai-plugin/code.html")
def ai_plugin_code_html():
    from flask import Response as _R
    html = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<script src="/onlyoffice/sdkjs-plugins/v1/plugins.js"></script>
</head>
<body>
<script src="/api/ai-plugin/code.js"></script>
</body>
</html>"""
    return _R(html, mimetype="text/html")


@app.route("/api/ai-plugin/code.js")
def ai_plugin_code_js():
    from flask import Response as _R
    js = r"""
/* AI Ассистент — background variation
   Registers context menu items using Asc.ButtonContextMenu (OO 9.x API).
   Context menu actions always auto-accept (paste immediately).
   For preview/copy workflow use the sidebar panel instead.
*/
"use strict";

function _runLocal(text, action) {
  if (action === "upper")      return text.toUpperCase();
  if (action === "lower")      return text.toLowerCase();
  if (action === "capitalize") return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
  return text;
}

function _runAI(text, action, customPrompt) {
  return fetch("/api/ai/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: action, text: text, custom_prompt: customPrompt || "" }),
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(d){ throw new Error(d.error || r.status); });
    return r.json();
  })
  .then(function(d) {
    if (d.error) throw new Error(d.error);
    return d.result;
  });
}

function _withSelectedText(cb) {
  window.Asc.plugin.executeMethod(
    "GetSelectedText",
    [{ Numbering: false, Math: false }],
    function(text) {
      if (text && text.trim()) cb(text);
    }
  );
}

function _paste(text) {
  window.Asc.plugin.executeMethod("PasteText", [text], function() {});
}

function _addSubBtn(parent, label, handler) {
  var btn = new Asc.ButtonContextMenu(parent);
  btn.text = label;
  btn.onClick = handler;
  return btn;
}

window.Asc = window.Asc || {};
window.Asc.plugin = window.Asc.plugin || {};

window.Asc.plugin.init = function() {
  /* Top-level "AI Ассистент" entry in the context menu */
  var root = new Asc.ButtonContextMenu();
  root.text = "AI Ассистент";
  root.addCheckers("All");

  /* ── AI actions ──────────────────────────────────────────── */
  _addSubBtn(root, "✓ Грамматика", function() {
    _withSelectedText(function(text) {
      _runAI(text, "grammar").then(_paste).catch(function(e){ console.error(e); });
    });
  });

  _addSubBtn(root, "✎ Переписать", function() {
    _withSelectedText(function(text) {
      _runAI(text, "rewrite").then(_paste).catch(function(e){ console.error(e); });
    });
  });

  _addSubBtn(root, "⊘ Убрать повторы", function() {
    _withSelectedText(function(text) {
      _runAI(text, "dedup").then(_paste).catch(function(e){ console.error(e); });
    });
  });

  /* ── Local text transforms ───────────────────────────────── */
  _addSubBtn(root, "АА ВЕРХНИЙ", function() {
    _withSelectedText(function(text) { _paste(_runLocal(text, "upper")); });
  });

  _addSubBtn(root, "аа нижний", function() {
    _withSelectedText(function(text) { _paste(_runLocal(text, "lower")); });
  });

  _addSubBtn(root, "Аа Первая буква", function() {
    _withSelectedText(function(text) { _paste(_runLocal(text, "capitalize")); });
  });
};

window.Asc.plugin.button = function() {};
"""
    return _R(js, mimetype="application/javascript")


@app.route("/api/ai/process", methods=["POST"])
def ai_process():
    """Proxy selected text + action to an OpenAI-compatible API."""
    data       = request.get_json(silent=True) or {}
    action     = str(data.get("action", "")).strip()
    text       = str(data.get("text", "")).strip()
    custom_p   = str(data.get("custom_prompt", "")).strip()
    # Credentials come from server env only — never from the client
    api_key    = os.environ.get("AI_API_KEY", "").strip()
    model      = os.environ.get("AI_MODEL",   "gpt-4o-mini").strip() or "gpt-4o-mini"
    base_url   = os.environ.get("AI_BASE_URL","https://api.openai.com/v1").strip().rstrip("/")

    if not text:
        return jsonify({"error": "Текст не указан"}), 400
    if not api_key:
        return jsonify({"error": "API ключ не указан — укажите в настройках плагина"}), 400

    PROMPTS = {
        "grammar": (
            "You are a professional editor. Fix all grammar, spelling, and punctuation errors "
            "in the text below. Return only the corrected text, no explanations."
        ),
        "rewrite": (
            "You are a professional copywriter. Rewrite the text below to be clearer, more concise, "
            "and better structured while keeping the original meaning. "
            "Return only the rewritten text, no explanations."
        ),
        "dedup": (
            "You are a text editor. Remove duplicate sentences, redundant phrases, and repeated ideas "
            "from the text below. Preserve the original meaning and style. "
            "Return only the cleaned text, no explanations."
        ),
        "custom": custom_p or "Process the following text:",
    }
    system_prompt = PROMPTS.get(action, PROMPTS["custom"])

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }).encode("utf-8")

    req = Request(f"{base_url}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        content = result["choices"][0]["message"]["content"].strip()
        return jsonify({"result": content})
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw).get("error", {}).get("message", raw)
        except Exception:
            msg = raw
        return jsonify({"error": msg}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


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
