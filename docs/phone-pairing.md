# Pair a phone with one invitation

Pairing imports the relay profile and complete ordered SSH route, creates an
Ed25519 key on the phone, and authorizes its public key on every host. No second
QR is needed. Existing profiles and private keys are not overwritten.

On a computer with working SSH access to the destination and every jump host:

```sh
pipx inject codex-tools qrcode  # optional QR encoder, once
ct connect pair-phone destination-via-jump --address 192.0.2.10
```

Use the computer's address reachable from the phone through Wi-Fi or a VPN.
The command uses the named `ct-connect` profile from `ssh-launcher.json` when
present, otherwise an SSH alias. It resolves `ProxyJump` from SSH configuration.
Overrides: `--ssh`, `--remote-port`, `--remote-bin`. SSH authentication and host
verification must already work in batch mode. The remote relay identity must
already be initialized (`ct relay init`).

In the native app, choose **Settings → Connections → Pair phone**, then:

1. Scan the QR, or paste the computer's `ct-pair:…` invitation.
2. Review the destination and jump hosts, including host fingerprints.
3. Tap **Pair and authorize SSH**.
4. Compare the phone-key fingerprint on both devices. Type `y` on the computer
   to authorize that key on every listed host.
5. The phone saves the SSH hosts and relay profile. Tap **Connect** or **Done**.

Pairing defaults relay auto-start to off, unless the saved client connection
explicitly opted in. Start the destination relay manually with `ct relay serve`,
or first run `ct relay configure --allow-auto-start` on that destination and
enable **Start relay automatically** in the phone's connection settings. The
phone's switch alone cannot authorize a missing relay to start.

`--output phone.svg` also saves the QR as an SVG. It contains the temporary
invitation secret, so keep it private. Invitations expire after five minutes;
the endpoint closes once the phone acknowledges saving. No daemon stays running.

The QR contains a temporary address, secret, and certificate fingerprint; the
full configuration is fetched over HTTPS pinned to that certificate. Neither a
relay token nor an SSH private key is placed in the QR. The relay token is released
only after authorization. The phone proves possession of its private key.

## Networking

Only the temporary pairing listener defaults to `0.0.0.0`, on a free port.
`--bind ADDRESS` restricts its interface; `--port PORT` selects a port for firewall
or tunnel configuration. `--address` is the address advertised to the phone.
The relay's own listener stays unchanged and can remain localhost-only.

Devices need network reachability, not necessarily the same subnet. Pairing does
not bypass hotspot isolation, firewalls, or NAT. Use an existing VPN or tunnel
when necessary. Network discovery and an internet pairing broker are not included.
Browser preview cannot pin this temporary certificate and does not pair.
Native Linux supports pasting invitations; camera scanning is supported on
Android/iOS and supported macOS builds.

## Recovery and requirements

The pending phone key is saved in OS secure storage before submission. Reopening
the same invitation reuses it. Imported IDs are stable for that invitation, so
retrying a partial local save does not duplicate entries. Existing connections
remain intact. Pending keys remain in secure storage if setup is interrupted.

Authorization preserves `authorized_keys` and skips keys already present. It
requires remote Python 3 and reads the server's Ed25519 host key through an
already trusted SSH session. Custom `ProxyCommand` routes cannot be exported;
use named `ProxyJump` hosts. SSH passwords and computer private keys stay separate.

If a host fails, the computer reports hosts already authorized; their keys are
not automatically removed. The phone saves a complete profile only after all
hosts succeed. A lost acknowledgement after saving does not erase the profile.
Start another invitation if the current one expires.

Tests cover native Dart/Python TLS interoperability, incorrect-certificate
rejection, Ed25519 proof, invitation expiry, SSH routes, key preservation, and
the review/save UI. Physical camera and phone permission behavior still require
device testing.
