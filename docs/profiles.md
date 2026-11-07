# Profiles, wrapper commands and encrypted transfer

[Back to README](../README.md)

**Separate logins and configuration; share conversations when useful.** Each
profile has its own `CODEX_HOME`. New profiles share `~/.codex` history by default:

```bash
codex-manager new work
codex-manager run work
codex-manager new private --isolated
```

New shared profiles link transcripts, `state_5.sqlite`, and `thread_history_1.sqlite`
to their conversation store. SQLite keeps WAL/SHM beside the canonical databases.
`--isolated` gives a private store and databases. Existing profiles are not
migrated automatically; updates preserve their layout.

## Wrapper commands

**The command is the profile name, with no added prefix.**

```bash
codex-manager install work
work
codex-manager install private --wrapper cpvt  # optional custom name
```

Each profile has one registered wrapper. A default wrapper follows a rename;
a custom name stays unchanged:

```bash
codex-manager rename work company
codex-manager uninstall company
```

Upgrades retain existing wrappers. To replace an old automatically prefixed
wrapper with the plain name, run `codex-manager uninstall work`, then
`codex-manager install work`.

## Encrypted login transfer

**Exports are encrypted by default**, covering file-backed credentials,
configuration, and any selected conversations:

```bash
codex-manager export work
codex-manager update work.codex-profile
```

**`update` makes the profile ready to use.** It imports a missing profile or
refreshes an existing login/configuration, installing or restoring its wrapper.
It preserves existing conversations, sharing, and registered wrapper settings.
If no config was bundled, local settings are retained with file-backed auth enabled.

| Option | Use |
|---|---|
| `update BUNDLE --no-install` | Login/configuration only; leave commands untouched |
| `--name laptop-work` | Override the embedded profile name |
| `--bin-dir PATH` | Choose a new profile's wrapper directory |
| `import BUNDLE --install` | Import conservatively, with an optional wrapper |

Modified or unrelated commands are refused before credentials change. Existing
wrapper names, paths, and Codex executables are retained; missing wrappers are
restored. Explicit `update --install` and older `import --force` remain supported.

To transfer conversations, create a bundle and import under a new name:

```bash
codex-manager export work --include-conversations
codex-manager import work.codex-profile --name laptop-work --install
```

Bundled conversations restore privately. Auth-only imports share the destination's
default history unless `--isolated` is selected. Existing profiles reject bundled
conversations or isolation changes; use a new name instead.

### Transfer without a passphrase

```bash
codex-manager export work --no-passphrase
codex-manager update work.codex-profile
```

**This file contains plaintext credentials.** Permissions are `0600`; keep it
private during and after transfer. Import/update detect the format automatically;
encrypted bundles still require their passphrase. Both formats validate the same
archive and generate destination-local homes, stores, and wrappers. Review any
machine-specific paths in your supplied configuration.

Exports support file-backed `auth.json`, not keyring-only logins. Existing bundles
are never overwritten; select another `--output` for a fresh export.

**Inspect or clean up:** use `codex-manager list`, `codex-manager doctor work`,
`codex-manager path work`, or `codex-manager remove work`.
