# MOSS realtime crash repair validation

Validated locally on 2026-11-11 with the existing MOSS GGUF and an RTX 5070 Ti
Laptop GPU. User video, extracted images and full inference logs stayed local.

The static decode rebuild allocated `dec_ctx`, then `build_text_graph` allocated
a separate `dg.ctx`. The actual graph context was not adopted by the owner,
leaking 64 MiB on each rebuild. New visual rows caused frequent rebuilds; the
original process failed after about 480 completed turns. A bounded old-binary
run was stopped after 15 frames at 1410 MiB of private-memory growth.

The repair adopts `dg.ctx` as `dec_ctx` and releases it on rebuild or failure.
Static decode and CUDA graphs remain enabled. Repetition also occurred with
static decode and CUDA graphs disabled, so it required a separate mitigation:
an updated rapid-motion prompt, penalty 1.10 over 256 tokens, and opt-in
`recover-repeats=1`. Recovery resets older model history after the same trimmed
autonomous observation appears three times in the latest 64 nonempty
observations. Capture, original task, latest question and last frame are kept.

## Installed build

Build commit: `d160e1a694f47461c19d66ed03e61e2325bc288e`.
[Successful CUDA Actions run](https://github.com/KonziaX/skyvision-ci/actions/runs/42959506078).
The artifact was verified and installed in the separate local directory
`skyvision-windows-cuda-runtime-d160e1a/`; the old package remains available.

Executable SHA-256:
`5d119ba26010919e1952e1d2f5eb4ff10c1edd9305175aa772b1d3fb452456ce`.

Patch SHA-256:
`8a400320fda1e75742c8cd2c2a2bbb5ab18b46052bb0f4f3e062be0abdf31896`.

SkyVision ref: `a4822e5065a08f1198b9d6b1cffc274626172bd1`.
ggml ref: `cd6d2b75f5d3e4b2f3fe8e8fbc6c8b84ac80e74a`.
CUDA 13.0, architecture 120, runtime API 2, CUDA graphs enabled.

## Local results

| Check | Result |
| --- | --- |
| Fixed CPU, 100 frames | Warmed-up private-memory growth 0.63 MiB |
| Fixed CUDA, 1000 frames, context 4096 / visual rows 8192 | Completed without allocation failure; 516.38 seconds |
| Private memory after turn 150 | 10342.16–10396.42 MiB; bounded rather than per-rebuild growth |
| Loop recovery during the 1000-frame run | Four triggers; 233 nonempty observations, 217 distinct |
| Question immediately after CUDA recovery, no new frame | Visible answer in 289.04 ms |
| Browser playback of the full 69.77-second source | Completed; follow-up answered in about 0.34 seconds |
| FFmpeg playback of the full source | 140 captured frames, four dropped; EOF and follow-up worked |
| CUDA runtime smoke, context / visual rows 1024 | Image answer, forced cutoff recovery, text and visual eviction, question interruption, bounded queue passed |
| Stop / load status in runtime smoke | 48.44 ms / maximum 20.87 ms |
| Launcher verification | Model, tokenizer, frontend, FFmpeg and executable hash checks passed |

These are memory and runtime regression checks, not a model-accuracy benchmark.
The final text-only question after the 1000-frame stress run returned silence;
normal full-playback follow-ups and the question immediately after recovery
returned visible answers. Recovery detects exact text loops and may also fire
when a real action repeats; it does not eliminate all semantic repetition or
recognition errors. It is off by default at the API and in daily/reading
presets. The frontend rapid-motion preset enables it and explains that older
model history is cleared.

All test-owned servers were stopped. The validation/documentation commit does
not change the runtime patch and needs no additional CUDA build.
