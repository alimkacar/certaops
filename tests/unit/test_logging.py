"""Yapilandirilmis loglama: baglam, maskeleme ve dairesel tampon."""

from __future__ import annotations

import json
import logging

import pytest

from robotics_agent.observability import context as log_ctx
from robotics_agent.observability.logging import (
    JsonFormatter,
    RingBufferHandler,
    TextFormatter,
    configure_logging,
    get_log_buffer,
)


@pytest.fixture(autouse=True)
def _isolated_logging():
    """Her test kendi loglama kurulumuyla calisir ve arkasini toplar."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    log_ctx.clear()
    yield
    log_ctx.clear()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def _record(message: str, *args, level: int = logging.INFO, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="robotics_agent.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=exc_info,
    )


# --- Baglam ----------------------------------------------------------------
def test_context_is_scoped_and_restored():
    with log_ctx.log_context(correlation_id="corr-1", tenant="100"):
        assert log_ctx.current_context() == {"correlation_id": "corr-1", "tenant": "100"}
        with log_ctx.log_context(tenant="200"):
            assert log_ctx.current_context()["tenant"] == "200"
        assert log_ctx.current_context()["tenant"] == "100"
    assert log_ctx.current_context() == {}


def test_empty_value_does_not_erase_existing_field():
    """Eksik bir parametre dogru olan bir correlation ID'yi silmemeli."""
    with log_ctx.log_context(correlation_id="corr-1"):
        with log_ctx.log_context(correlation_id=""):
            assert log_ctx.current_context()["correlation_id"] == "corr-1"


def test_unknown_context_field_fails_loudly():
    """Yazim hatasi sessizce kaybolmaz; eksik alan olay incelemesini kirar."""
    with pytest.raises(log_ctx.UnknownContextField):
        log_ctx.bind(tenat="100")


# --- Maskeleme -------------------------------------------------------------
def test_text_formatter_masks_message_but_not_timestamp():
    formatter = TextFormatter(mask=True)
    line = formatter.format(_record("musteri %s iban %s", "ali@firma.test", "TR330006100519786457841326"))
    assert "ali@firma.test" not in line
    assert "a***@firma.test" in line
    assert "TR33***" in line
    # Saat damgasi maskelenmemeli; aksi halde loglar okunamaz hale gelir.
    assert line.split(" | ")[0].count(":") == 2
    assert "***" not in line.split(" | ")[0]


def test_masking_can_be_disabled():
    formatter = TextFormatter(mask=False)
    assert "ali@firma.test" in formatter.format(_record("musteri ali@firma.test"))


def test_formatting_does_not_mutate_the_shared_record():
    """Kayit handler'lar arasinda paylasilir; maskeleme kalici olmamali."""
    record = _record("musteri %s", "ali@firma.test")
    TextFormatter(mask=True).format(record)
    assert record.msg == "musteri %s"
    assert record.args == ("ali@firma.test",)
    assert "ali@firma.test" in TextFormatter(mask=False).format(record)


def test_traceback_is_masked():
    try:
        raise ValueError("iletisim: ali@firma.test")
    except ValueError:
        import sys

        record = _record("patladi", level=logging.ERROR, exc_info=sys.exc_info())
    line = TextFormatter(mask=True).format(record)
    assert "ali@firma.test" not in line
    assert "ValueError" in line


def test_text_formatter_puts_context_before_traceback():
    """Cok satirli hata kaydinda correlation ID en alta dusup kaybolmamali."""
    try:
        raise ValueError("bozuldu")
    except ValueError:
        import sys

        record = _record("patladi", level=logging.ERROR, exc_info=sys.exc_info())
    with log_ctx.log_context(correlation_id="corr-9"):
        line = TextFormatter().format(record)
    assert "correlation_id=corr-9" in line.split("\n")[0]


# --- JSON ------------------------------------------------------------------
def test_json_formatter_emits_single_line_with_context():
    with log_ctx.log_context(correlation_id="corr-2", tenant="100", channel="api"):
        line = JsonFormatter().format(_record("islem tamam"))
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["message"] == "islem tamam"
    assert payload["correlation_id"] == "corr-2"
    assert payload["tenant"] == "100"
    assert payload["channel"] == "api"
    assert payload["level"] == "INFO"


def test_explicit_extra_overrides_context_var():
    record = _record("islem")
    record.tenant = "999"
    with log_ctx.log_context(tenant="100"):
        payload = json.loads(JsonFormatter().format(record))
    assert payload["tenant"] == "999"


# --- Dairesel tampon --------------------------------------------------------
def test_ring_buffer_keeps_only_the_last_entries():
    handler = RingBufferHandler(capacity=3)
    for index in range(10):
        handler.emit(_record(f"kayit-{index}"))
    rows = handler.snapshot(limit=10)
    assert [r["message"] for r in rows] == ["kayit-9", "kayit-8", "kayit-7"]


def test_ring_buffer_filters_by_level():
    handler = RingBufferHandler(capacity=10)
    handler.emit(_record("bilgi", level=logging.INFO))
    handler.emit(_record("uyari", level=logging.WARNING))
    rows = handler.snapshot(limit=10, min_level=logging.WARNING)
    assert [r["message"] for r in rows] == ["uyari"]


def test_ring_buffer_masks_entries():
    handler = RingBufferHandler(capacity=5)
    handler.emit(_record("musteri ali@firma.test"))
    assert "ali@firma.test" not in handler.snapshot()[0]["message"]


def test_ring_buffer_has_no_stream_attribute():
    """MCP stdio tasimasi stdout'a dusen handler'lari stderr'e cevirir.

    Tampon handler'inin bir akisi yoktur; o dongunun kapsamina girmemeli.
    """
    assert not hasattr(RingBufferHandler(), "stream")


# --- Kurulum ----------------------------------------------------------------
def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    configure_logging(level="INFO", buffer_size=10)
    first = len(root.handlers)
    configure_logging(level="INFO", buffer_size=10)
    assert len(root.handlers) == first


def test_configure_logging_keeps_foreign_handlers():
    """pytest'in caplog handler'i veya operatorun ekledigi handler silinmez."""
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        configure_logging(level="INFO", buffer_size=10)
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)


def test_configure_logging_wires_context_into_the_buffer():
    configure_logging(level="INFO", buffer_size=10)
    log = logging.getLogger("robotics_agent.test")
    with log_ctx.log_context(correlation_id="corr-3", tenant="100"):
        log.info("tur tamamlandi")
    entry = get_log_buffer().snapshot(limit=1)[0]
    assert entry["correlation_id"] == "corr-3"
    assert entry["tenant"] == "100"


def test_buffer_can_be_disabled():
    configure_logging(level="INFO", buffer_size=0)
    assert get_log_buffer() is None


def test_log_file_is_written_as_json(tmp_path):
    path = tmp_path / "agent.log"
    configure_logging(level="INFO", buffer_size=0, log_file=path)
    logging.getLogger("robotics_agent.test").info("dosyaya yazildi")
    logging.shutdown()
    payload = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert payload["message"] == "dosyaya yazildi"
