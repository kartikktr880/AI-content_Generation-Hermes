# Kokoro-82M Narration Provider (P8)

`src/ayce/tts_kokoro.py` implements the primary real narration provider
behind the P3 `AudioProvider` adapter seam — **strictly opt-in, local
execution only** (no paid API).

- **Runtime**: the official `kokoro` package (`>=0.9.4`) with weights from
  the official repository `hexgrad/Kokoro-82M` (Apache-2.0). Installed via
  the optional extra `pip install -e ".[tts-kokoro]"`; the base AYCE
  install stays stdlib + pydantic. Weights are downloaded by the package
  itself from the official Hub repo (never an unofficial mirror) and cached
  locally by `huggingface_hub`.
- **Windows native prerequisite**: PyTorch's DLLs require the **Microsoft
  Visual C++ Redistributable (x64)**. When it is absent, importing the
  engine fails with
  `[WinError 126] ... Error loading "...\torch\lib\c10.dll" or one of its
  dependencies`. `health()` performs a memoized engine-import probe and
  reports this as unavailable with the official download
  (<https://aka.ms/vs/17/release/vc_redist.x64.exe>) — AYCE never installs
  system software itself, and synthesis raises the same actionable error
  instead of a raw `OSError`.
- **Selection**: `AYCE_KOKORO_VOICE` configured → `KokoroNarrationProvider`
  (priority over Piper); otherwise the deterministic file-backed fixture
  provider remains the default and Kokoro is never imported. Piper remains
  available as a configuration-selected alternative; there is NO silent
  fallback in either direction (an in-provider fallback chain would be a new
  mechanism the architecture deliberately does not have).
- **Languages**: derived from Kokoro's own voice-name convention
  (`af_heart` → `a` = English US, `bm_george` → `b` = English GB,
  `hf_alpha` → `h` = Hindi). The Scene Contract carries only narration text,
  so no contract field was added.
- **Hindi prerequisite**: Hindi G2P runs through espeak-ng (misaki's
  `EspeakG2P`). Availability is verified BEFORE synthesis (pip runtime
  `espeakng-loader`, shipped with `misaki[en]`, or a system espeak-ng); when
  missing, the stage fails with a precise actionable error and AYCE installs
  no system software. English uses misaki's English G2P, where espeak-ng is
  only an out-of-dictionary fallback.
- **Truthful output**: text is synthesized verbatim (no SSML, trimming,
  padding or resampling). Output is 24 kHz mono 16-bit PCM WAV written with
  the stdlib `wave` module; duration is read back from the produced file and
  the format is verified (24 kHz / mono) before the record is returned. The
  sha256 is of the actual run copy.
- **Cache**: synthesis results are stored content-addressed
  (`sha256(model|voice|speed|text)`) under `AYCE_KOKORO_CACHE_DIR`
  (default `<data_dir>/narration_cache/kokoro`), so identical requests reuse
  the same real WAV instead of re-synthesizing; the run copy always lands at
  `audio/<scene_id>.wav`, so timeline/captions integration is unchanged.
- **Provenance**: `provider=kokoro-82m`, `source=kokoro_local`,
  `source_ref=<repo id>:<voice>`; `license` is recorded ONLY from the
  explicitly configured `AYCE_KOKORO_LICENSE` (never guessed).
- **Health**: truthful and cheap — missing voice/dependency/espeak-ng is
  reported with the exact install command; nothing is synthesized.

## Configuration (all optional; see `.env.example`)

```text
AYCE_KOKORO_VOICE=af_heart                        # hf_alpha for Hindi
AYCE_KOKORO_SPEED=1.0
AYCE_KOKORO_REPO_ID=hexgrad/Kokoro-82M            # official weights repo only
AYCE_KOKORO_LICENSE=Apache-2.0                    # optional, recorded in provenance
AYCE_KOKORO_CACHE_DIR=data/narration_cache/kokoro # optional override
```

## Verification

- Mocked-engine unit tests run in the normal suite (`tests/unit/
  test_tts_kokoro.py`) via the provider's explicit `pipeline_factory` test
  seam — no model download and no torch import are required.
- Live smoke verification uses the real installed runtime; without the
  optional extra (or espeak-ng for Hindi) the provider reports the exact
  missing prerequisite instead of producing fabricated audio.