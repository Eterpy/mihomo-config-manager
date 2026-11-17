import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    team_domain: str
    audience: str
    admin_hostname: str = "admin.example.com"
    public_base_url: str = "https://subscribe.example.com"
    max_upload_bytes: int = 10 * 1024 * 1024
    history_versions: int = 5
    yaml_timeout: float = 5.0
    yaml_memory_mb: int = 256
    admin_scheme: str = "https"
    backup_max_bytes: int = 1024**3
    backup_quota_bytes: int = 2 * 1024**3

    @property
    def origin(self):
        return f"{self.admin_scheme}://{self.admin_hostname}"

    @property
    def issuer(self):
        return f"https://{self.team_domain}"

    @classmethod
    def from_env(cls):
        if os.getenv("APP_ENV", "production") not in {"production", "development"}:
            raise ValueError("APP_ENV 必须是 production 或 development")
        for key in ("DEV_AUTH_BYPASS", "AUTH_BYPASS", "MOCK_AUTH"):
            if os.getenv(key, "").lower() not in {"", "0", "false"}:
                raise ValueError("生产入口不支持认证绕过")
        team = os.getenv("CF_ACCESS_TEAM_DOMAIN", "").removeprefix("https://").rstrip("/")
        aud = os.getenv("CF_ACCESS_AUD", "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.cloudflareaccess\.com", team) or not aud or team == "your-team.cloudflareaccess.com" or aud == "replace-with-your-access-application-aud":
            raise ValueError("必须配置有效的 CF_ACCESS_TEAM_DOMAIN 和 CF_ACCESS_AUD")
        host = os.getenv("ADMIN_HOSTNAME", "").strip().lower()
        if not valid_hostname(host):
            raise ValueError("ADMIN_HOSTNAME 必须是域名，不含协议、端口或路径")
        public = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        parsed = urlsplit(public)
        if parsed.scheme != "https" or not valid_hostname(parsed.hostname or "") or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.netloc.lower() != parsed.hostname:
            raise ValueError("PUBLIC_BASE_URL 必须是 HTTPS 域名，不含路径或凭据")
        if any(name == "example.com" or name.endswith(".example.com") for name in (host, parsed.hostname)):
            raise ValueError("请将示例域名替换为自己的管理域名和订阅域名")
        if host == parsed.hostname:
            raise ValueError("管理域名和订阅域名必须不同")
        public = f"https://{parsed.hostname}"
        try:
            size = int(os.getenv("MAX_UPLOAD_MIB", "10"))
            history = int(os.getenv("HISTORY_VERSIONS", "5"))
            timeout = float(os.getenv("YAML_TIMEOUT_SECONDS", "5"))
            memory = int(os.getenv("YAML_MEMORY_MIB", "256"))
        except ValueError:
            raise ValueError("资源配置必须为数字") from None
        if not (1 <= size <= 100 and 0 <= history <= 100 and 0 < timeout <= 30 and 64 <= memory <= 1024):
            raise ValueError("资源配置超出允许范围")
        backup_max = int(os.getenv("BACKUP_MAX_MIB", "1024"))
        backup_quota = int(os.getenv("BACKUP_QUOTA_MIB", "2048"))
        if not 1 <= backup_max <= backup_quota <= 102400:
            raise ValueError("备份容量配置无效")
        return cls(Path(os.getenv("DATA_DIR", "data")).resolve(), team, aud, host, public, size * 1024 * 1024, history, timeout, memory, backup_max_bytes=backup_max * 1048576, backup_quota_bytes=backup_quota * 1048576)


def valid_hostname(value):
    return len(value) <= 253 and "." in value and all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    )

