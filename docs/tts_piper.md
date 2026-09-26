# Piper TTS Narration Provider (P7)

`src/ayce/tts_piper.py` implements the first real narration provider
behind the P3 `AudioProvider` adapter seam — **strictly opt-in**.

- **GPL isolation**: Piper (piper-tts / piper1-gpl, GPL-3.0) is invoked
  as an EXTERNAL CLI tool (`<python> -m piper ...`, explicit argv list,
  never a shell) in its own configured Python environment. Piper is
  never imported into AYCE and is not an AYCE dependency.
- **Selection**: `AYCE_PIPER_MODEL` configured → `PiperNarrationProvider`;
  otherwise the deterministic file-backed fixture provider remains the
  default and Piper is never invoked. No silent fallback exists in the
  other direction (a broken Piper fails the narration stage truthfully).
- **Truthful output**: the synthesized WAV is validated (readable,
  non-empty), duration is read from the actual WAV header, sha256 is of
  the produced bytes. No trimming, padding, resampling, or duration
  fabrication — if narration exceeds its scene duration, the existing
  timeline validation rejects it.
- **Provenance**: `provider=piper-tts`, `source=piper_tts_local`,
  `source_ref=<model file name>`; `license` is recorded ONLY from the
  explicitly configured `AYCE_PIPER_LICENSE` (voice models can carry
  different licenses — unverified means unset).
- **Health**: truthful and cheap (config presence, path existence, fast
  `-m piper --help` probe; no synthesis).
- **Rate control**: `AYCE_PIPER_LENGTH_SCALE` (>0) maps to Piper's
  `--length-scale` for fitting speech to scene durations naturally
  (P6.6 evidence: natural Piper rate ≈140 wpm vs the 150 wpm scene
  duration rule → ~0.85–0.93 fits typical derived scenes).

## Configuration (all optional; see `.env.example`)

```text
AYCE_PIPER_PYTHON=C:\path\to\piper-env\Scripts\python.exe
AYCE_PIPER_MODEL=C:\path\to\en_US-amy-medium.onnx
AYCE_PIPER_CONFIG=C:\path\to\en_US-amy-medium.onnx.json   # optional
AYCE_PIPER_LENGTH_SCALE=1.0                               # optional
AYCE_PIPER_LICENSE=<voice-model license string>           # optional, recorded in provenance
```

## Verification

- Mocked-boundary unit tests run in the normal suite (no Piper needed).
- `tests/unit/test_tts_piper.py::test_live_piper_synthesis` is a
  `live`-marked opt-in test that runs only when the Piper environment is
  configured.
