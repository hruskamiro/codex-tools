# Installation and updates

[Back to README](../README.md)

## Install from GitHub

Use Linux or macOS with Python 3.10 or newer and pipx:

```bash
pipx install git+https://github.com/hruskamiro/codex-tools.git
```

The commands are `codex-tools`, `codex-viewer` and `codex-manager`. Codex Tools
is not yet published on PyPI. Model-backed summaries and structured tasks also
need an installed Codex CLI; ordinary viewing and search do not call a model.
The Markdown viewer and Agent Console need no LaTeX or Homebrew.

## Older macOS releases

On older macOS releases, use prebuilt wheels for native Python dependencies
to avoid compiling them and downloading build toolchains:

```bash
pipx install --pip-args='--only-binary=cryptography,cffi,rpds-py' git+https://github.com/hruskamiro/codex-tools.git
```

Monterey's bundled Python 3.9 is too old; select an installed Python 3.10 or
newer with `pipx install --python /path/to/python3` when needed. The default browser opens through
macOS's `open` command. Mouse-wheel-down support depends on the curses library
provided by Python; Page Down remains available when that support is missing.

## Update

For a GitHub installation, fetch published updates with:

```bash
pipx upgrade codex-tools
codex-tools alias install --force  # refresh installed alias/completion
codex-viewer restart              # reload an already-running viewer
```

Restart an already-running relay separately to load new code. Upgrading preserves
configuration and pairing keys; it does not start a relay or grant auto-start
permission. See [relay setup](relay.md#start-the-remote-relay-automatically-over-ssh).

If the installation needs rebuilding from its original GitHub source, use
`pipx reinstall codex-tools`. An editable source-checkout installation instead
uses its checked-out code; update that checkout before refreshing its installation.

## Install `ct` and completion

The shorter `ct` command is convenient for everyday use. Its installer also
adds Bash completion for both `ct` and `codex-tools`:

```bash
codex-tools alias install
source ~/.local/share/bash-completion/completions/codex-tools
```

**Completion covers every current top-level tool and its subcommands.** New
shells load it automatically when the `bash-completion` package is available.

If an earlier uninstall left stale alias or completion files, replace them with:

```bash
codex-tools alias install --force
```
