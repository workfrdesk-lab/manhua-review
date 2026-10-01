import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "event": record.getMessage(),
                "request_id": getattr(record, "request_id", None),
                "job_id": getattr(record, "job_id", None),
                "user_id": getattr(record, "user_id", None),
                "project_id": getattr(record, "project_id", None),
                "stage": getattr(record, "stage", None),
                "status": getattr(record, "status", None),
                "error": getattr(record, "error", None),
                "duration_ms": getattr(record, "duration_ms", None),
            },
            ensure_ascii=False,
        )


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
