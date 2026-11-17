from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_compose_isolation():
    path = ROOT / "compose.yaml"
    if not path.exists():
        pytest.skip("deployment manifests are checked on host")
    services = yaml.safe_load(path.read_text())["services"]
    assert services["admin"]["ports"] == ["127.0.0.1:18081:8000"]
    assert services["public"]["ports"] == ["127.0.0.1:18080:8080"]
    public = services["public"]
    assert "@sha256:" in public["image"]
    assert {v["target"] for v in public["volumes"]} == {"/published", "/etc/nginx/nginx.conf"}
    assert all(v["read_only"] for v in public["volumes"])
    for item in services.values():
        assert item["read_only"] and item["cap_drop"] == ["ALL"]
        assert item["user"].split(":")[0] != "0"
    test_services = yaml.safe_load((ROOT / "compose.test.yaml").read_text())["services"]
    for item in test_services.values():
        assert not item.get("ports")
        assert not item.get("env_file")
        for entry in item.get("tmpfs", []):
            assert entry.startswith("/tmp:") and "mode=" in entry


def test_build_context_excludes_profiles_and_secrets():
    path = ROOT / ".dockerignore"
    if not path.exists():
        pytest.skip("build context rules are checked on host")
    patterns = set(path.read_text().split())
    assert {"**/*.yaml", "**/*.yml", "**/*.mcmbackup", "**/.env", "**/.env.*"} <= patterns


def test_frontend_uses_text_nodes():
    js = (ROOT / "app/static/app.js").read_text()
    assert "innerHTML" not in js and "eval(" not in js
    assert '$("#preview-content").textContent' in js
