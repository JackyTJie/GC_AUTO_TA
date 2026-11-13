"""Offline regression tests for Canvas bulk writeback responses."""

import json
import contextlib
import io
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from requests import Response
from requests import Request
from requests.exceptions import ReadTimeout

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canvasapi.assignment import Assignment
from canvasapi.requester import Requester
from gc_auto_ta.canvas_api import CanvasGateway
from gc_auto_ta.config import Config, DEFAULTS
from gc_auto_ta.util import RateLimiter
from gc_auto_ta.util import setup_logging, trace_http_response
from gc_auto_ta.writeback import build_comment


class WritebackCommentTests(unittest.TestCase):
    def test_default_and_fallback_comments_only_include_score(self):
        for writeback in [DEFAULTS["writeback"], {}, {"comment_template": ""}]:
            cfg = Config(path=Path("config.yaml"), project_root=Path.cwd(), raw={
                "grading": {"max_score": 60}, "writeback": writeback,
            })
            for score in [60.0, 32.5, 0.0]:
                with self.subTest(writeback=writeback, score=score):
                    row = {
                        "raw_score": score,
                        "comment": "Detailed compiler output\n" * 1000,
                        "stages": [{"name": "run", "score": score}],
                    }
                    self.assertEqual(build_comment(cfg, row, {"numeric": score}),
                                     f"【自动评分】{score}/60.0 分")

    def test_custom_comment_template_is_preserved(self):
        cfg = Config(path=Path("config.yaml"), project_root=Path.cwd(), raw={
            "writeback": {"comment_template": "{score}/{max}: {comment}"},
        })
        self.assertEqual(build_comment(cfg, {"raw_score": 50, "comment": "Details"}, {}),
                         "50/100.0: Details")


class BulkWritebackTests(unittest.TestCase):
    def setUp(self):
        self.requester = Requester("https://canvas.test", "test-token-secret")
        self.gateway = CanvasGateway.__new__(CanvasGateway)
        self.gateway.course_id = 1
        self.gateway.assignment_id = 2
        self.gateway._assignment = Assignment(self.requester, {"id": 2, "course_id": 1})
        self.gateway.limiter = RateLimiter(0)
        self.grades = {10: {"posted_grade": "90", "text_comment": "Good"},
                       11: {"posted_grade": "80"}}

    def response(self, body, status=200, content_type="application/json"):
        response = Response()
        response.status_code = status
        response._content = body
        response.headers["Content-Type"] = content_type
        response.url = "https://canvas.test/api/v1/courses/1/assignments/2/submissions/update_grades"
        return response

    def progress(self, state="queued"):
        return self.response(json.dumps({"id": 42, "workflow_state": state}).encode())

    def submit(self):
        return self.gateway.post_grades_bulk(self.grades, dry_run=False,
                                             poll_interval=0, poll_timeout=0.05)

    def test_success_preserves_form_encoding_and_requests_json(self):
        with patch.object(self.requester._session, "request", side_effect=[
            self.progress(), self.progress("completed"),
        ]) as request:
            result = self.submit()
        self.assertEqual(result, {"workflow_state": "completed", "count": 2, "progress_id": 42})
        self.assertEqual(request.call_count, 2)
        post = request.call_args_list[0]
        self.assertEqual(post.args[0], "POST")
        self.assertEqual(post.kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(dict(post.kwargs["data"]), {
            "grade_data[10][posted_grade]": "90",
            "grade_data[10][text_comment]": "Good",
            "grade_data[11][posted_grade]": "80",
        })
        self.assertEqual(request.call_args_list[1].args[0], "GET")

    def test_invalid_response_is_diagnostic_and_never_reposts(self):
        for status, body, content_type in [
            (200, b"", "application/json"),
            (204, b"", "application/json"),
            (200, b"<html>test-token-secret</html>", "text/html"),
            (401, b"", "text/html"),
        ]:
            with self.subTest(status=status, body=body), patch.object(
                self.requester._session, "request",
                return_value=self.response(body, status, content_type),
            ) as request, patch("time.sleep"):
                with self.assertRaises(RuntimeError) as caught:
                    self.submit()
                detail = str(caught.exception)
                self.assertIn(f"HTTP {status}", detail)
                self.assertIn(content_type, detail)
                self.assertIn(f"{len(body)} bytes", detail)
                self.assertNotIn("test-token-secret", detail)
                self.assertEqual(request.call_count, 1)

    def test_invalid_progress_is_not_treated_as_success(self):
        for data in [{}, [], {"id": 42}, {"workflow_state": "queued"}]:
            with self.subTest(data=data), patch.object(
                self.requester._session, "request",
                return_value=self.response(json.dumps(data).encode()),
            ) as request, patch("time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "Progress"):
                    self.submit()
                self.assertEqual(request.call_count, 1)

    def test_explicit_rate_limit_rejection_can_retry(self):
        with patch.object(self.requester._session, "request", side_effect=[
            self.response(b"{}", 429), self.progress(), self.progress("completed"),
        ]) as request, patch("time.sleep"):
            self.assertEqual(self.submit()["workflow_state"], "completed")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", "POST", "GET"])

    def test_read_timeout_does_not_repost(self):
        with patch.object(self.requester._session, "request", side_effect=ReadTimeout()) as request:
            with self.assertRaises(ReadTimeout):
                self.submit()
        self.assertEqual(request.call_count, 1)

    def test_dry_run_does_not_send_requests(self):
        with patch.object(self.requester._session, "request") as request:
            self.assertEqual(self.gateway.post_grades_bulk(self.grades), {"dry_run": True, "count": 2})
        request.assert_not_called()

    def test_cli_reports_writeback_failures_and_returns_nonzero(self):
        from gc_auto_ta import cli

        class Config:
            state_db = "unused.sqlite3"
            writeback_mode = "live"

        state = unittest.mock.MagicMock()
        state.__enter__.return_value = state
        result = {"student-1": {"error": "Canvas returned HTML"},
                  "student-2": {"error": "Canvas returned HTML"}}
        output = io.StringIO()
        args = type("Args", (), {"live": True, "no_bulk": False, "only": None})()
        with patch.object(cli, "_load_cfg", return_value=Config()), \
             patch("gc_auto_ta.state.State", return_value=state), \
             patch("gc_auto_ta.writeback.run", return_value=result), \
             contextlib.redirect_stdout(output):
            code = cli.cmd_writeback(args)
        self.assertEqual(code, 1)
        self.assertIn("成功 0 人，失败 2 人", output.getvalue())
        self.assertEqual(output.getvalue().count("失败原因："), 1)
        self.assertIn("Canvas returned HTML", output.getvalue())

    def test_http_trace_is_opt_in_redacted_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "http.log"
            response = Response()
            response.status_code = 200
            response.reason = "OK"
            response.url = "https://canvas.test/api/v1/courses/1"
            response._content = b"token-secret-body"
            response.headers.update({"Content-Type": "text/html", "Set-Cookie": "session-secret"})
            response.request = Request("GET", response.url,
                                       headers={"Authorization": "Bearer token-secret"}).prepare()

            try:
                setup_logging(level="info", file=path, secrets=["token-secret"],
                              max_response_bytes=11)
                trace_http_response(response)
                self.assertEqual(path.read_text(encoding="utf-8"), "")

                setup_logging(level="trace", file=path, secrets=["token-secret"],
                              max_response_bytes=11)
                trace_http_response(response)
                for handler in logging.getLogger("gc_auto_ta").handlers:
                    handler.flush()
                output = path.read_text(encoding="utf-8")
            finally:
                setup_logging(level="info")
            self.assertIn('"status":200', output)
            self.assertIn('"Content-Type":"text/html"', output)
            self.assertIn('"body_truncated":true', output)
            self.assertIn("<REDACTED>", output)
            self.assertNotIn("token-secret", output)
            self.assertNotIn("session-secret", output)
            self.assertNotIn("Set-Cookie", output)

    def test_file_redirects_all_application_log_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "application.log"
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    setup_logging(level="trace", file=path)
                    logger = logging.getLogger("gc_auto_ta")
                    logger.debug("debug message")
                    logger.info("info message")
                    logger.warning("warning message")
                    logger.error("error message")
                    logging.getLogger("gc_auto_ta.http").log(5, "trace message")
                setup_logging(level="info")
                output = path.read_text(encoding="utf-8")
                for level in ("debug", "info", "warning", "error", "trace"):
                    self.assertEqual(output.count(f"{level} message"), 1)
                self.assertEqual(stderr.getvalue(), "")
                with contextlib.redirect_stderr(stderr):
                    setup_logging(level="info")
                    logging.getLogger("gc_auto_ta").info("stderr message")
                self.assertIn("stderr message", stderr.getvalue())
                self.assertNotIn("stderr message", path.read_text(encoding="utf-8"))
            finally:
                setup_logging(level="info")

    def test_log_file_config_is_optional_and_relative_to_config(self):
        from gc_auto_ta.config import Config

        cfg = Config(path=Path("D:/example/config.yaml"), project_root=Path("D:/example"))
        self.assertIsNone(cfg.log_file)
        cfg.raw["logging"] = {"file": "logs/application.log"}
        self.assertEqual(cfg.log_file, Path("D:/example/logs/application.log"))


if __name__ == "__main__":
    unittest.main()
