"""Find Codex in stripped-down SSH environments without running shell startup files."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve(command: str = "codex") -> str:
    expanded = os.path.expanduser(command)
    found = shutil.which(expanded)
    if found:
        # Preserve symlinks: version-manager shims can depend on their name/path.
        return str(Path(found).absolute())
    if command == "codex":
        candidates = [Path.home() / directory / "codex" for directory in
                      (".local/bin", ".volta/bin", "bin", ".cargo/bin", ".npm-global/bin")]
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        # The managed native CLI needs no Node/NVM shell setup and tracks the
        # installed daemon via its stable `current` link.
        candidates.append(codex_home / "packages/app-server-daemon/current/bin/codex")
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.absolute())
    # Keep read-only relay usage possible if Codex is not installed. A send
    # attempt will return an actionable server error, not "agent gone".
    return expanded
