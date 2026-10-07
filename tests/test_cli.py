from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from codex_tools import cli, structured


class CliTests(unittest.TestCase):
    def test_app_server_command_reports_structured_connection_rename(self) -> None:
        error = io.StringIO()

        with redirect_stderr(error):
            result = cli.main(["app-server", "status"])

        self.assertEqual(result, 2)
        self.assertIn("structured connection", error.getvalue())

    def test_structured_connection_delegates_to_connection_manager(self) -> None:
        with patch.object(structured.app_server, "main", return_value=7) as main:
            result = structured.main(["connection", "status", "--connection", "work"])

        self.assertEqual(result, 7)
        main.assert_called_once_with(
            ["status", "--connection", "work"],
            prog="codex-tools structured connection",
        )


if __name__ == "__main__":
    unittest.main()
