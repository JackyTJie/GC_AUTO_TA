"""Offline checks for provision push timeouts."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gc_auto_ta.config import Config
from gc_auto_ta.provision import git, push_workspace
from gc_auto_ta.util import run


class PushTimeoutTests(unittest.TestCase):
    def config(self, limits=None):
        return Config(Path("config.yaml"), Path.cwd(), raw={
            "gitea": {"url": "https://gitea.test", "org": "course"},
            "limits": limits or {},
        })

    def test_config_default_and_override(self):
        self.assertEqual(self.config().push_timeout, 120)
        self.assertEqual(self.config({"push_timeout": 2.5}).push_timeout, 2.5)
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config({"push_timeout": value}).push_timeout

    def test_only_push_gets_timeout(self):
        cfg = self.config({"push_timeout": 2.5})
        with patch("gc_auto_ta.provision.run") as command:
            git(cfg, ["push", "origin", "main"], Path.cwd())
            self.assertEqual(command.call_args.kwargs["timeout"], 2.5)
            git(cfg, ["add", "-A"], Path.cwd())
            self.assertIsNone(command.call_args.kwargs["timeout"])

    def test_hung_subprocess_times_out(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)

    def test_push_error_includes_remote_and_timeout(self):
        def command(cmd, **kwargs):
            if "push" in cmd:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("gc_auto_ta.provision.run", side_effect=command):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"https://gitea.test/course/student.git.*git push timed out after 2.5 seconds",
                ):
                    push_workspace(self.config({"push_timeout": 2.5}), Path(tmp),
                                   "student", "submission")

    def test_push_retries_after_timeout(self):
        calls = 0

        def command(cmd, **kwargs):
            nonlocal calls
            if "push" in cmd:
                calls += 1
                if calls == 1:
                    raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("gc_auto_ta.provision.run", side_effect=command):
                result = push_workspace(
                    self.config({"push_timeout": 2.5, "push_retries": 2}),
                    Path(tmp), "student", "submission")
        self.assertEqual(result, "abc123")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
