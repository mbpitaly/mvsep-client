# Changelog

All notable changes to MVSep Client are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-12

### Added
- Initial release.
- MVSep API engine: 117+ models, 4 mirrors (main/de/de2/hk), 6 output formats, serial queue.
- Local engine: audio-separator CUDA, single-stem or all stems, `.ckpt`/`.onnx` models.
- Drag & drop file queue (tkinterdnd2).
- Intelligent stem renaming (`piano.flac`, `lead vocals.flac`) with collision-safe output (`piano_1.flac`).
- Discard-secondary-stem toggle.
- ffmpeg post-processing: fold-to-mono, soxr resample (precision 28, Lipshitz dither).
- Dark theme (`#1e1e1e` / teal `#00b4d8` accent) and dead-centered window.
- First-run token auto-import from `~/.mvsep_cli_config`.
- Config persistence at `%APPDATA%\MVSepClient\config.json`.
