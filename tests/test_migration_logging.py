"""Regression coverage for Alembic's process-wide logging configuration."""

from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path

from app.db.migrate import upgrade_database


def test_programmatic_migration_preserves_existing_application_logger(
    tmp_path: Path,
) -> None:
    logger = logging.getLogger("app.synthetic.migration_logging_probe")
    original_disabled = logger.disabled
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    output = StringIO()
    handler = logging.StreamHandler(output)
    try:
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.disabled = False

        database_path = (tmp_path / "migration-logging.db").as_posix()
        upgrade_database(f"sqlite+pysqlite:///{database_path}")

        assert not logger.disabled
        assert logging.getLogger("alembic").level == logging.INFO
        logger.info("APPLICATION_LOGGER_STILL_ENABLED")
        assert output.getvalue().strip() == "APPLICATION_LOGGER_STILL_ENABLED"
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        logger.disabled = original_disabled
