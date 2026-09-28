from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import manager, summary_common


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

            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="summary", stderr=""
            )
            with patch.object(
                summary_common.subprocess, "run", return_value=completed
            ) as run:
                self.assertEqual(
                    summary_common.run_codex_exec(args, "prompt"), "summary"
                )
            self.assertEqual(
                run.call_args.kwargs["env"]["CODEX_HOME"],
                str(profile_home.resolve()),
            )


if __name__ == "__main__":
    unittest.main()
