"""Bridge-side log secret redaction (plan §84: "logs and spans undergo secret
redaction"). A small, literal-value redactor.

Unlike a structural redactor (which knows sensitive *field
names* and rewrites args/extra fields before interpolation), this filter
knows only a short, enumerable set of exact secret *values* — the resolved
terminal password, collected once at boot (``collect_bridge_secrets``) — and
scrubs
every literal occurrence of those values from a record's formatted message
and exception text before any handler can emit it. That is a deliberately
smaller guarantee, appropriate to this process's small, known secret set.

``install`` attaches one filter instance both to the root logger's own
handlers (which catches every record that propagates there) and directly to
a handful of logger names, as defense in depth. Root's handlers only see
uvicorn's records if uvicorn's own logging configuration is left disabled —
``main()`` passes ``log_config=None`` to ``uvicorn.run`` for exactly that
reason.
"""

import logging
from collections.abc import Iterable

_REDACTED = "[redacted]"
_WITHHELD = "[log record withheld: redaction failed]"
_EXCEPTION_FORMATTER = logging.Formatter()

UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")
DEFAULT_LOGGER_NAMES = (*UVICORN_LOGGER_NAMES, "mt5_bridge")

# Every attribute a bare LogRecord already carries, plus "message"/"asctime"
# (only populated once a Formatter renders them) -- anything else is an
# application-supplied ``extra=`` field, scrubbed the same way as the
# message text (core.log_formatting.JsonLogFormatter surfaces these).
STANDARD_LOG_RECORD_FIELDS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", (), None))
) | {"message", "asctime"}


class SecretRedactingFilter(logging.Filter):
    """Replaces literal secret values wherever they would reach a handler; never raises."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = frozenset(value for value in secrets if value)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets = self._secrets | frozenset(value for value in secrets if value)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._redact(record)
        except Exception:  # noqa: BLE001 - a redaction bug must never crash logging
            _withhold(record)
        return True

    def _redact(self, record: logging.LogRecord) -> None:
        record.msg = self._scrub(record.getMessage())
        record.args = ()
        exc_info = record.exc_info
        if exc_info is not None and exc_info[0] is not None:
            # Rendered and cleared now: a later, unfiltered formatter must
            # never get a second chance to render the raw traceback itself.
            record.exc_text = self._scrub(_EXCEPTION_FORMATTER.formatException(exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self._scrub(record.stack_info)
        for name, value in list(record.__dict__.items()):
            if name not in STANDARD_LOG_RECORD_FIELDS and isinstance(value, str):
                record.__dict__[name] = self._scrub(value)

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return text


def _withhold(record: logging.LogRecord) -> None:
    record.msg = _WITHHELD
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None


def collect_bridge_secrets(*, terminal_password: str | None) -> frozenset[str]:
    """Every literal secret value this bridge process holds at boot: the
    terminal password when the bridge logs the terminal in itself
    (``resolve_terminal_connection``)."""
    return frozenset({terminal_password}) if terminal_password else frozenset()


def install(
    *, secrets: Iterable[str], logger_names: Iterable[str] = DEFAULT_LOGGER_NAMES
) -> SecretRedactingFilter:
    """Attach one filter instance to root's own handlers and to ``logger_names``.

    Call before ``uvicorn.run(..., log_config=None)``: uvicorn's own logging
    configuration would otherwise attach its own, unfiltered handlers to its
    logger names and stop them propagating to root.
    """
    redactor = SecretRedactingFilter(secrets)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(redactor)
    root.addFilter(redactor)
    for name in logger_names:
        logging.getLogger(name).addFilter(redactor)
    return redactor


__all__ = [
    "DEFAULT_LOGGER_NAMES",
    "STANDARD_LOG_RECORD_FIELDS",
    "UVICORN_LOGGER_NAMES",
    "SecretRedactingFilter",
    "collect_bridge_secrets",
    "install",
]
