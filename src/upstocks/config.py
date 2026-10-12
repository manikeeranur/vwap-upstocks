"""Validated runtime configuration; importing the package has no I/O side effects."""
from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppSettings:
    database_path: Path = ROOT / "data" / "upstocks.sqlite3"
    log_dir: Path = ROOT / "logs"
    cache_dir: Path = ROOT / ".cache"
    legacy_dir: Path = ROOT
    host: str = "127.0.0.1"
    port: int = 3400
    scanner_enabled: bool = True
    max_backtests: int = 2
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backups: int = 5
    sqlite_timeout: float = 10.0
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

    def __post_init__(self):
        for name in ("database_path", "log_dir", "cache_dir", "legacy_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser().resolve())
        if not 1 <= self.port <= 65535 or not 1 <= self.max_backtests <= 8:
            raise ValueError("Invalid port or max_backtests (1–8)")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid UPSTOCKS_LOG_LEVEL")
        if self.log_max_bytes < 1024 or self.log_backups < 1 or self.sqlite_timeout <= 0:
            raise ValueError("Log limits and SQLite timeout must be positive")
        if not self.allowed_hosts or any(not host or "*" in host or "/" in host for host in self.allowed_hosts):
            raise ValueError("Specify explicit dashboard hostnames")

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")

        def path(name, default):
            value = Path(os.getenv(name, str(default))).expanduser()
            return value if value.is_absolute() else ROOT / value

        enabled = os.getenv("UPSTOCKS_SCANNER_ENABLED", "true").lower()
        if enabled not in {"true", "false", "1", "0"}:
            raise ValueError("UPSTOCKS_SCANNER_ENABLED must be true or false")
        return cls(
            database_path=path("UPSTOCKS_DATABASE_PATH", cls.database_path),
            log_dir=path("UPSTOCKS_LOG_DIR", cls.log_dir),
            cache_dir=path("UPSTOCKS_CACHE_DIR", cls.cache_dir),
            legacy_dir=path("UPSTOCKS_LEGACY_DIR", cls.legacy_dir),
            host=os.getenv("UPSTOCKS_HOST", cls.host),
            port=int(os.getenv("UPSTOCKS_PORT", cls.port)),
            scanner_enabled=enabled in {"true", "1"},
            max_backtests=int(os.getenv("UPSTOCKS_MAX_BACKTESTS", cls.max_backtests)),
            log_level=os.getenv("UPSTOCKS_LOG_LEVEL", cls.log_level).upper(),
            log_max_bytes=int(os.getenv("UPSTOCKS_LOG_MAX_BYTES", cls.log_max_bytes)),
            log_backups=int(os.getenv("UPSTOCKS_LOG_BACKUPS", cls.log_backups)),
            allowed_hosts=tuple(h.strip().lower() for h in os.getenv("UPSTOCKS_ALLOWED_HOSTS", ",".join(cls.allowed_hosts)).split(",")),
        )
