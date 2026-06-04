# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`gphotos-storage-saver` is a pair of Python scripts to reclaim storage from a Google Photos library:

1. **`jpg_to_webp.py`** — Parallel image converter (JPG/PNG/WebP/AVIF/HEIC/JXL → WebP/AVIF/HEIC/JXL) with EXIF preservation
2. **`takeout_merge.py`** — Google Takeout metadata merger that embeds JSON sidecar metadata back into media files (images via EXIF, video via ffmpeg + XMP sidecars)

Typical workflow: Takeout export → merge metadata → convert to modern format → re-upload.

## Development Commands

```bash
# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_jpg_to_webp.py -v
python -m pytest tests/test_takeout_merge.py -v

# Run a single test class or test
python -m pytest tests/test_jpg_to_webp.py::TestConvertOneWebP -v
python -m pytest tests/test_jpg_to_webp.py::TestConvertOneWebP::test_jpg_to_webp -v

# Run with coverage
python -m pytest tests/ -v --cov=. --cov-report=term-missing

# Smoke test scripts
python jpg_to_webp.py --help
python takeout_merge.py --help
```

The project uses `uv` as its package manager (`uv.lock` exists) but CI uses plain `pip`. Python 3.14 is the target version; CI tests 3.11–3.14.

## Architecture

### No `src/` layout

All three Python scripts (`jpg_to_webp.py`, `takeout_merge.py`, `generate_test_data.py`) live at the project root. Tests manually add the parent directory to `sys.path` in `conftest.py`.

### `jpg_to_webp.py` — Image Converter

- **Format registry:** `FORMATS` dict maps CLI format names to `(Pillow format string, file extension, supports_lossless)` tuples
- **Plugin system:** `register_plugins()` dynamically imports optional Pillow plugins (`pillow_heif`, `pillow_avif`, `pillow_jxl`). Plugins are re-registered inside each `ProcessPoolExecutor` worker since child processes don't inherit imports.
- **JXL fast-path:** `_transcode_jpeg_to_jxl()` uses `cjxl --lossless_jpeg=1` for verbatim JPEG-in-JXL transcoding (~20% smaller, perfectly reversible). Falls back to Pillow pixel-lossless on failure.
- **`convert_one(args)`**: Top-level picklable function for `ProcessPoolExecutor`. Handles EXIF extraction/sanitization (strips MakerNote), mode normalization, format-specific save kwargs. Returns `(status, message)` tuples.

### `takeout_merge.py` — Metadata Merger

- **JSON sidecar discovery:** `_json_candidates()` returns candidate paths in priority order covering Google Takeout naming conventions (`.supplemental-metadata.json`, `.json`, plus `(N)` duplicate-counter suffixes). `build_json_index()` does an O(n) directory walk for efficient lookup.
- **EXIF building:** `build_exif()` merges Google Takeout JSON fields (title, description, people, dates, GPS, views) into a piexif EXIF dict.
- **XMP sidecars:** `write_xmp_sidecar()` generates `.xmp` files with Dublin Core, XMP, EXIF, Photoshop, IPTC, and Google Photo namespaces.
- **Image processing:** `process_image()` opens image → extracts existing EXIF → builds merged EXIF → flattens alpha for non-alpha-capable formats → saves with `quality="keep"` for JPEG.
- **Video processing:** `process_video()` uses `ffmpeg -y -i input -c copy -map_metadata 0 [tags] -movflags use_metadata_tags output` (stream copy, no re-encode). Falls back to `shutil.copy2` + XMP sidecar if ffmpeg is unavailable.

### `generate_test_data.py` — Test Data Generator

Generates fake image + JSON sidecar pairs mimicking real Google Takeout structure. Creates random noise images with realistic metadata (timestamps, GPS coordinates of real cities, people names).

### Tests

- `tests/conftest.py` — Shared fixtures: `tmp_dir`, `make_image` (factory with optional EXIF via piexif), `make_json_sidecar` (Google Takeout-style JSON), and sample image fixtures
- `tests/test_jpg_to_webp.py` — Unit tests for `convert_one()` + end-to-end CLI subprocess tests
- `tests/test_takeout_merge.py` — Unit tests for helper functions, EXIF building, JSON discovery, image processing, and CLI subprocess tests

## Dependencies

- **Core:** `pillow>=11.0`, `piexif>=1.1.3`
- **Optional format plugins:** `pillow-avif-plugin`, `pillow-heif`, `pillow-jxl-plugin`
- **Non-Python:** `cjxl` (for JXL lossless JPEG transcoding), `ffmpeg` (for MP4 metadata embedding)

Note: `pytest`, `pytest-cov`, and `ruff` are listed under `[project.dependencies]` in `pyproject.toml` but are dev-only tools. There is no `[tool.ruff]` configuration section and no linting step in CI.
