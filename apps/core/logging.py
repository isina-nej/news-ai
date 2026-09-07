import json
import logging
import re

SECRET_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|bot[_-]?token|password|session|cookie|authorization).{0,5}[:=]\s*\S+"
    ),
]

REDACTED = r"\1=[REDACTED]"


class RedactFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        for pat in SECRET_PATTERNS:
            msg = pat.sub("[REDACTED]", msg)
        record.msg = msg
        record.args = ()
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
                "correlation_id": getattr(record, "correlation_id", None),
                "job_id": getattr(record, "job_id", None),
            }
        )
