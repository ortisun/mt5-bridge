"""SecretRedactingFilter/install/collect_bridge_secrets: the bridge's own
secret redaction for logs (plan §84) — a literal-value scrub over a small,
known secret set."""

import logging

from mt5_bridge.core.log_redaction import SecretRedactingFilter, collect_bridge_secrets, install


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestSecretRedactingFilter:
    def test_a_secret_value_never_appears_in_the_formatted_message(self) -> None:
        logger = logging.getLogger("test.log_redaction.value")
        logger.setLevel(logging.INFO)
        handler = _ListHandler()
        logger.addHandler(handler)
        redactor = SecretRedactingFilter({"top-secret-password"})
        logger.addFilter(redactor)
        try:
            logger.info("connecting with password %s", "top-secret-password")
        finally:
            logger.removeHandler(handler)
            logger.removeFilter(redactor)

        assert len(handler.records) == 1
        message = handler.records[0].getMessage()
        assert "top-secret-password" not in message
        assert "[redacted]" in message

    def test_a_secret_inside_an_exception_message_is_redacted_and_exc_info_cleared(self) -> None:
        logger = logging.getLogger("test.log_redaction.exc")
        logger.setLevel(logging.INFO)
        handler = _ListHandler()
        logger.addHandler(handler)
        redactor = SecretRedactingFilter({"s3cr3t-pw"})
        logger.addFilter(redactor)
        try:
            try:
                raise RuntimeError("login failed for password s3cr3t-pw")
            except RuntimeError:
                logger.exception("terminal connect failed")
        finally:
            logger.removeHandler(handler)
            logger.removeFilter(redactor)

        record = handler.records[0]
        formatted = logging.Formatter().format(record)
        assert "s3cr3t-pw" not in formatted
        assert record.exc_info is None  # no later formatter re-renders the raw traceback

    def test_no_secrets_configured_leaves_records_untouched(self) -> None:
        redactor = SecretRedactingFilter()
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)

        assert redactor.filter(record) is True
        assert record.getMessage() == "hello world"

    def test_a_malformed_message_withholds_the_record_instead_of_crashing(self) -> None:
        redactor = SecretRedactingFilter({"s"})
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %s", ("only-one",), None)

        assert redactor.filter(record) is True
        assert record.getMessage() == "[log record withheld: redaction failed]"

    def test_add_secrets_extends_the_set_without_losing_the_original(self) -> None:
        redactor = SecretRedactingFilter({"first"})
        redactor.add_secrets({"second"})
        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "%s and %s", ("first", "second"), None
        )

        redactor.filter(record)

        message = record.getMessage()
        assert "first" not in message
        assert "second" not in message

    def test_a_secret_inside_a_string_extra_field_is_scrubbed(self) -> None:
        redactor = SecretRedactingFilter({"pw-abc"})
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "issued", (), None)
        record.__dict__["terminal_password"] = "pw-abc"
        record.__dict__["attempt"] = 3

        redactor.filter(record)

        assert record.__dict__["terminal_password"] == "[redacted]"
        assert record.__dict__["attempt"] == 3


class TestInstall:
    def test_attaches_the_same_filter_instance_to_every_named_logger(self) -> None:
        names = ("test.log_redaction.install.a", "test.log_redaction.install.b")
        redactor = install(secrets=["x"], logger_names=names)

        try:
            for name in names:
                assert redactor in logging.getLogger(name).filters
        finally:
            for name in names:
                logging.getLogger(name).removeFilter(redactor)

    def test_records_from_a_child_logger_are_redacted_via_an_ancestors_handler(self) -> None:
        # Mirrors the real wiring: install() attaches to root's *handlers*
        # (not just root's own .filters), which is what actually catches
        # records logged by a descendant that propagates up to root.
        ancestor = logging.getLogger("test.log_redaction.ancestor")
        ancestor.setLevel(logging.INFO)
        handler = _ListHandler()
        ancestor.addHandler(handler)
        child = logging.getLogger("test.log_redaction.ancestor.child")
        child.setLevel(logging.INFO)
        redactor = SecretRedactingFilter({"deep-secret"})
        handler.addFilter(redactor)
        try:
            child.info("value=%s", "deep-secret")
        finally:
            ancestor.removeHandler(handler)

        assert "deep-secret" not in handler.records[0].getMessage()


class TestCollectBridgeSecrets:
    def test_gathers_the_terminal_password(self) -> None:
        assert collect_bridge_secrets(terminal_password="term-pw") == frozenset({"term-pw"})

    def test_holds_nothing_without_a_terminal_password(self) -> None:
        assert collect_bridge_secrets(terminal_password=None) == frozenset()
        assert collect_bridge_secrets(terminal_password="") == frozenset()
