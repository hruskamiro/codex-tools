from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import codex_exec, manager, summary_common


class SummaryProfileTests(unittest.TestCase):
    def test_summary_runner_uses_managed_profile_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager_root = root / "manager"
            profile_home = root / "summary-home"
            profile_home.mkdir()
            manager.save_profile(
                manager_root,
                {
                    "name": "summaries",
                    "home": str(profile_home),
                    "conversation_store": str(root / "store"),
                    "share": "isolated",
                    "created_at": "",
                },
            )
            args = argparse.Namespace(
                codex_bin="codex",
                model=None,
                profile="summaries",
                manager_root=manager_root,
                default_home=root / "default-home",
            )

            def run_exec(**kwargs):
                command = kwargs["command"]
                response = Path(command[command.index("--output-last-message") + 1])
                response.write_text(
                    json.dumps(
                        {"title": "Daily Summary", "summary_markdown": "Summary"}
                    ),
                    encoding="utf-8",
                )
                return codex_exec.ExecResult(
                    command=command,
                    returncode=0,
                    events=[],
                    event_parse_errors=[],
                    stderr="",
                    duration_s=0.1,
                )

            with patch.object(
                summary_common.codex_exec, "run_exec", side_effect=run_exec
            ) as run:
                self.assertEqual(
                    summary_common.run_codex_exec(args, "prompt"),
                    "# Daily Summary\n\nSummary",
                )
            self.assertEqual(
                run.call_args.kwargs["env"]["CODEX_HOME"],
                str(profile_home.resolve()),
            )
            command = run.call_args.kwargs["command"]
            self.assertIn("--output-schema", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)


if __name__ == "__main__":
    unittest.main()
