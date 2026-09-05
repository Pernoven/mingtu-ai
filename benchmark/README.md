# Mingtu AI M0 benchmark

This directory contains two reproducible benchmark inputs:

- `manifest.example.json`, an empty schema example for validation-only checks.
- `manifest.e2b-smoke.json`, which references one deterministic RGB PNG generated
  from geometric shapes by the standard-library-only
  `generate_smoke_fixture.py` script.

It does not contain user screenshots, documents, model weights, or host-specific
reports. Private or licensed evaluation data must stay outside the public tree.

## Validate the empty example

From the repository root in fish:

```fish
env PYTHONPATH=src python -m mingtu_ai.benchmark benchmark/manifest.example.json --validate-only
```

Validation checks the manifest schema, rejects unknown fields and unsafe paths,
and verifies every referenced dataset entry is a regular file. It does not start
`llama-server`, access port 3001, or require a model.

## Public E2B synthetic smoke

The committed fixture is a 640x400 RGB image containing a blue square, orange
circle, and green triangle. Regenerating it produces this SHA-256 digest:
`c367ce7805a3b044a76466f4a68e33457876c3f7a5cd0c40626ee4485adf39c8`.

```fish
env PYTHONPATH=src python benchmark/generate_smoke_fixture.py
env PYTHONPATH=src python -m unittest tests.test_e2b_fixture -v

set -x MINGTU_AI_MODEL "/path/to/model.gguf"
set -x MINGTU_AI_MMPROJ "/path/to/mmproj.gguf"
set -x MINGTU_AI_LLAMA_SERVER "/path/to/llama-server"
set -x MINGTU_AI_RESULTS "/path/to/private-results"

env PYTHONPATH=src python -m mingtu_ai.benchmark \
    benchmark/manifest.e2b-smoke.json \
    --model "$MINGTU_AI_MODEL" \
    --mmproj "$MINGTU_AI_MMPROJ" \
    --llama-server "$MINGTU_AI_LLAMA_SERVER" \
    --output-dir "$MINGTU_AI_RESULTS" \
    --report-name e2b-smoke
```

One development-host run with Gemma 4 E2B UD Q4_K_XL, an F16 multimodal
projector, and llama.cpp build 10509 measured:

| Measurement | Result |
|---|---:|
| Cold start | 2,846.233 ms |
| Adapter | 0.386 ms |
| Inference | 1,448.970 ms |
| Case total | 1,449.530 ms |
| Shutdown | 364.591 ms |
| GPU memory | 41 MiB baseline; 3,134 MiB peak; 41 MiB after shutdown |
| GPU peak increase | 3,093 MiB |
| Finished-child peak RAM (`ru_maxrss`) | 3,492.254 MiB |

The owned child exited and port 3001 was released after the run. These values
are one smoke measurement, not a performance guarantee or hardware requirement.
The case establishes that the local multimodal request, strict response
contract, report pipeline, and cleanup path work together. It does **not**
validate broad real-world screenshot understanding, OCR, mixed Chinese/English
naming, specialist-term retention, or filename search quality.

## Add a private dataset

Copy `manifest.example.json`, place authorized test files under `dataset/`, and
add one case per file. Case paths must remain relative to the manifest and may
not contain `..`. The benchmark intentionally rejects symlinks and duplicate
file paths.

Useful automatic fields:

- `expected_terms`: terms that should remain in the generated stem.
- `expected_stem_tokens`: broader searchable words expected in the stem.
- `expected_language`, `expected_category`, `expected_adapter_kind`: exact-match
  checks when a stable expectation exists.
- `manual_scores`: optional 0–5 scores for `content_understanding`,
  `stem_quality`, `filename_searchability`, and `ocr_quality`.

`model_id` is report metadata only. It never downloads or selects a model.

## Run with an already installed model

```fish
env PYTHONPATH=src python -m mingtu_ai.benchmark benchmark/manifest.private.json \
    --model /absolute/path/to/model.gguf \
    --mmproj /absolute/path/to/mmproj.gguf \
    --llama-server /absolute/path/to/llama-server \
    --output-dir benchmark/results
```

The managed server is fixed to `127.0.0.1:3001`. The benchmark never probes,
opens, or stops port 3000. A queued batch shares one runtime. After the final
case, the runner shuts down only its owned runtime handle, checks that it exited,
and checks whether 3001 can be rebound without killing another process.

Reports contain the suggested stem, classification fields, scores, timings,
resource measurements, and sanitized errors. They never contain extracted file
text, image bytes, prompts, base64 payloads, or data URLs.

Before the model starts, the benchmark streams every dataset file, the model,
the optional multimodal projector, and the `llama-server` executable through
SHA-256. It also runs `llama-server --version` in an owned process group with a
5-second and 16-KiB capture limit. The optional projector is recorded with an
explicit `skipped` status and null digest when it was not supplied. Hashing uses
bounded memory, but reading multi-gigabyte model files adds disk I/O and startup
time; the report therefore records reproducibility, not a zero-cost run. Keep
the controlled dataset and runtime artifacts unchanged for the whole run. The
pre-run fingerprints identify the bytes checked at startup, but cannot bind a
file that another process modifies later to the bytes subsequently consumed.

The report directory is created with owner-only mode `0700`, and both final
files use `0600`. An existing directory must already exclude group/world access;
the benchmark rejects a shared directory instead of silently changing its
permissions. JSON and Markdown are published as a pair without following
symlinks or replacing existing paths. Reusing a report name fails with a clear
error, so choose a new `--report-name` rather than deleting prior evidence.
Ordinary write/publish failures roll back the member created by that call;
an abrupt process kill or power loss between the two filesystem publications
can still leave one member, because two paths cannot form one filesystem
transaction. Treat such a partial pair as incomplete and rerun under a new name.

## Report fields

- `cold_start_ms`: successful `ensure_started()` duration before the first case.
- Per-case `adapter_ms`, `inference_ms`, `total_ms`.
- `automatic_scores`: term/token coverage plus optional exact-match fields.
- `manual_scores` and `manual_notes`: copied from the manifest for human review.
- `resource_samples`: Python `resource.getrusage` RAM high-water marks and
  best-effort NVIDIA framebuffer-memory queries.
- `run_metadata.reproducibility`: per-case dataset digests plus model, projector,
  server executable, and bounded server build/version evidence. Digests can
  prove byte identity but do not establish model quality or file authenticity.
- `lifecycle`: queue-drained, owned-process-exited, shutdown, and port-3001
  verification results.

An `nvidia-smi` unavailable/error result is explicitly inconclusive. A sandbox
or permission failure is never interpreted as proof that a GPU is absent.

## Official references

- [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [NVIDIA System Management Interface](https://docs.nvidia.com/deploy/nvidia-smi/index.html)
- [Python resource](https://docs.python.org/3.13/library/resource.html)
- [Python argparse](https://docs.python.org/3.13/library/argparse.html)
- [Python json](https://docs.python.org/3.13/library/json.html)
- [Gemma get started](https://ai.google.dev/gemma/docs/get_started)
- [Google Gemma 4 E2B QAT GGUF metadata](https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf)
