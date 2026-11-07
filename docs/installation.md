# Installation and updates

[Back to README](../README.md)

## Install from GitHub

**Linux or macOS, Python 3.10+, and pipx.** Codex Tools is installed from GitHub:

```bash
pipx install git+https://github.com/hruskamiro/codex-tools.git
```

This provides `codex-tools`, `codex-viewer`, and `codex-manager`. Viewing and
search need no model or LaTeX; model-backed summaries and tasks also need Codex CLI.
The toolkit is not yet published on PyPI.

## Install `ct` and completion

**Use `ct` for shorter commands**, with Bash completion for all tools and subcommands:

```bash
codex-tools alias install
source ~/.local/share/bash-completion/completions/codex-tools
ct viewer
```

New shells load completion when `bash-completion` is available. Add `--force`
to replace stale alias or completion files.

## Update

```bash
pipx upgrade codex-tools
codex-tools alias install --force  # refresh installed completion
codex-viewer restart              # reload an already-running viewer
```

**Configuration and pairing keys are preserved.** Restart a running relay
separately; upgrading does not start one or grant [auto-start permission](relay.md#start-the-remote-relay-automatically-over-ssh).
Use `pipx reinstall codex-tools` to rebuild from the original installation source.
For an editable installation, update its checkout before refreshing the install.

## Older macOS releases

**Monterey's bundled Python 3.9 is too old.** Select an installed Python 3.10+
with `pipx install --python /path/to/python3 ...`. Prebuilt dependencies avoid
compiling native extensions:

```bash
pipx install --pip-args='--only-binary=cryptography,cffi,rpds-py' git+https://github.com/hruskamiro/codex-tools.git
```

No Homebrew is required. Browsers open through macOS `open`; if your Python's
curses lacks wheel-down support, use Page Down.
