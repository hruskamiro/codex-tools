# Optional voice service

[Back to README](../README.md)

**Dictation support for the upcoming mobile Agent Console.** Mobile integration
is still in development. Voice uses a separate whisper.cpp `whisper-server`,
connection, and credentials. **Normal installation adds no models or voice dependencies.**

## Quick setup

Install whisper.cpp on the transcription computer, then:

```sh
ct voice doctor          # find the engine and existing models
ct voice install large   # explicitly download/reuse large-v3
ct voice configure       # choose model, device and engine
ct voice serve
```

`install` downloads a model, not the executable. Setup can reuse Iris models or
an external engine. See [engine installation](voice-reference.md#discover-and-configure).

## Connect a phone

**Keep the service on localhost and reach it through independent SSH.** Voice
and agents can use different computers; voice needs no Codex login or agent relay.

```sh
ct voice pair-phone --local --address COMPUTER_IP
```

Use a phone-reachable address. In **Settings → Voice → Pair voice server**, scan
and authorize the phone's own SSH key. Pairing transfers the token securely but
does not start the service. For another destination, use
`ct voice pair-phone PROFILE --address COMPUTER_IP` from a trusted SSH client.
See [mobile connections](voice-reference.md#mobile-connection).

## Status and recovery

```sh
ct voice status
ct voice models list
```

**Accepted jobs survive disconnects and restarts.** Pending audio stays on the
phone; completed results remain retrievable for at least seven days. Partial
uploads restart; recordings are limited to three minutes. The [reference](voice-reference.md)
covers [background services](voice-reference.md#run-and-check),
[GPU timing](voice-reference.md#gpu-verification-and-timing), and [recovery limits](voice-reference.md#recovery-and-retention).
