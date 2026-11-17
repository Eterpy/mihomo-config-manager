"""Disposable localhost UI preview. This module is absent from production images."""
import tempfile
from pathlib import Path

import uvicorn
from starlette.responses import Response

from app.auth import AccessVerifier
from app.main import create_app
from app.services import Store
from app.settings import Settings
from tests.helpers import TestKeys


def main():
    keys = TestKeys()
    with tempfile.TemporaryDirectory(prefix="mcm-preview-") as directory:
        settings = Settings(Path(directory), "test.cloudflareaccess.com", "test-aud", admin_hostname="127.0.0.1:18089", public_base_url="https://cfg.example.invalid", admin_scheme="http")
        store = Store(settings)
        source = Path(directory) / "fixture.yaml"
        examples = [("日常使用", "daily.yaml", "日常浏览与工作使用的主要配置", True), ("家庭网络", "home.yml", "家中设备共用的网络配置", True), ("测试配置", "testing.yaml", "保留用于验证新规则", False)]
        for name, filename, description, enabled in examples:
            source.write_bytes(b"# local test fixture\nproxies: []\nrules:\n  - MATCH,DIRECT\n")
            item = store.create(source, filename, name, description)
            if not enabled:
                store.change(item["id"], "disable")
        store.close()
        async def fetch():
            return keys.jwks()
        application = create_app(settings, AccessVerifier(settings, fetcher=fetch))
        async def preview_app(scope, receive, send):
            if scope["type"] == "http":
                if not scope.get("client") or scope["client"][0] != "127.0.0.1":
                    return await Response(status_code=404)(scope, receive, send)
                scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"cf-access-jwt-assertion"] + [(b"cf-access-jwt-assertion", keys.token(settings).encode())]
            await application(scope, receive, send)
        uvicorn.run(preview_app, host="127.0.0.1", port=18089, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
