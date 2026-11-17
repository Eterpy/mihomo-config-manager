"""Optional local Nginx verification; does not replace Docker verification."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization

from app.services import Store
from app.settings import Settings
from tests.helpers import TestKeys


def main():
    binary = os.environ["NGINX_BIN"]
    with tempfile.TemporaryDirectory(prefix="mcm-native-") as temporary:
        root = Path(temporary).resolve()
        (root / "logs").mkdir()
        (root / "tmp").mkdir()
        settings = Settings(root / "data", "test.cloudflareaccess.com", "test")
        Store(settings).close()
        keyfile = root / "key.pem"
        keyfile.write_bytes(TestKeys().private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        keyfile.chmod(0o600)
        text = Path("nginx/nginx.conf").read_text()
        text = text.replace("/tmp/", str(root / "tmp") + "/").replace("/published/", str(root / "data/published") + "/")
        text = text.replace("listen 8080 default_server;", "listen 127.0.0.1:18092 default_server;").replace("listen 127.0.0.1:8081;", "listen 127.0.0.1:18093;")
        config = root / "nginx.conf"
        config.write_text(text)
        command = [binary, "-p", str(root), "-c", str(config)]
        subprocess.run(command + ["-t"], check=True)
        env = dict(os.environ, MCM_TEST_DATA_DIR=str(settings.data_dir), MCM_TEST_KEY_PATH=str(keyfile), MCM_CONTAINER_TEST="1", MCM_TEST_ADMIN_URL="http://127.0.0.1:18091", MCM_TEST_PUBLIC_HOST="127.0.0.1", MCM_TEST_PUBLIC_PORT="18092")
        processes = []
        try:
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.test_server:create_test_app", "--factory", "--host", "127.0.0.1", "--port", "18091", "--no-access-log", "--no-proxy-headers"], env=env))
            processes.append(subprocess.Popen(command + ["-g", "daemon off;"]))
            with httpx.Client(trust_env=False, timeout=1) as client:
                for _ in range(100):
                    if any(p.poll() is not None for p in processes):
                        raise RuntimeError("测试进程启动失败")
                    try:
                        if client.get("http://127.0.0.1:18091/healthz").status_code == 200 and client.get("http://127.0.0.1:18093/healthz").status_code == 204:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("本地测试服务启动超时")
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "container"], env=env)
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
