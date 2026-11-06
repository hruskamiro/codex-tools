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
automatically migrated, and importing with `--force` preserves their layout.

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

**The complete bundle is encrypted with your passphrase.** Credentials,
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
codex-tools manager import work.codex-profile --force
```

`--force` updates credentials and any bundled configuration, preserving the
existing conversation store and sharing settings. If the bundle has no config,
existing settings are retained with file-backed authentication enabled. To
restore bundled conversations or change isolation, import under a new `--name`.

Export currently supports file-backed `auth.json` credentials, not credentials
stored only in an OS keyring.

Useful profile commands:

```bash
codex-manager list
codex-manager doctor work
codex-manager path work
codex-manager remove work
```
