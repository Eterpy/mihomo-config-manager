import asyncio
import hashlib
import hmac
import json
import secrets
import time

import httpx
import jwt
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, JSONResponse

from .errors import AppError


class AccessVerifier:
    def __init__(self, settings, fetcher=None, ttl=300, refresh_interval=30):
        self.settings = settings
        self.fetcher = fetcher or self._fetch
        self.ttl = ttl
        self.refresh_interval = refresh_interval
        self.keys = {}
        self.expires = 0.0
        self.last_attempt = float("-inf")
        self.lock = asyncio.Lock()

    async def _fetch(self):
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", f"{self.settings.issuer}/cdn-cgi/access/certs") as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 256 * 1024:
                        raise ValueError("JWKS too large")
                return json.loads(data)

    async def _key(self, kid):
        if time.monotonic() < self.expires and kid in self.keys:
            return self.keys[kid]
        async with self.lock:
            current = time.monotonic()
            if current < self.expires and kid in self.keys:
                return self.keys[kid]
            if current - self.last_attempt < self.refresh_interval:
                if current >= self.expires:
                    raise AppError(503, "认证公钥暂时不可用，请稍后重试")
                raise AppError(401, "访问凭据无效")
            self.last_attempt = current
            try:
                document = await self.fetcher()
                keys = {}
                for item in document["keys"]:
                    if item.get("kty") == "RSA" and item.get("alg", "RS256") == "RS256" and item.get("use", "sig") == "sig" and isinstance(item.get("kid"), str):
                        keys[item["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(item))
                if not keys:
                    raise ValueError("empty JWKS")
            except Exception:
                raise AppError(503, "认证公钥暂时不可用，请稍后重试") from None
            self.keys = keys
            self.expires = time.monotonic() + self.ttl
            if kid not in keys:
                raise AppError(401, "访问凭据无效")
            return keys[kid]

    async def verify(self, token):
        if not token or len(token) > 16384:
            raise AppError(401, "请通过 Cloudflare Access 登录管理端")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or len(header["kid"]) > 256:
                raise ValueError("invalid JWT header")
            key = await self._key(header["kid"])
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.settings.audience, issuer=self.settings.issuer, leeway=60, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
            if not isinstance(claims["sub"], str) or not claims["sub"].strip():
                raise ValueError("missing identity")
            return claims
        except AppError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AppError(401, "访问凭据无效或已过期，请重新登录") from None


class CSRF:
    def __init__(self):
        self.secret = secrets.token_bytes(32)

    def issue(self, subject):
        payload = f"{int(time.time()) + 12 * 3600}.{secrets.token_hex(32)}"
        signature = hmac.new(self.secret, f"{subject}\0{payload}".encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify(self, subject, token):
        if not token or len(token) > 256:
            return False
        try:
            expires, nonce, signature = token.split(".")
            expected = hmac.new(self.secret, f"{subject}\0{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
            return int(expires) >= time.time() and hmac.compare_digest(expected, signature)
        except (ValueError, TypeError):
            return False


class SecurityBoundary:
    """Authenticate and limit the raw body before FastAPI parses multipart."""
    def __init__(self, app, settings, verifier, csrf):
        self.app, self.settings, self.verifier, self.csrf = app, settings, verifier, csrf
        self.upload_active = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.requests import Request
        request = Request(scope)
        started = False
        admitted = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"private, no-store, max-age=0"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ]
            await send(message)

        try:
            if scope["path"] == "/healthz":
                peer = scope.get("client")
                if not peer or peer[0] not in {"127.0.0.1", "::1"}:
                    raise AppError(404, "不存在")
                return await self.app(scope, receive, safe_send)
            if request.headers.get("host", "").lower() != self.settings.admin_hostname:
                raise AppError(400, "管理域名不匹配")
            identity = await self.verifier.verify(request.headers.get("cf-access-jwt-assertion"))
            scope.setdefault("state", {})["identity"] = identity
            if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("origin") != self.settings.origin:
                    raise AppError(403, "请求来源校验失败")
                if not self.csrf.verify(identity["sub"], request.headers.get("x-csrf-token")):
                    raise AppError(403, "页面凭据已失效，请刷新后重试", code="csrf")
                store = getattr(request.app.state, "store", None)
                read_link = scope["method"] == "POST" and scope["path"].endswith("/subscription-link")
                if store and store.snapshotting and not read_link:
                    raise AppError(503, "正在创建一致快照，请稍后重试")
            upload = scope["method"] == "POST" and scope["path"] == "/api/configs" or scope["method"] == "PUT" and scope["path"].endswith("/content")
            limit = self.settings.max_upload_bytes + 65536 if upload else 65536
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    size = int(length)
                    if size < 0:
                        raise ValueError()
                except ValueError:
                    raise AppError(400, "请求长度无效") from None
                if size > limit:
                    raise AppError(413, "请求超过大小限制")
            if upload:
                if self.upload_active:
                    raise AppError(429, "另一个文件正在上传，请稍后重试")
                self.upload_active = admitted = True
            consumed = 0

            async def limited_receive():
                nonlocal consumed
                message = await receive()
                if message["type"] == "http.request":
                    consumed += len(message.get("body", b""))
                    if consumed > limit:
                        raise HTTPException(413, "请求超过大小限制")
                return message

            await self.app(scope, limited_receive, safe_send)
        except AppError as exc:
            if not started:
                if exc.status in (401, 503) and scope["method"] == "GET" and not scope["path"].startswith(("/api/", "/static/")) and scope["path"] != "/openapi.json" and "text/html" in request.headers.get("accept", ""):
                    title = "登录已过期或校验未通过" if exc.status == 401 else "认证服务暂不可用"
                    response = HTMLResponse(f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{title}</title><h1>{title}</h1><p><a href="/">重新加载</a></p><p><a href="/cdn-cgi/access/logout">退出后重新登录</a></p></html>', status_code=exc.status)
                else:
                    response = JSONResponse(exc.body, status_code=exc.status, headers={"Retry-After": "2"} if exc.status == 429 else None)
                await response(scope, receive, safe_send)
        except Exception:
            # Never log arbitrary exceptions: they may contain YAML or token paths.
            if not started:
                await JSONResponse({"detail": "服务暂时不可用，请检查运行状态"}, status_code=503)(scope, receive, safe_send)
        finally:
            if admitted:
                self.upload_active = False
