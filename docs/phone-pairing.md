# Pair a phone with one invitation

[Back to README](../README.md)

**Import the relay connection and SSH route with one temporary QR invitation.**
This supports the upcoming mobile Agent Console; mobile integration is still
in development. The phone creates its own Ed25519 key; existing profiles and
private keys are preserved.

On a computer with trusted SSH access to the destination and every jump host:

```sh
pipx inject codex-tools qrcode  # optional QR encoder
ct connect pair-phone destination-via-jump --address 192.0.2.10
```

The relay identity must already exist (`ct relay init`), and SSH authentication
and host verification must work in batch mode. The route comes from a
saved `ct-connect` profile or SSH alias, including `ProxyJump`. Overrides:
`--ssh`, `--remote-port`, `--remote-bin`.

In **Settings → Connections → Pair phone**:

1. Scan the QR or paste its `ct-pair:…` invitation.
2. Review the hosts and fingerprints; choose **Pair and authorize SSH**.
3. Compare the phone-key fingerprint on both devices; authorize it with `y` on the computer.
4. Save, then connect.

**Auto-start is off unless explicitly opted in.** Start the relay manually,
or grant [destination permission](relay.md#start-the-remote-relay-automatically-over-ssh)
and enable the phone's auto-start switch. That switch alone cannot grant permission.

Invitations expire after five minutes. `--output phone.svg` saves a copy containing
the temporary secret; keep it private. The QR contains an address, secret, and
certificate fingerprint, not a relay token or computer private key. Configuration
travels over certificate-pinned HTTPS; the token is released after authorization.
The temporary endpoint closes after the phone acknowledges saving.

## Networking

**Use an address reachable from the phone**, through Wi-Fi, VPN, or a tunnel.
Only the temporary pairing listener defaults to `0.0.0.0`; `--bind` and `--port`
restrict it. `--address` sets the advertised address. The relay stays unchanged
and can remain localhost-only. Pairing does not bypass isolation, firewalls, or NAT.

Browser preview cannot pin the certificate and does not pair. Native Linux can
paste invitations; camera scanning is supported on Android/iOS and supported macOS builds.

## Recovery and requirements

**Interrupted setup can be retried.** The pending phone key stays in secure
storage; reopening the invitation reuses it, and stable IDs avoid duplicate
connections. Expired invitations need replacing.

Authorization preserves `authorized_keys` and skips existing keys. Remote Python 3
and the server's Ed25519 host key are required. Use named `ProxyJump` hosts;
custom `ProxyCommand` routes cannot be exported. SSH passwords/private keys stay separate.

If a host fails, already-authorized keys remain and are reported. A complete
phone profile is saved only after all hosts succeed; a lost final acknowledgement
does not erase it. TLS, proof-of-key, expiry, routes, and save/review flows have
automated tests; physical cameras and permission behavior still need device checks.
