"""Runtime inspection must work before application dependencies or a database exist."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zhiwiki.cli import main
from zhiwiki.diagnostics import environment_report


class DiagnosticsTests(unittest.TestCase):
    def test_doctor_does_not_create_database_or_load_server(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "absent-workspace"
            output = io.StringIO()
            with patch("zhiwiki.cli.KnowledgeStore", side_effect=AssertionError("Must not open a database")):
                with contextlib.redirect_stdout(output):
                    code = main(["--data-dir", str(data_dir), "doctor"])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(report["core_ready"])
            self.assertFalse(data_dir.exists())

    def test_missing_dependencies_are_reported_without_importing_them(self):
        with patch("zhiwiki.diagnostics.importlib.util.find_spec", return_value=None):
            report = environment_report()
        self.assertFalse(report["server_dependencies_ready"])
        self.assertIn("fastapi", report["missing"])
        self.assertTrue(all(item["version"] is None for item in report["packages"].values()))

    def test_old_pydantic_is_not_reported_as_server_ready(self):
        with patch("zhiwiki.diagnostics.importlib.util.find_spec", return_value=object()):
            with patch("zhiwiki.diagnostics.metadata.version", return_value="1.10.0"):
                report = environment_report()
        self.assertEqual(report["missing"], [])
        self.assertFalse(report["server_dependencies_ready"])


if __name__ == "__main__":
    unittest.main()
