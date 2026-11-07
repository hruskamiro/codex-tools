# Profiles, wrapper commands and encrypted transfer

[Back to README](../README.md)

Profiles give each account its own `CODEX_HOME`, login, and configuration.
Conversations are shared with the normal `~/.codex` profile by default.
New profiles link both transcript files and the modern `state_5.sqlite` and
`thread_history_1.sqlite` databases into their conversation store, so thread
indexes and history stay shared too. SQLite keeps its WAL/SHM files beside the
canonical databases. Authentication and configuration remain profile-local.
`--isolated` uses a private conversation store, including private databases.
This applies to newly created/imported profiles; existing profiles are not
automatically migrated, and `update` (or `import --force`) preserves their layout.

```bash
codex-manager new work
codex-manager run work
```

Use a private conversation store when needed:

```bash
codex-manager new private --isolated
```

## Wrapper commands

Install a convenient command for a profile:

```bash
codex-manager install work
work
```

**The default wrapper is the profile name, with no added prefix.** Choose another name with:

```bash
codex-manager install work --wrapper cw
```

Each profile has at most one registered wrapper. Rename and uninstall stay
coordinated with it:

```bash
codex-manager rename work company
codex-manager uninstall company
```

A default wrapper follows the renamed profile (`work` becomes
`company`). A custom wrapper such as `cw` keeps its name.

Existing registered wrappers are preserved when upgrading. To replace an old
automatically prefixed command with the plain profile name:

```bash
codex-manager uninstall work
codex-manager install work
```

## Encrypted login transfer

Export file-backed credentials and profile configuration:

```bash
codex-manager export work
codex-manager import work.codex-profile
```

**The complete bundle is encrypted with your passphrase by default.** Credentials,
configuration, and—when selected—conversations are protected together.

```bash
codex-manager export work --include-conversations
codex-manager import work.codex-profile \
  --name laptop-work --isolated --install
```

Import uses the embedded profile name unless `--name` is supplied. Without
`--isolated`, an auth-only import shares the destination machine's default
conversations. A bundle that includes conversations restores them privately.
To refresh a login when the destination profile already exists:

```bash
codex-manager update work.codex-profile
```

`update` imports the bundle if the profile is missing, or refreshes an existing
profile without requiring `--force`. It updates credentials and bundled configuration, preserving the
existing conversation store and sharing settings. If the bundle has no config,
existing settings are retained with file-backed authentication enabled. To
restore bundled conversations or change isolation, import under a new `--name`.

Existing wrapper commands are retained. With `update --install`, an unmodified
registered wrapper can be reinstalled at its saved path, including a custom
name and Codex executable. Locally modified or unrelated commands are refused
before the login is changed. A new profile uses its name as the command; use
`--bin-dir` for a nondefault installation directory.

```bash
codex-manager update work.codex-profile --install
```

### Transfer without a passphrase

For an explicitly unencrypted export:

```bash
codex-manager export work --no-passphrase
codex-manager update work.codex-profile --install
```

**An unencrypted bundle contains plaintext credentials.** It is created with
owner-only permissions (`0600`); keep it private during and after transfer.
`import` and `update` detect the format automatically, so unencrypted bundles
do not prompt for a passphrase. Encrypted bundles still require their passphrase;
`--no-passphrase` does not disable authentication on an existing encrypted file.

Both formats contain the same validated profile archive. They use the embedded
profile name unless overridden with `--name`, and never reuse source-machine
home/store paths for a new destination profile. Installed wrappers are generated
for the destination. Portable configuration is copied as supplied, so review any
machine-specific paths in your own `config.toml`.

`import` remains conservative when a profile exists. The older
`import BUNDLE --force` refresh behavior is still supported. Exports never
overwrite an existing bundle; choose another `--output` path for a fresh export.

Export currently supports file-backed `auth.json` credentials, not credentials
stored only in an OS keyring.

Useful profile commands:

```bash
codex-manager list
codex-manager doctor work
codex-manager path work
codex-manager remove work
```
