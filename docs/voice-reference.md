# Voice service reference

[Quick setup](voice.md) · [Back to README](../README.md)

**An optional transcription queue for the upcoming mobile Agent Console.**
The app integration is still in development. The queue/CLI use Python's standard
library; recognition comes from a separately installed whisper.cpp server.
Installation starts no voice processes and downloads no models.

```text
phone -- independent SSH --> localhost:8790  ct voice serve
                                                |
                                      localhost:8080  whisper-server
phone -- separate SSH ----> agent relay (same or another computer)
```

Voice has independent routes, ports, credentials, identity, and state. It requires
neither a Codex login nor a running agent relay.

## Guided setup

```sh
ct voice install large
ct voice configure
ct voice serve
```

**Configure discovers engines/models and saves only after confirmation.** Choose
a model, device, threads, and ports, or an external engine. `q` cancels; an explicitly
downloaded model remains installed. Existing identities are retained.

`large` means `large-v3`; `turbo` means `large-v3-turbo`. `install MODEL --use`
also selects it; installing alone does not change the selected model.
`configure --interactive` forces the wizard with redirected input; bare configure
without a terminal writes nothing. Scripted configuration flags remain available.
Restart a service after changing its model or settings.

## Discover and configure

```sh
ct voice doctor
ct voice models list
ct voice init
```

**Doctor discovers without loading a model or creating state.** It checks PATH,
common/Homebrew locations, and Iris's configured CLI. Models are discovered from
Iris, managed downloads, `~/.cache/whisper.cpp`, and a nearby source build.
Iris is never imported or modified; invalid explicit paths fail.

To build whisper-server with a C++ toolchain and CMake:

```sh
git clone https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release --target whisper-server -j
```

CUDA/Vulkan also need their SDKs/drivers and [upstream build flags](https://github.com/ggml-org/whisper.cpp).
For an unusual executable, set `configure --whisper-bin /absolute/path/to/whisper-server`.
Select an existing model with `configure --model /path/to/ggml-small.bin`, or:

```sh
ct voice install small
ct voice configure --model small
```

Use multilingual models for Slovak/Czech; `.en` models are English-only.
Downloads come from whisper.cpp's Hugging Face model repository. Private temporary
files are checked for expected size/GGML header, then renamed into place. Existing
valid files are reused. `--force` replaces only after a completed download;
interrupted downloads can be rerun, but are not resumed or cryptographically verified.

## Run and check

**A managed engine stays loaded and restarts if it exits.** Accepted jobs wait
through an engine outage. Both ports bind localhost; the service neither downloads
models while serving nor takes over an occupied engine port.

```sh
ct voice serve --start-engine
ct voice status  # from another terminal
ct voice token   # explicitly reveal the pairing key
```

The wizard saves `manage_engine`; plain `serve` follows it. `--start-engine`
and `--external-engine` override it for one launch. For a separately managed engine:

```sh
ct voice configure --whisper-url http://127.0.0.1:8080
ct voice serve --external-engine
```

`configure --cpu`, `--gpu`, and `--threads 4` affect managed children, not external
servers. `--port 8790 --whisper-url http://127.0.0.1:8080` sets nondefault endpoints.
Private engine output is in `whisper.log`. Doctor checks setup; status authenticates
the running queue and checks engine health. Both, plus models list, support `--json`
without printing pairing keys.

**Background operation uses your service manager.** Example Linux user unit:

```ini
[Unit]
Description=Codex Tools voice jobs and Whisper engine
[Service]
ExecStart=/absolute/path/to/ct voice serve --start-engine
Restart=on-failure
RestartSec=3
UMask=0077
[Install]
WantedBy=default.target
```

Save as `~/.config/systemd/user/codex-tools-voice.service`, then run
`systemctl --user daemon-reload` and `systemctl --user enable --now codex-tools-voice.service`.
Logout behavior follows the machine's user-service policy; macOS can use launchd.
Codex Tools installs/enables no service automatically. Dropping mobile SSH does
not stop a separately supervised service.

## GPU verification and timing

**Warm requests reuse the loaded model.** Startup/restarts still load weights;
measurements below exclude startup, upload, and queue waiting. `configure --gpu`
permits the installed binary's backend; `--cpu` forces CPU. Status verifies the
current managed model/backend/device from its initialization log. An external
engine remains unverified. Linux service users need render-device/driver access;
sandbox restrictions can force CPU fallback. No drivers or groups are changed.

```sh
ct voice benchmark recording.wav --model large --device both --language en
ct voice benchmark recording.wav --model medium.en --device gpu --json
```

Benchmark uses temporary localhost engines, leaving existing services/settings
alone. Defaults: one warmup and three measured runs; `--warmup`/`--runs` override.
Loading and recognition are reported separately. A GPU request fails if GPU use
cannot be verified. Input format and three-minute limits match mobile recordings.

**Reference measurements: AMD Radeon 890M/Vulkan, four threads, 24 October 2026.**
An 11-second public English clip gave these mean warm processing times:

| Model | CPU | GPU | Speedup |
|---|---:|---:|---:|
| small.en | 2.38 s | 0.36 s | 6.7x |
| medium.en | 7.62 s | 0.94 s | 8.1x |
| large-v3 | 15.03 s | 1.70 s | 8.8x |

Queue requests were about 0.40/0.99/1.77 seconds. All produced the expected words
on this sample; these are throughput checks, not accuracy rankings.
[Raw measurements](benchmarks/voice-amd-890m-2026-10-24.json) retain startup,
individual requests, hardware, and sample metadata.

### Longer recordings

**Content matters as well as duration.** Distinct utterances from
[LibriSpeech test-clean](https://www.openslr.org/12/) (Panayotov et al., CC BY 4.0)
were concatenated in archive order without loops/added silence, then cut to exact
prefix lengths. This differs from the 11-second sample. Each model stayed loaded
across lengths, using `en`, four threads, one initial and three measured requests
per length. Vulkan GPU use was verified; upload/loading/queue wait are excluded.

| Length | small.en GPU | medium.en GPU | large-v3 GPU |
|---|---:|---:|---:|
| 15 s | 0.6 s | 1.6 s | 2.9 s |
| 30 s | 1.1 s | 3.3 s | 4.3 s |
| 60 s | 2.1 s | 5.7 s | 19.5 s |
| 120 s | 4.1 s | 10.5 s | 27.2 s |
| 180 s | 6.4 s | 15.9 s | 39.1 s |

| Length | small.en CPU | medium.en CPU | large-v3 CPU |
|---|---:|---:|---:|
| 30 s | 5.4 s | 16.7 s | 31.4 s |
| 60 s | 8.8 s | 27.9 s | 66.1 s |

These are measured examples, not guaranteed bounds. Large-v3's one-minute result
was consistently slower than extrapolation from 30 seconds; decoding settings
were unchanged. [Raw results](benchmarks/voice-lengths-amd-890m-2026-10-24.json)
include every run, startup, source-utterance IDs, and audio hashes.

WAV uses about **1.92 MB/minute**: a minute needs roughly **15.4 seconds to upload
at 1 Mbit/s**, before recognition or SSH overhead. The queue processes one job at
a time. Results can include `audio_seconds`, `transcription_seconds`, `queue_seconds`;
older results omit unknown timings. Retrieval/resubmission preserves saved values.
Queue time includes engine backoff; processing time is the last recognition attempt.
Status averages up to twenty recent completed jobs.

## Mobile connection

**Voice and agents have independent destinations.** In Settings → Voice, pair
with `ct voice pair-phone --local --address COMPUTER_IP`, or from a trusted SSH
client using `ct voice pair-phone PROFILE --address COMPUTER_IP`. Review/authorize
the phone key; the token travels over pinned temporary HTTPS after approval.
Pairing does not start voice. Override destination settings with `--remote-port`
or `--remote-state-dir`.

For manual setup, choose **Add voice connection**, select SSH hosts/jumps, port
**8790**, and the key from `ct voice token`. Port 8080 need not be forwarded.
Choose a language hint, record, stop, and review text in the original draft.
The phone needs neither Python nor whisper.cpp; voice can be disabled independently.

**Add API provider** supports OpenAI Whisper or a custom OpenAI-compatible
multipart `/audio/transcriptions` endpoint, using a base URL, model, and key.
No local engine/models are needed. Native keys use secure storage; browser keys
stay in the private bridge configuration. This is not support for arbitrary speech APIs.
API requests are synchronous: lost responses require an **explicit retry**, since
they may already have been processed/billed. No automatic paid fallback is used.
Provider/model choices are fixed per recording; text returns to the original draft.

## Recovery and retention

**Save now, retrieve later.** The phone saves audio and a UUID before upload;
the server commits the complete recording to SQLite before acknowledging it.
A lost acknowledgement triggers lookup of that UUID before reuploading. Identical
submissions reuse the job; different audio/language with the same UUID conflicts.

Accepted work survives SSH drops and restarts. Interrupted recognition retries;
completed results are reused. A crash between inference and result persistence
can repeat recognition. Engine failures retry with backoff; saved transcripts
return only to their original drafts, with insertion receipts, never as automatic
agent messages. Completed results are retained for at least seven days, pruned
on startup or submission.

Partial uploads restart. Audio remains on the phone until its transcript is saved;
uploads allow up to ten minutes overall with bounded idle waits. Phone-side work
resumes when foregrounded; OS background execution is not guaranteed.

| Limit | Value |
|---|---|
| Audio | Mono 16 kHz, 16-bit PCM WAV; 180 s / 10 MiB |
| Queue | 10 unfinished jobs; 1000 results |
| Storage | Private files/directories; no separate at-rest encryption |

Processed audio is logically removed, not securely erased from database pages/backups.
State is `~/.local/share/codex-tools/voice` (XDG-aware); all commands accept
`--state-dir PATH` before the subcommand. An existing `~/.local/share/voice-jobs`
identity is reused if the new location has none. Preserve identities/destinations
with pending recordings. The old relay-embedded cache is not silently converted.

Authenticated API: `GET /v1/voice`, `POST /v1/voice/jobs/{uuid}?language=sk`
(raw WAV), and `GET /v1/voice/jobs/{uuid}`. The engine API stays behind this queue.
