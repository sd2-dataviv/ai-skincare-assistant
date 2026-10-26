"""Logging with PII redaction (NFR-06): no phone numbers, emails or OTPs reach log output."""
import logging
import re

_PATTERNS = [
    (re.compile(r"\+?\d[\d\s-]{8,}\d"), "[redacted-number]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[redacted-email]"),
    (re.compile(r"\b\d{6}\b"), "[redacted-code]"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        record.msg = redact(msg)
        record.args = ()
        if record.exc_info:
            # Tracebacks can carry SQL parameters or request data: redact them too.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn access logs carry paths only (no bodies); route them through the redacting handler too.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False
