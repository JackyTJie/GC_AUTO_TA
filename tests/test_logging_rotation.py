"""Regression tests for bounded application log files."""

import logging
import tempfile
import unittest
from pathlib import Path

from gc_auto_ta.config import Config
from gc_auto_ta.util import setup_logging


class LoggingRotationTests(unittest.TestCase):
    def tearDown(self):
        setup_logging(level="info")

    def test_file_rotates_at_configured_size_and_keeps_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "application.log"
            try:
                setup_logging(level="info", file=path, max_bytes=256, backup_count=2)
                logger = logging.getLogger("gc_auto_ta")
                for index in range(20):
                    logger.info("message-%02d %s", index, "x" * 60)
                for handler in logger.handlers:
                    handler.flush()

                files = [path, Path(f"{path}.1"), Path(f"{path}.2")]
                self.assertTrue(all(file.exists() for file in files))
                self.assertFalse(Path(f"{path}.3").exists())
                self.assertTrue(all(file.stat().st_size <= 256 for file in files))
                self.assertIn("message-19", path.read_text(encoding="utf-8"))
                self.assertIn("message-17", files[1].read_text(encoding="utf-8"))
            finally:
                setup_logging(level="info")

    def test_existing_full_log_rotates_on_next_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "application.log"
            path.write_text("x" * 256, encoding="utf-8")
            try:
                setup_logging(level="info", file=path, max_bytes=256, backup_count=2)
                logging.getLogger("gc_auto_ta").info("next message")
                self.assertEqual(Path(f"{path}.1").read_text(encoding="utf-8"), "x" * 256)
                self.assertIn("next message", path.read_text(encoding="utf-8"))
            finally:
                setup_logging(level="info")

    def test_size_and_backup_settings_are_available_from_config(self):
        cfg = Config(path=Path("config.yaml"), project_root=Path.cwd(), raw={
            "logging": {"max_bytes": 256, "backup_count": 2},
        })
        self.assertEqual(cfg.log_max_bytes, 256)
        self.assertEqual(cfg.log_backup_count, 2)


if __name__ == "__main__":
    unittest.main()
