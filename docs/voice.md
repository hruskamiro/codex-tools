# Optional voice service

Codex Tools can serve durable transcription jobs independently of its agent
relay. Voice is opt-in: normal installation adds no Whisper/PyTorch/CTranslate2
packages, downloads no models and starts no voice processes. The queue and CLI
use Python's standard library. Install whisper.cpp separately on the chosen
transcription machine; its `whisper-server` provides recognition.

```text
mobile voice connection -- SSH --> 127.0.0.1:8790  ct voice serve
                                                 |
                                                 v
                                  127.0.0.1:8080  whisper-server

mobile agent connection -- SSH --> Codex relay (same or another machine)
```

The two connections have independent routes, ports and credentials. Voice has
its own identity, state and process; it does not require agents, a Codex CLI or a
Codex login. Running an agent relay never enables voice.

## Guided setup

Run `ct voice configure` in a terminal to open the guided setup. It discovers
whisper-server, lists existing models (including Iris models), lets you select
or explicitly download a model, and asks for device, threads and localhost
ports. You can instead choose an already running engine. Saving initializes a
missing voice identity while preserving an existing one. Enter `q` to cancel;
existing settings are only written after the final Save step. A model you
explicitly downloaded remains installed if you later cancel configuration.

For example:

```sh
ct voice install large                 # download/reuse large-v3
ct voice configure                     # choose model and engine settings
ct voice serve                         # follows the saved engine choice
```

`ct voice install large --use` also selects the model, preserving the other
settings. `large` is an alias for `large-v3`, and `turbo` for `large-v3-turbo`.
`install` installs model files; whisper.cpp/whisper-server remains a separately
installed executable. Download progress is shown and existing discovered models
are reused without copying/downloading them again. `--force` creates a fresh
managed download. Installing alone does not change the selected model unless
`--use` is passed. Restart a running service to load a newly selected model.

For scripts, configuration flags still work without prompts. Use
`configure --interactive` to explicitly request the wizard with redirected
input. Bare configure without a terminal gives usage guidance and writes
nothing. The wizard's local-engine choice is saved as `manage_engine`; plain
`serve` then supervises that engine. `serve --start-engine` or
`serve --external-engine` override the saved choice for a single run.

## Discover and configure

On the transcription machine, with this checkout/version installed:

```sh
ct voice doctor
ct voice models list
ct voice init
```

`doctor` reports an existing executable, installed models and engine readiness
without loading a model or creating state. It searches PATH, common user install
locations, Homebrew locations, and the server beside Iris's configured
`whisper-cli`. Model discovery checks Iris's configured model, the managed model
directory, `~/.cache/whisper.cpp`, and the models beside a discovered source
build. Iris is never imported or modified. Explicit invalid executable paths
fail rather than silently selecting a different installation.

Install/build whisper.cpp if needed:

```sh
git clone https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release --target whisper-server -j
```

A C++ toolchain and CMake are required. CUDA/Vulkan builds require their matching
SDKs/drivers and upstream build flags; see the
[whisper.cpp instructions](https://github.com/ggml-org/whisper.cpp). This command
does not install or enable anything automatically. Point Codex Tools at an
unusual installation with `ct voice configure --whisper-bin /absolute/path/to/whisper-server`.

Choose an existing model, including one already used by Iris:

```sh
ct voice configure --model /absolute/path/to/ggml-small.bin
```

Or explicitly predownload and select one:

```sh
ct voice install small
ct voice configure --model small
```

Models download from whisper.cpp's model repository on Hugging Face. Files are
written privately to temporary files, checked for expected length/GGML header,
and renamed into place only after completion. Existing valid files are reused;
`--force` replaces one only after a successful download. Interrupted downloads
can be rerun; this is not a resumable partial download or a cryptographic
checksum guarantee. Names available for download are listed by `models list`.
Use multilingual `small`/`base` rather than `.en` for Slovak or Czech.

## Run and check

To run the job service and supervise its own whisper-server child:

```sh
ct voice serve --start-engine
```

The configured executable/model are used. Both ports bind only to `127.0.0.1`.
The engine model remains loaded while whisper-server runs. If that child exits,
Codex Tools restarts it; accepted jobs remain queued while it is unavailable.
Engine output goes to private `whisper.log` in the voice state directory. No
model download happens when serving, and an already occupied engine port is
left untouched: do not use `--start-engine` to adopt an existing process.

To use a whisper-server managed separately, start it yourself, then run:

```sh
ct voice configure --whisper-url http://127.0.0.1:8080
ct voice serve
```

`serve` does not start or stop an external engine. A configured engine can be
forced to CPU with `ct voice configure --cpu`; `--gpu` permits the acceleration
supported by the installed binary. These flags affect only a child started by
`--start-engine`, not an external server. Change its threads with `--threads 4`.
Restart the voice service after configuration changes.

In a second terminal:

```sh
ct voice status
ct voice doctor
ct voice token
```

`status` authenticates to the configured queue, verifies its identity, reports
queue counts and checks the engine's `/health` endpoint. `doctor --json`,
`status --json` and `models list --json` produce machine-readable reports without
including pairing keys. Only `token` prints the key, explicitly for setup.
`doctor` can report a valid configuration before the processes are started;
`status` checks the running service. Configure nondefault ports with
`ct voice configure --port 8790 --whisper-url http://127.0.0.1:8080`.

For background operation, run the foreground command under your user service
manager. For example, a Linux user unit:

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

After initializing/configuring voice, save the unit as
`~/.config/systemd/user/codex-tools-voice.service`, run
`systemctl --user daemon-reload` and
`systemctl --user enable --now codex-tools-voice.service`. To keep it running
after logout, use your machine's usual user-service/session policy. macOS can
run the same foreground command under launchd. Disconnecting mobile SSH has no
effect on a separately managed voice service. No service is installed or
enabled merely by installing Codex Tools.

## GPU verification and timing

The serving process keeps the model loaded between jobs. Each recording uses
the same running recognizer; it does not start a CLI process or reload model
weights. Startup or an engine restart still incurs model loading and may make
the first recognition slower. Warm processing measurements below exclude that
startup, mobile network transfer and time waiting behind other jobs.

The installed whisper-server decides which acceleration backend to use. In
local managed mode, `configure --gpu` (or Auto in guided setup) permits that
backend; `--cpu` forces CPU. GPU permission/configuration alone is not proof of
GPU use. `ct voice status` reports the actual model, backend and device observed
in the current managed engine's initialization log, with a verification flag.
An external engine remains explicitly unverified by this service.

On Linux, the service account must be able to access the GPU render device
(for example `/dev/dri/renderD128`) and its installed Vulkan/other driver.
Sandboxed commands without device access can fall back to CPU even when a
normal terminal or user service can use the GPU. No driver or group membership
is changed automatically.

To measure your own recording without changing saved setup or stopping an
existing service:

```sh
ct voice benchmark /path/to/recording.wav --model large --device both --language en
ct voice benchmark /path/to/recording.wav --model medium.en --device gpu --json
```

The command starts and cleans up temporary localhost engines, separates model
loading from recognition, performs one warmup and three measured runs by
default, and reports mean processing time and speed relative to the audio's
length. `--runs` and `--warmup` can adjust those counts. A requested GPU run fails
if it cannot verify GPU use, rather than labelling a CPU fallback as GPU.
The input format/three-minute limit matches the mobile voice recordings.

Measured on this machine on 2026-10-24, using a clean 11-second public English
sample, four CPU threads, and the AMD Radeon 890M through Vulkan:

| Model | CPU processing | GPU processing | GPU speedup |
| --- | ---: | ---: | ---: |
| small.en | 2.38 s | 0.36 s | 6.7x |
| medium.en | 7.62 s | 0.94 s | 8.1x |
| large-v3 | 15.03 s | 1.70 s | 8.8x |

These are means of three warm requests, excluding startup and network upload.
The durable queue requests took about 0.40, 0.99 and 1.77 seconds respectively.
All models produced the expected words on this particular sample. This is a
throughput sanity check, not an accuracy ranking for accents, noisy trains,
technical terms, or longer recordings. Reproduce on representative recordings
before choosing based on speed alone. [Raw measurements](benchmarks/voice-amd-890m-2026-10-24.json)
include startup, individual requests, hardware and sample metadata.

### Longer recordings

Additional measurements on the same machine use distinct English utterances from
[LibriSpeech test-clean (OpenSLR SLR12)](https://www.openslr.org/12/), prepared by
Vassil Panayotov and collaborators, licensed CC BY 4.0. The utterances were
concatenated in archive order, with no loops or added silence, then cut to exact
15/30/60/120/180-second prefixes. This is different speech from the JFK sample.

Each model stays loaded in one server across durations. At each duration, one
initial request is followed by three measured requests; means are below. The
English hint is `en`, CPU thread count is four, and GPU use is verified as Vulkan
on the Radeon 890M. Loading, mobile upload and queue waiting are excluded.

| Recording length | small.en GPU | medium.en GPU | large-v3 GPU |
| --- | ---: | ---: | ---: |
| 15 s | 0.6 s | 1.6 s | 2.9 s |
| 30 s | 1.1 s | 3.3 s | 4.3 s |
| 60 s | 2.1 s | 5.7 s | 19.5 s |
| 120 s | 4.1 s | 10.5 s | 27.2 s |
| 180 s | 6.4 s | 15.9 s | 39.1 s |

CPU comparisons with the same recordings and four threads:

| Recording length | small.en CPU | medium.en CPU | large-v3 CPU |
| --- | ---: | ---: | ---: |
| 30 s | 5.4 s | 16.7 s | 31.4 s |
| 60 s | 8.8 s | 27.9 s | 66.1 s |

These are measured examples, not guaranteed maximums. Runtime depends on speech
content as well as duration: large-v3 consistently took longer for the one-minute
clip than extrapolating its 30-second result would suggest. No decoding settings
were changed to improve the numbers. The
[raw measurements](benchmarks/voice-lengths-amd-890m-2026-10-24.json) retain every
run, model startup, initial requests, source-utterance identifiers and audio hashes.

The current mono PCM WAV format is about 1.92 MB per minute. A one-minute upload
alone takes roughly 15.4 seconds at a sustained 1 Mbit/s, before recognition; SSH
overhead and interruptions can add more. The durable service processes one job
at a time, so accepted work may also wait behind another recording.

New job responses optionally include `audio_seconds`, `transcription_seconds`
and `queue_seconds`. They persist with the result and are unchanged when a
completed UUID is retrieved or resubmitted. Older results remain readable and
omit unknown timings. Processing time measures the last recognition request;
queue time measures the wait from accepted upload to that attempt, including
engine backoff. CLI status includes a compact average over up to twenty recent
completed jobs, and clients can display one unobtrusive processing-time label.

## Mobile connection

In the mobile console, open **Settings → Voice → Add voice connection**. For
SSH, choose the voice machine and any jump hosts, set destination port **8790**,
and enter the voice pairing key from `ct voice token`. The same machine may
also host agents, but the app opens separate connections. Choose an optional
language hint, record, stop, then review the transcript in the original draft.

The engine's port 8080 need not be forwarded. Agent relay keys/configuration do
not configure voice. Changing the active agent connection does not change the
voice destination. The Flutter app needs neither Python nor whisper.cpp on the
phone. Voice can be disabled without affecting agents.

The mobile console also offers **Add API provider** for OpenAI Whisper or a
custom OpenAI-compatible transcription endpoint. Supply the API base URL,
model and key in that profile; it requires no `ct voice` service or local
models. Native keys use the phone's secure storage; browser keys stay in the
private local bridge configuration. This option supports providers exposing
the multipart `/audio/transcriptions` API, rather than arbitrary speech APIs.

API transcription is synchronous: recordings remain saved locally, but a lost
response cannot be recovered through the durable job lookup described below.
After an interrupted request, the app requires an explicit retry because the
provider may already have processed or billed it. There is no automatic paid
fallback. Provider/model selections are captured for each recording, and
transcripts still return to their original agent draft.

## Recovery and retention

The phone saves audio and a stable UUID before uploading. The service commits
the complete recording and metadata to SQLite before acknowledging acceptance.
If its response is lost, the app checks the same UUID before reuploading. A
repeated identical submission returns the existing job; changed audio/language
with that UUID conflicts.

Accepted work continues after SSH drops. Jobs survive service restart;
unfinished recognition is retried, while saved completed results are reused.
Crashing between engine completion and result persistence can repeat inference.
Completed results are available for at least seven days, pruned on startup or
submission. Temporary engine failures retry with backoff. Nothing automatically
sends an agent message: transcripts return to their original drafts, protected
by persisted insertion receipts.

Partial uploads restart from the beginning; audio stays on the phone until its
transcript is saved. The app permits up to ten minutes overall for an upload,
with bounded idle waits. Phone-side work resumes when foregrounded; Android/iOS
background execution is not promised. No network means no new upload/download,
but accepted server work continues.

Clips are mono 16 kHz, 16-bit PCM WAV, at most 180 seconds / 10 MiB. Limits are ten
unfinished jobs and 1000 stored results. Audio is logically removed from SQLite
after processing; that is not secure erasure of database pages or backups.
Private directories/files are used, without separate at-rest encryption.

State defaults to `~/.local/share/codex-tools/voice` (respecting XDG data home).
All commands accept `--state-dir PATH` before the subcommand. The prototype's
`~/.local/share/voice-jobs` is reused if its identity exists and the new location
has no identity, preserving existing jobs and keys. Do not rotate identities or
retarget mobile profiles that still have pending recordings. The old
relay-embedded faster-whisper experiment is superseded; its file-per-job cache
is a different format and is not silently converted.

The job API remains authenticated `GET /v1/voice`, raw-WAV
`POST /v1/voice/jobs/{uuid}?language=sk` and `GET /v1/voice/jobs/{uuid}`. A client
can submit now and retrieve later. The Whisper engine's HTTP API is kept behind
this service and never used as the mobile reliability protocol.
