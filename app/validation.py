import json
import re
import subprocess
import sys
from pathlib import Path

from .errors import AppError
from .settings import Settings


def filename_valid(name: str):
    if not name or len(name) > 255 or name in {".", ".."} or any(c in name for c in "/\\:") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise AppError(422, "文件名不合法，不得包含路径或控制字符")
    if Path(name).suffix.lower() not in {".yaml", ".yml"}:
        raise AppError(422, "只允许 .yaml 或 .yml 文件")
    return name


def display_name(name: str):
    name = name.strip()
    if not 1 <= len(name) <= 100 or re.search(r"[\x00-\x1f\x7f]", name):
        raise AppError(422, "显示名称须为 1–100 个字符，不能包含控制字符")
    return name


def validate_yaml(path: Path, settings: Settings):
    if path.stat().st_size == 0:
        raise AppError(422, "不能上传空文件")
    if path.stat().st_size > settings.max_upload_bytes:
        raise AppError(413, "文件超过大小限制")
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("yaml_worker.py")), str(path), str(settings.yaml_memory_mb)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=settings.yaml_timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise AppError(422, "YAML 校验超时，请简化配置") from None
    if result.returncode != 0:
        raise AppError(422, "YAML 复杂度超出资源限制或解析失败")
    try:
        status = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise AppError(422, "YAML 解析失败") from None
    if not status.get("ok"):
        raise AppError(422, status["message"])
