"""Verify both processes use UTC console output and rotating files."""

import logging
from logging.handlers import RotatingFileHandler
import time

import pytest

from brain import logging_setup as brain_logging
from executor import logging_setup as executor_logging


@pytest.mark.parametrize("module", [brain_logging, executor_logging])
def test_logging_outputs_rotation_utc_and_repeat_setup(module, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "__file__", str(tmp_path / "process" / "logging_setup.py"))
    name = f"test-{module.__name__}"
    logger = module.setup_logging(name)
    try:
        assert module.setup_logging(name) is logger
        assert len(logger.handlers) == 2
        file_handler = next(handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler))
        assert file_handler.maxBytes == 5 * 1024 * 1024
        assert file_handler.backupCount == 5
        assert any(type(handler) is logging.StreamHandler for handler in logger.handlers)
        record = logging.LogRecord(name, logging.INFO, "", 0, "test message", (), None)
        record.created = 0
        assert file_handler.formatter.converter is time.gmtime
        assert file_handler.format(record).startswith("1970-01-01T00:00:00Z")
        logger.info("test message")
        file_handler.flush()
        assert "test message" in (tmp_path / "logs" / f"{name}.log").read_text(encoding="utf-8")
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        del logger._ict_logging_configured
