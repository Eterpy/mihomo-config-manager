import asyncio
import os
import re
import tempfile
from email.message import Message
from email.utils import collapse_rfc2231_value
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from .backups import Backups
from .auth import AccessVerifier, CSRF, SecurityBoundary
from .errors import AppError, MissingDatabase
from .services import Store
from .settings import Settings
from .validation import check_filename_chars, filename_valid, validate_yaml

BASE = Path(__file__).parent


def archive_range(header, size):
    """Return a single inclusive byte range, or None for an unsupported range unit or multiple ranges."""
    if not header.startswith("bytes=") or "," in header:
        return None
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", header)
    if not match or not any(match.groups()):
        raise ValueError("invalid range")
    first, last = match.groups()
    if not first:
        start, end = max(0, size - int(last)), size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    if start > end:
        raise ValueError("unsatisfiable range")
    return start, end


def archive_chunks(archive, start, length):
    archive.seek(start)
    while length > 0:
        chunk = archive.read(min(65536, length))
        if not chunk:
            raise OSError("verified archive truncated")
        yield chunk
        length -= len(chunk)


class Rollback(BaseModel):
    expected_current_version: int = Field(gt=0)


class BackupCreate(BaseModel):
    password: SecretStr = Field(min_length=16, max_length=256)


class Rename(BaseModel):
    expected_updated_at: str
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class ConfigView(BaseModel):
    id: str
    name: str
    description: str
    filename: str
    size: int
    sha256: str
    version: int
    enabled: bool
    status: str
    created_at: str
    updated_at: str


UPLOAD_SCHEMA = {"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}, "name": {"type": "string", "maxLength": 100}, "description": {"type": "string", "maxLength": 2000}}}}}}}


def create_app(settings=None, verifier=None):
    settings = settings or Settings.from_env()
    csrf = CSRF()

    @asynccontextmanager
    async def lifespan(app):
        os.umask(0o077)
        try:
            app.state.store = await asyncio.to_thread(Store, settings)
            app.state.backups = await asyncio.to_thread(Backups, app.state.store)
        except MissingDatabase:
            raise
        except Exception:
            if hasattr(app.state, "store"):
                app.state.store.close()
            raise RuntimeError("存储初始化或恢复失败，请检查磁盘、权限、数据库版本与单实例限制") from None
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.backups.close)
            await asyncio.to_thread(app.state.store.close)

    app = FastAPI(title="YAML Config Manager", version="0.4.0", lifespan=lifespan, docs_url=None, redoc_url=None,
                  telemetry={"auto_configure": False, "tracing": False, "metrics": False, "logs": False, "operation_spans": False})
    app.state.settings = settings
    app.state.csrf = csrf
    app.add_middleware(SecurityBoundary, settings=settings, verifier=verifier or AccessVerifier(settings), csrf=csrf)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(exc.body, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        for err in exc.errors():
            if err["type"] != "missing":
                continue
            missing_revision = err["loc"][-1] in {"expected_updated_at", "expected_current_version"}
            missing_body = err["loc"] == ("body",) and request.scope["route"].name in {"rename", "rollback"}
            if missing_revision or missing_body:
                return JSONResponse({"detail": "缺少版本条件，请刷新页面后重试", "code": "precondition"}, status_code=428)
        return JSONResponse({"detail": "请求参数不合法，请检查字段类型、长度与配置 ID"}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        messages = {400: "请求格式不正确", 404: "不存在", 405: "请求方法不允许", 413: "请求超过大小限制"}
        return JSONResponse({"detail": messages.get(exc.status_code, "请求失败")}, status_code=exc.status_code)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        token = csrf.issue(request.state.identity["sub"])
        return templates.TemplateResponse(request=request, name="index.html", context={"csrf_token": token, "max_mib": settings.max_upload_bytes // 1048576})

    @app.get("/api/csrf")
    async def refresh_csrf(request: Request):
        return {"csrf_token": csrf.issue(request.state.identity["sub"])}

    @app.get("/healthz", include_in_schema=False)
    async def health():
        store = app.state.store
        ok = store.ready and not store.failed
        return JSONResponse({"status": "ok" if ok else "recovery_required"}, status_code=200 if ok else 503)

    @app.get("/api/status")
    async def status():
        store = app.state.store
        return {"ready": store.ready and not store.failed, "max_upload_bytes": settings.max_upload_bytes, "history_versions": settings.history_versions, "snapshotting": store.snapshotting}

    @app.get("/api/configs", response_model=list[ConfigView])
    async def configs():
        return await asyncio.to_thread(app.state.store.list)

    async def upload(request, config_id=None, expected_current_version=None):
        async with request.form(max_files=1, max_fields=2, max_part_size=65536) as form:
            if len(form.getlist("file")) != 1 or not isinstance(form.get("file"), UploadFile) or any(k not in {"file", "name", "description"} for k in form):
                raise AppError(422, "请提交一个名为 file 的 YAML 文件")
            file = form["file"]
            if config_id and any(k != "file" for k in form):
                raise AppError(422, "替换内容只接受 file 字段；修改名称或说明请使用元数据接口")
            # python-multipart strips IE-style Windows paths. Validate the raw
            # header too, before treating the normalized filename as metadata.
            disposition = Message()
            disposition["content-disposition"] = file.headers.get("content-disposition", "")
            original_name = disposition.get_param("filename", header="content-disposition")
            if isinstance(original_name, tuple):
                original_name = collapse_rfc2231_value(original_name)
            if original_name is not None:
                check_filename_chars(original_name)
            filename = filename_valid(file.filename)
            name = form.get("name", Path(filename).stem.strip()[:100] or filename.strip()[:100] or "未命名配置")
            description = form.get("description", "")
            if not isinstance(name, str) or not isinstance(description, str) or len(description) > 2000:
                raise AppError(422, "名称或说明格式错误，说明最长 2000 个字符")
            if any(ord(c) == 0 for c in description):
                raise AppError(422, "说明不能包含 NUL")
            store = app.state.store
            fd, name_on_disk = tempfile.mkstemp(prefix="upload-", dir=store.incoming)
            path = Path(name_on_disk)
            try:
                with os.fdopen(fd, "wb") as output:
                    total = 0
                    while chunk := await file.read(65536):
                        total += len(chunk)
                        if total > settings.max_upload_bytes:
                            raise AppError(413, "文件超过大小限制")
                        output.write(chunk)
                await asyncio.to_thread(validate_yaml, path, settings)
                if config_id:
                    return await asyncio.to_thread(store.replace, str(config_id), path, filename, expected_current_version=expected_current_version)
                return await asyncio.to_thread(store.create, path, filename, name, description)
            finally:
                path.unlink(missing_ok=True)

    @app.post("/api/configs", status_code=201, response_model=ConfigView, openapi_extra=UPLOAD_SCHEMA)
    async def create(request: Request):
        return await upload(request)

    @app.get("/api/configs/{config_id}", response_model=ConfigView)
    async def get(config_id: UUID):
        return await asyncio.to_thread(app.state.store.get, str(config_id))

    @app.post("/api/configs/{config_id}/subscription-link")
    async def subscription_link(config_id: UUID):
        url = await asyncio.to_thread(app.state.store.subscription_url, str(config_id))
        return {"subscription_url": url}

    @app.get("/api/configs/{config_id}/preview", response_class=Response)
    async def preview(config_id: UUID):
        data = await asyncio.to_thread(app.state.store.preview, str(config_id))
        return Response(data, media_type="text/plain; charset=utf-8")

    @app.put("/api/configs/{config_id}/content", response_model=ConfigView, openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}}}}}})
    async def replace(config_id: UUID, request: Request, expected_current_version: int = Query(gt=0)):
        return await upload(request, config_id, expected_current_version)

    @app.patch("/api/configs/{config_id}", response_model=ConfigView)
    async def rename(config_id: UUID, values: Rename):
        return await asyncio.to_thread(app.state.store.rename, str(config_id), values.name, values.description, values.expected_updated_at)

    @app.post("/api/configs/{config_id}/enable", response_model=ConfigView)
    async def enable(config_id: UUID, expected_updated_at: str):
        return await asyncio.to_thread(app.state.store.change, str(config_id), "enable", expected_updated_at)

    @app.post("/api/configs/{config_id}/disable", response_model=ConfigView)
    async def disable(config_id: UUID, expected_updated_at: str):
        return await asyncio.to_thread(app.state.store.change, str(config_id), "disable", expected_updated_at)

    @app.post("/api/configs/{config_id}/rotate-token", response_model=ConfigView)
    async def rotate(config_id: UUID, expected_updated_at: str):
        return await asyncio.to_thread(app.state.store.change, str(config_id), "rotate-token", expected_updated_at)

    @app.delete("/api/configs/{config_id}", status_code=204)
    async def delete(config_id: UUID, expected_updated_at: str):
        await asyncio.to_thread(app.state.store.change, str(config_id), "delete", expected_updated_at)
        return Response(status_code=204)

    @app.get("/api/configs/{config_id}/versions")
    async def versions(config_id: UUID):
        return await asyncio.to_thread(app.state.store.versions, str(config_id))

    @app.get("/api/configs/{config_id}/versions/{version}/preview")
    async def historical_preview(config_id: UUID, version: int):
        data = await asyncio.to_thread(app.state.store.version_preview, str(config_id), version)
        return Response(data, media_type="text/plain; charset=utf-8")

    @app.post("/api/configs/{config_id}/versions/{version}/rollback", response_model=ConfigView)
    async def rollback(config_id: UUID, version: int, values: Rollback):
        return await asyncio.to_thread(app.state.store.rollback, str(config_id), version, values.expected_current_version)

    @app.post("/api/backups", status_code=202)
    async def backup_create(values: BackupCreate):
        return app.state.backups.create(values.password.get_secret_value())

    @app.get("/api/backups")
    async def backups():
        return await asyncio.to_thread(app.state.backups.list)

    @app.get("/api/backups/{backup_id}")
    async def backup_get(backup_id: UUID):
        return await asyncio.to_thread(app.state.backups.get, str(backup_id))

    def verified_archive(backup_id: UUID):
        with app.state.backups.download(str(backup_id)) as archive:
            yield archive

    @app.get("/api/backups/{backup_id}/download")
    @app.head("/api/backups/{backup_id}/download")
    async def backup_download(backup_id: UUID, request: Request, verified=Depends(verified_archive)):
        archive, row = verified
        size = row["size"]
        etag = f'"{row["sha256"]}"'
        headers = {"Content-Disposition": f'attachment; filename="{backup_id}.mcmbackup"',
                   "Accept-Ranges": "bytes", "ETag": etag, "Content-Type": "application/octet-stream"}
        selected = None
        if request.headers.get("if-range", etag) == etag:
            try:
                selected = archive_range(request.headers.get("range", ""), size)
            except ValueError:
                headers["Content-Range"] = f"bytes */{size}"
                return Response(status_code=416, headers=headers)
        start, end = selected if selected is not None else (0, size - 1)
        status = 206 if selected is not None else 200
        if selected is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = end - start + 1
        headers["Content-Length"] = str(length)
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)
        return StreamingResponse(archive_chunks(archive, start, length), status_code=status, headers=headers)

    @app.delete("/api/backups/{backup_id}", status_code=204)
    async def backup_delete(backup_id: str):
        await asyncio.to_thread(app.state.backups.delete, str(backup_id))
        return Response(status_code=204)

    def authenticated_openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
            schemes.update({
                "AccessJWT": {"type": "apiKey", "in": "header", "name": "Cf-Access-Jwt-Assertion"},
                "CSRFHeader": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token"},
            })
            schema["security"] = [{"AccessJWT": []}]
            for path in schema["paths"].values():
                for method, operation in path.items():
                    if method in {"post", "put", "patch", "delete"}:
                        operation["security"] = [{"AccessJWT": [], "CSRFHeader": []}]
                        operation.setdefault("parameters", []).append({"name": "Origin", "in": "header", "required": True, "schema": {"type": "string", "enum": [settings.origin]}})
            app.openapi_schema = schema
        return app.openapi_schema
    app.openapi = authenticated_openapi
    return app
