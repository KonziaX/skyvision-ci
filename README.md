# SkyVision MOSS runtime build

Windows x64 / CUDA 13.0 / RTX 50 (`sm_120`). The workflow pins both upstream
repositories and applies `patches/moss-runtime.patch` before compiling. Editing
the separately cloned frontend repository does not change this build.

The patch exposes the MOSS runtime controls, corrects realtime initialization,
commits the final generated token and a turn boundary on capped/interrupted
turns, bounds the camera queue to recent frames, and accepts browser JPEG/PNG
frames. Text-only turns rebuild physical KV positions after context eviction.
Static decode graph rebuilds retain and free the context actually returned by
the graph builder, preventing a 64 MiB host allocation leak on each new frame.
CUDA graphs and flash attention remain enabled. Model dimensions and
token identifiers still come from the GGUF/tokenizer; do not change them as
performance knobs.

## Start

Use the downloaded executable with the original frontend checkout:

```powershell
& ./vqa-cli.exe --models-dir /path/to/models --tokenizer /path/to/tokenizer.json `
  --web-dir /path/to/SkyVision.cpp/web --n-ctx 4096 --serve 8899
```

The 12 GB laptop GPU profile uses 4096 self-cache tokens. `MOSS_XCTX` controls
visual-cache rows (1024..32768, defaults to the context size). Increasing these
values costs VRAM; neither needs a recompile. `--threads` and `--cpu` also reach
server loads now. The package includes CUDA DLLs and a base `web` directory;
models/tokenizers and FFmpeg are separate. The enhanced MOSS page is maintained
in the local frontend repository. Windows console output is UTF-8 automatically.

`scripts/install-runtime-artifact.py` verifies the pinned source refs, patch hash,
binary hash and CUDA settings before extracting a GitHub artifact. Pass
`--archive`, `--dest` (a new folder inside this repository), and
`--expected-commit` (the workflow run's full commit SHA). It refuses existing
destinations, path traversal, ZIP links and duplicate filenames.

## Runtime API

`GET /api/runtime` returns API version 2 and server load options.

`POST /api/model/<id>/rt/start?session=<id>&src=<source>` accepts:

| Query | Default | Meaning |
| --- | --- | --- |
| `interval` | 1 | Sampling interval in seconds, 0.2..60 |
| `frames` | 1 | Most recent frames per inference, 1..8 |
| `n` | 512 | User-answer token cap |
| `observe-n` | 128 | Autonomous-observation token cap |
| `max-pixels` | 262144 | Per-frame inference pixel budget |
| `temperature`, `top-k`, `top-p` | 0, 0, 1 | Greedy or sampling settings |
| `repeat-penalty`, `repeat-last-n` | 1.05, 128 | Recent text repetition control |
| `recover-repeats` | 0 | Opt-in short-context recovery when one autonomous observation appears 3 times in the latest 64 nonempty observations |
| `video-size` | 1920x1080 | DirectShow input resolution; `auto` uses device default |
| `capture-fps` | 30 | DirectShow input FPS; independent of inference sampling |
| `pixel-format` | auto | DirectShow format, e.g. `yuyv422` |
| `warmup` | 2 | Camera-only trim in seconds, independent of input FPS |
| `jpeg-quality` | 2 | FFmpeg MJPEG quality, 1..31 |
| `sys` | 1 | Use the official training-shaped realtime system prompt |
| `system-prompt`, `prompt` | official / concise Chinese task | Runtime prompt overrides |

`src=browser` enables external frames through
`POST .../rt/frame?session=<id>&timestamp=<seconds>`, with JPEG/PNG bytes as the
body. Timestamps must be finite, nonnegative and nondecreasing.

`rt/say?q=...` wakes the worker immediately and interrupts its current response.
`rt/interrupt` interrupts without resetting the KV session; `rt/stop` ends the
stream. `rt/preview`, `rt/status`, and `rt/events?since=<seq>` provide the actual
sampled frame, bounded-queue counts, and incremental output. One live stream
owns the resident model; conflicting offline asks/model swaps receive 409.

`recover-repeats=1` keeps the capture process, original task and latest user
question while retiring old model history when repeated observations dominate
the window. The next frame starts a fresh model context; the last sampled frame
is also available to questions after video EOF. A `session_recovered` event
explicitly informs the client that older conversation/visual history was cleared.
User-answer repetitions do not trigger recovery. The rapid-motion frontend
preset opts in; history-sensitive use can leave it off.

Single/multi-turn `ask` accepts `n`, `max-pixels`, sampling and repetition
settings too. Streaming completion events carry actual token counts, first-token
latency, vision/prefill/decode times, and `finish_reason`. `length` explicitly
means a user-configured output cap; the runtime still closes the turn in KV.

Existing environment overrides remain available: `MOSS_VIDEO_MP`, `MOSS_XCTX`,
`MOSS_RT_SINK`, `MOSS_RT_SILENCE`, `MOSS_NO_STATIC`, and `MOSS_DUMP_DIR`.
Keep static decode enabled and tensor dumping disabled for normal inference.

## Reproduce or change C++

The local `skyvision/` checkout is ignored and is the place to edit compiled
sources. Export its diff to `patches/moss-runtime.patch`, review the diff, and
commit/push the workflow repository. The workflow checks patch compatibility
and runs syntax/UTF-8/option/argument-quoting tests before installing CUDA or
starting the expensive build. `build-info.json` records source refs, patch and
binary hashes. Runtime setting changes need no code edits or rebuilding.

Local real-model checks also cover a forced two-token cutoff and recovery,
realtime question interruption, bounded queues, and repeated text-only turns
that force eviction in a 1024-token context. Run `tests/runtime_smoke.py` with
explicit binary/model/web paths; add `--context 1024 --cross-rows 1024 --long-cache`
to exercise the eviction boundary. `--visual-cache` fills the visual window,
changes the blue circle to green, and checks the newest image after eviction.
For an 8192-row visual window use `--visual-frames 50`.
Model tests need Pillow and the user's GGUF;
they use deterministic colored shapes rather than a live camera.

`tests/media_source_smoke.py` checks Chinese/space-containing FFmpeg source paths,
video EOF, preview decoding, upload cleanup, owned-child shutdown and early HTTP
disconnects. It also verifies the two-second camera filter against generated
30/60 FPS sources; it does not access a real camera. `tests/benchmark_backend.py`
compares retained/rebuilt binaries using the same image and question. These
scripts write reports and logs under the explicitly selected output directory.

`tests/long_stream_regression.py` accepts an explicitly selected local video,
streams resized frames through the browser-frame API, and monitors Windows
private bytes on every turn. It starts and stops only its own test process,
aborts excessive memory growth before exhausting system commit, and reports
steady-state growth after warmup. Use at least 600 frames to exceed the 480-turn
failure seen with the old static graph owner; `--frames 1000` covers repeated
visual-cache compaction. `--disable-static` and `--disable-cuda-graphs` are
diagnostic controls for retained old binaries, not normal deployment defaults.
Video bytes and generated frames stay local; do not upload them to Actions.

Protocol reference: [MOSS realtime documentation](https://github.com/OpenMOSS/MOSS-VL/blob/main/inference/realtime/README.md).
