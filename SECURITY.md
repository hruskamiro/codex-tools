# Security

Codex Tools reads local Codex transcripts, which may contain private source
code, prompts, filesystem paths, and tool output. Keep the generated summaries,
viewer state, and managed conversation stores private to your user account.

## Profile exports

`codex-manager export` bundles file-backed Codex credentials and profile setup.
Exports are encrypted with a passphrase and created with owner-only permissions,
but they still grant access to the exported account when decrypted. Use a strong
passphrase, transfer bundles only through trusted channels, and remove copies
that are no longer needed. Passphrases are read from the terminal and should
never be supplied in command arguments.

## Viewer exposure

The viewer has no authentication. It binds to `127.0.0.1` by default and
refuses non-loopback hosts unless `--allow-remote` is supplied. Do not expose it
to an untrusted network.

The browser viewer uses bundled, pinned rendering libraries by default. The
optional `--web-assets cdn` mode loads matching copies from jsDelivr. Those
third-party scripts execute in the viewer origin and can access its local
transcript API, so use CDN mode only when that trust is acceptable.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability
reporting feature rather than opening a public issue. Include the affected
command, reproduction steps, and the expected impact. Please do not include
real transcript content or credentials in a report.
