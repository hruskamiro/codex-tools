# Optional voice service

**Transcription support for the upcoming mobile Agent Console.** Mobile
integration is still in development. Voice uses a separately installed
whisper.cpp `whisper-server`, with its own connection and credentials.
**Normal Codex Tools installation adds no models or voice dependencies.**

## Quick setup

On the transcription computer, install whisper.cpp, then:

```sh
ct voice doctor          # discover the engine and existing models
ct voice install large   # explicitly download/reuse large-v3
ct voice configure       # choose model, device and engine settings
ct voice serve
```

`install` downloads a model, not the whisper.cpp executable. The guided setup
can reuse existing models, including Iris models, or connect to an external
engine. See [engine installation and configuration](voice-reference.md#discover-and-configure).

## Connect a phone

The service binds to localhost; the phone reaches it through its own SSH
connection. Agent and voice connections are configured independently and can
use different computers. A Codex login or running agent relay is not required.

```sh
ct voice pair-phone --local --address COMPUTER_IP
```

Use an address reachable from the phone. In the app, choose **Settings → Voice →
Pair voice server**. Pairing asks you to authorize the phone's own SSH public
key and transfers the voice token securely; it does not start the service.

For a different destination, use `ct voice pair-phone PROFILE --address COMPUTER_IP`
from a computer with trusted SSH access to that profile. See
[mobile connection details](voice-reference.md#mobile-connection).

## Status and recovery

```sh
ct voice status
ct voice models list
```

**Accepted jobs survive disconnects and service restarts.** Completed transcripts
can be retrieved later; pending audio stays on the phone until uploaded.
Interrupted uploads must be retried. The service retains results for at least
seven days and currently accepts recordings up to three minutes.

See the [full reference](voice-reference.md) for
[background services](voice-reference.md#run-and-check),
[GPU setup and benchmarks](voice-reference.md#gpu-verification-and-timing), and
[recovery limits](voice-reference.md#recovery-and-retention).
