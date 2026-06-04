# gphotos-storage-saver

[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD%203--Clause-blue.svg)](LICENSE)

A pair of Python scripts to help you reclaim storage from your Google Photos
library — either by converting images to modern compressed formats, or by
merging Google Takeout metadata sidecars back into your media files so nothing
is lost when you re-upload.

---

## Scripts

### `jpg_to_webp.py` — Parallel Image Converter

Converts images (JPG, PNG, WebP, AVIF, HEIC, JXL) to a modern output format
(WebP, AVIF, HEIC, or JXL) in parallel, preserving all EXIF metadata.

**Supported output formats:**

| Format | Lossy | Lossless | Extra dependency |
|--------|-------|----------|-----------------|
| WebP   | yes   | yes      | — (Pillow built-in) |
| AVIF   | yes   | yes      | `pillow-avif-plugin` |
| HEIC   | yes   | no       | `pillow-heif` |
| JXL    | yes   | yes*     | `pillow-jxl-plugin` + `cjxl` |

\* JXL lossless for JPEG sources uses `cjxl --lossless_jpeg=1` to store the
original JPEG bitstream verbatim (~20% smaller, perfectly reversible).

**Base requirements:**

```bash
pip install Pillow piexif
```

**Usage:**

```bash
# Convert all images in a folder to WebP (default)
python jpg_to_webp.py /path/to/folder

# Convert to AVIF
python jpg_to_webp.py /path/to/folder --format avif

# Lossless JXL (JPEG transcode + pixel-lossless fallback)
python jpg_to_webp.py /path/to/folder --format jxl --lossless

# Write to a separate output folder, 8 parallel workers
python jpg_to_webp.py /path/to/folder --output /path/to/out --workers 8

# Dry run — see what would happen without writing anything
python jpg_to_webp.py /path/to/folder --dry-run

# Delete originals after successful conversion
python jpg_to_webp.py /path/to/folder --delete-src
```

---

### `takeout_merge.py` — Google Takeout Metadata Merger

Merges metadata from Google Takeout `.json` sidecar files into the
corresponding media files (JPG, PNG, WebP, AVIF, HEIC, JXL, MP4).

Google Takeout strips EXIF metadata from your photos and stores it in separate
JSON files. This script puts it back — into the image EXIF directly, and into
XMP sidecars for all formats including video.

**Supported media types:**

- **Images** (JPG, PNG, WebP, AVIF, HEIC, JXL) — metadata embedded into EXIF
- **Video** (MP4) — metadata embedded via ffmpeg (stream copy, no re-encode),
  with XMP sidecar fallback when ffmpeg is unavailable

**Requirements:**

```bash
pip install Pillow piexif
# Optional, for non-JPEG/PNG formats:
pip install pillow-heif pillow-jxl-plugin pillow-avif-plugin
# Optional, for MP4 metadata embedding:
#   install ffmpeg (e.g. `brew install ffmpeg`, `apt install ffmpeg`)
```

**Usage:**

```bash
# Merge metadata in-place (modifies files directly)
python takeout_merge.py /path/to/takeout/folder

# Write to a separate output folder
python takeout_merge.py /path/to/folder --output /path/to/output --copy

# Dry run
python takeout_merge.py /path/to/folder --dry-run

# Skip XMP sidecar writing
python takeout_merge.py /path/to/folder --no-xmp
```

---

## Typical Workflow

1. **Export** your Google Photos library via [Google Takeout](https://takeout.google.com/).
2. **Merge metadata** back into the files:
   ```bash
   python takeout_merge.py ~/Downloads/Takeout --output ~/PhotosRestored --copy
   ```
3. **Convert** to a space-saving format:
   ```bash
   python jpg_to_webp.py ~/PhotosRestored --format jxl --lossless --delete-src
   ```
4. Re-upload to Google Photos (or your preferred cloud storage) with full
   metadata intact and significantly reduced file sizes.

---

## License

This project is licensed under the [BSD 3-Clause License](LICENSE).
