# Security

Codex Tools reads local Codex transcripts, which may contain private source
code, prompts, filesystem paths, and tool output. Keep the generated summaries,
viewer state, and managed conversation stores private to your user account.

## Viewer exposure

The viewer has no authentication. It binds to `127.0.0.1` by default and
refuses non-loopback hosts unless `--allow-remote` is supplied. Do not expose it
to an untrusted network.

The browser viewer currently loads pinned versions of several rendering
libraries from jsDelivr. Those scripts execute in the viewer origin and can
access its local transcript API. Bundling these assets locally is planned; do
not use the viewer where third-party CDN execution is unacceptable.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability
reporting feature rather than opening a public issue. Include the affected
command, reproduction steps, and the expected impact. Please do not include
real transcript content or credentials in a report.
