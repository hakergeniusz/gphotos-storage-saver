# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`gphotos-storage-saver` is a pair of standalone Python CLI scripts to reclaim Google Photos storage:

1. **`jpg_to_webp.py`** — Parallel image converter (JPG/PNG/WebP/AVIF/HEIC/JXL → WebP/AVIF/HEIC/JXL) with EXIF preservation
2. **`takeout_merge.py`** — Google Takeout metadata merger that embeds JSON sidecar metadata back into media files (images via EXIF, video via ffmpeg + XMP sidecars)

Typical workflow: Takeout export → merge metadata → convert to modern format → re-upload.

## Development Commands

```bash
# Run all tests
python -m pytest tests/ -v

# Run a single test file / class / test
python -m pytest tests/test_jpg_to_webp.py -v
python -m pytest tests/test_jpg_to_webp.py::TestConvertOneWebP -v
python -m pytest tests/test_jpg_to_webp.py::TestConvertOneWebP::test_jpg_to_webp -v

# Run with coverage
python -m pytest tests/ -v --cov=. --cov-report=term-missing

# Smoke test scripts
python jpg_to_webp.py --help
python takeout_merge.py --help
```

Python 3.14 is the target version; CI tests 3.11–3.14. The project uses `uv` (`uv.lock` exists) but CI uses plain `pip`.

## Architecture

### No `src/` layout

All Python scripts live at the project root. `tests/conftest.py` adds the parent directory to `sys.path` so tests can import them directly.

### `jpg_to_webp.py` — Image Converter

- **`FORMATS` registry** — dict mapping CLI format names to `(Pillow format string, file extension, supports_lossless)` tuples. Add new output formats here.
- **Plugin system** — `register_plugins()` dynamically imports optional Pillow plugins (`pillow_heif`, `pillow_avif`, `pillow_jxl`). Plugins are re-registered inside each `ProcessPoolExecutor` worker since child processes don't inherit parent imports.
- **JXL fast-path** — `_transcode_jpeg_to_jxl()` shells out to `cjxl --lossless_jpeg=1` for verbatim JPEG-in-JXL transcoding (~20% smaller, perfectly reversible). Falls back to Pillow pixel-lossless on failure.
- **`convert_one(args)`** — top-level picklable function (required for `ProcessPoolExecutor`). Handles EXIF extraction/sanitization (strips MakerNote), mode normalization, format-specific save kwargs. Returns `(status, message)` tuples.

### `takeout_merge.py` — Metadata Merger

- **JSON sidecar discovery** — `_json_candidates()` returns candidate paths in priority order covering Google Takeout naming conventions (`.supplemental-metadata.json`, `.json`, plus `(N)` duplicate-counter suffixes). `build_json_index()` does an O(n) directory walk for efficient lookup.
- **`build_exif()`** — merges Google Takeout JSON fields (title, description, people, dates, GPS, views) into a `piexif` EXIF dict.
- **`write_xmp_sidecar()`** — generates `.xmp` files with Dublin Core, XMP, EXIF, Photoshop, IPTC, and Google Photo namespaces.
- **`process_image()`** — opens image → extracts existing EXIF → builds merged EXIF → flattens alpha for non-alpha formats → saves with `quality="keep"` for JPEG.
- **`process_video()`** — uses `ffmpeg` stream copy (`-c copy -map_metadata 0 -movflags use_metadata_tags`, no re-encode). Falls back to `shutil.copy2` + XMP sidecar if ffmpeg is unavailable.

### Tests

- `tests/conftest.py` — shared fixtures: `tmp_dir`, `make_image` (factory with optional EXIF via piexif), `make_json_sidecar` (Google Takeout-style JSON), and pre-built sample image fixtures (`sample_jpg`, `sample_png`, `sample_webp`)
- `tests/test_jpg_to_webp.py` — unit tests for `convert_one()` organized by output format + CLI subprocess integration tests
- `tests/test_takeout_merge.py` — unit tests for helpers (`ts_to_exif_str`, `to_rational`, `decimal_to_dms`, `safe_ascii`), `build_exif`, JSON discovery, image processing, and CLI subprocess tests

## Dependencies

- **Core (runtime):** `pillow>=11.0`, `piexif>=1.1.3`
- **Optional format plugins:** `pillow-avif-plugin`, `pillow-heif`, `pillow-jxl-plugin`
- **Non-Python:** `cjxl` (JXL lossless JPEG transcoding), `ffmpeg` (MP4 metadata embedding)
- **Dev:** `pytest`, `pytest-cov`, `ruff` — listed under `[project.dependencies]` in `pyproject.toml` for convenience, but are dev-only. There is no `[tool.ruff]` configuration section and no linting step in CI.
