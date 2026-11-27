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
    rules = [line for line in path.read_text().splitlines() if line and not line.startswith("#")]
    from scripts.package_release import source_files
    copy_sources = set()
    copied = set()
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            for pattern in line.split()[1:-1]:
                copy_sources.add("!" + pattern)
                matches = list(ROOT.glob(pattern))
                assert matches and all(path.is_file() for path in matches)
                copied.update(matches)
    allowed = set(source_files(ROOT))
    expected = {path for path in allowed if path.relative_to(ROOT).parts[0] in {"app", "tests", "scripts"}}
    assert copied == expected | {ROOT / name for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml")}
    assert rules[0] == "**"
    assert set(rules[1:]) == copy_sources | {"!Dockerfile"}


def test_frontend_uses_text_nodes():
    js = (ROOT / "app/static/app.js").read_text()
    assert "innerHTML" not in js and "eval(" not in js
    assert '$("#preview-content").textContent' in js
