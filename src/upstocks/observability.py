"""Structured, rotating logs with request correlation and credential redaction."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import traceback

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
SENSITIVE_KEY = re.compile(r"(?i)(access.?token|refresh.?token|request.?token|app.?token|dashboard.?token|csrf.?token|session.?token|api.?key|api.?secret|password|authorization|cookie|checksum|secret)")


def clean(value):
    """Redact recursively before saving structured evidence or exporting it."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return redact(value) if isinstance(value, str) else value


def exception_details(exc):
    """Capture locations and messages, never source lines or frame locals."""
    result, seen = [], set()
    while exc is not None and id(exc) not in seen and len(result) < 5:
        seen.add(id(exc))
        result.append({"type": type(exc).__name__, "message": redact(str(exc))[:2000],
                       "frames": [{"file": f.filename, "line": f.lineno, "function": f.name}
                                  for f in traceback.extract_tb(exc.__traceback__)[-30:]]})
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return result


def redact(value: str) -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if token:
        value = value.replace(token, "[REDACTED]")
    keys = r"(?:access_token|refresh_token|request_token|api_key|api_secret|app_token|dashboard_token|csrf_token|session_token|password|secret|authorization|cookie|checksum)"
    value = re.sub(rf"(?i)({keys}[\"']?\s*[:=]\s*)([\"'])(.*?)\2",
                   lambda match: match[1] + match[2] + "[REDACTED]" + match[2], value)
    value = re.sub(r"(?i)Bearer\s+[^\s\"',;]+", "Bearer [REDACTED]", value)
    return re.sub(rf"(?i)({keys}[\"']?\s*[:=]\s*)(?![\"'])[^\s\"',;}}]+",
                  r"\1[REDACTED]", value)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname, "logger": record.name,
                "message": redact(record.getMessage())}
        rid = getattr(record, "request_id", None) or request_id.get()
        if rid:
            data["request_id"] = rid
        for key in ("method", "route", "status_code", "duration_ms", "job_id", "preset", "trade_id"):
            if hasattr(record, key):
                value = getattr(record, key)
                data[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            data["exception"] = exception_details(record.exc_info[1])
        return json.dumps(data, ensure_ascii=False)


def configure_logging(settings):
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("upstocks")
    logger.setLevel(settings.log_level)
    logger.propagate = False
    access = logging.getLogger("upstocks.access")
    access.setLevel(logging.INFO)
    access.propagate = False
    handlers = []
    for owner, filename, level in ((logger, "application.jsonl", settings.log_level),
                                   (logger, "error.jsonl", logging.ERROR),
                                   (access, "access.jsonl", logging.INFO)):
        handler = RotatingFileHandler(settings.log_dir / filename, maxBytes=settings.log_max_bytes,
                                      backupCount=settings.log_backups, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        handler.setLevel(level)
        owner.addHandler(handler)
        handlers.append((owner, handler))
        if owner is logger:
            server = logging.getLogger("uvicorn.error")
            server.addHandler(handler)
            handlers.append((server, handler))
    return handlers


def close_logging(handlers):
    for owner, handler in handlers:
        owner.removeHandler(handler)
        handler.close()


SERVER_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": JsonFormatter}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "loggers": {
        "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
        "uvicorn.access": {"handlers": [], "propagate": False},
    },
}
