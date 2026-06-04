# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, hakergeniusz
"""Tests for takeout_merge.py — the Google Takeout metadata merger."""

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image
import piexif
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from takeout_merge import (
    build_exif,
    build_json_index,
    find_json_for_media,
    process_image,
    ts_to_exif_str,
    to_rational,
    decimal_to_dms,
    safe_ascii,
    IMAGE_EXTS,
)


# ---------------------------------------------------------------------------
# Helper-function tests
# ---------------------------------------------------------------------------


class TestTsToExifStr:
    def test_valid_timestamp(self):
        result = ts_to_exif_str("1700000000")
        assert result is not None
        assert ":" in result  # "YYYY:MM:DD HH:MM:SS" format

    def test_zero(self):
        result = ts_to_exif_str("0")
        assert result is not None

    def test_invalid(self):
        assert ts_to_exif_str("not_a_number") is None

    def test_none_like(self):
        assert ts_to_exif_str("") is None


class TestToRational:
    def test_zero(self):
        assert to_rational(0) == (0, 1)

    def test_positive(self):
        numer, denom = to_rational(12.34)
        assert denom == 100
        assert numer > 0

    def test_negative_uses_abs(self):
        numer, denom = to_rational(-5.0)
        assert numer > 0  # abs value


class TestDecimalToDms:
    def test_zero(self):
        dms = decimal_to_dms(0.0)
        assert dms == [(0, 1), (0, 1), (0, 1000)]

    def test_positive(self):
        dms = decimal_to_dms(52.2297)
        assert len(dms) == 3
        assert dms[0] == (52, 1)  # degrees

    def test_negative_uses_abs(self):
        dms = decimal_to_dms(-33.8688)
        assert dms[0][0] == 33  # abs


class TestSafeAscii:
    def test_plain(self):
        assert safe_ascii("hello") == b"hello"

    def test_unicode_replaced(self):
        result = safe_ascii("café")
        assert b"?" in result or b"caf" in result

    def test_max_len(self):
        result = safe_ascii("hello world", max_len=5)
        assert result == b"hello"


class TestBuildExif:
    def test_title_and_description(self):
        meta = {"title": "My Photo", "description": "A nice day"}
        exif = build_exif(meta, {})
        zeroth = exif["0th"]
        assert zeroth[piexif.ImageIFD.ImageDescription] == b"My Photo"

    def test_gps(self):
        meta = {
            "geoDataExif": {"latitude": 52.2297, "longitude": 21.0122, "altitude": 110.0}
        }
        exif = build_exif(meta, {})
        gps = exif["GPS"]
        assert piexif.GPSIFD.GPSLatitude in gps
        assert piexif.GPSIFD.GPSLongitude in gps
        assert gps[piexif.GPSIFD.GPSLatitudeRef] == b"N"
        assert gps[piexif.GPSIFD.GPSLongitudeRef] == b"E"

    def test_no_gps_when_zero(self):
        meta = {
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
        }
        exif = build_exif(meta, {})
        # GPS should not be populated when lat/lon are both 0
        assert piexif.GPSIFD.GPSLatitude not in exif["GPS"]

    def test_people(self):
        meta = {"people": [{"name": "Alice"}, {"name": "Bob"}]}
        exif = build_exif(meta, {})
        author = exif["0th"][piexif.ImageIFD.XPAuthor]
        # XPAuthor is stored as UTF-16-LE + null terminator
        assert "Alice".encode("utf-16-le") in author
        assert "Bob".encode("utf-16-le") in author

    def test_software_tag(self):
        meta = {}
        exif = build_exif(meta, {})
        assert b"Google" in exif["0th"][piexif.ImageIFD.Software]

    def test_date_from_photo_taken(self):
        meta = {"photoTakenTime": {"timestamp": "1700000000"}}
        exif = build_exif(meta, {})
        exif_ifd = exif["Exif"]
        assert piexif.ExifIFD.DateTimeOriginal in exif_ifd
        assert piexif.ExifIFD.DateTimeDigitized in exif_ifd

    def test_views_makernote(self):
        meta = {"imageViews": "123"}
        exif = build_exif(meta, {})
        assert b"GooglePhotosViews=123" in exif["Exif"][piexif.ExifIFD.MakerNote]


# ---------------------------------------------------------------------------
# JSON sidecar discovery tests
# ---------------------------------------------------------------------------


class TestFindJsonForMedia:
    def test_exact_match(self, tmp_dir):
        media = tmp_dir / "photo.jpg"
        media.touch()
        json_file = tmp_dir / "photo.jpg.supplemental-metadata.json"
        json_file.write_text("{}")
        result = find_json_for_media(media)
        assert result == json_file

    def test_json_extension(self, tmp_dir):
        media = tmp_dir / "photo.jpg"
        media.touch()
        json_file = tmp_dir / "photo.jpg.json"
        json_file.write_text("{}")
        result = find_json_for_media(media)
        assert result == json_file

    def test_no_match(self, tmp_dir):
        media = tmp_dir / "photo.jpg"
        media.touch()
        result = find_json_for_media(media)
        assert result is None

    def test_with_index(self, tmp_dir):
        media = tmp_dir / "photo.jpg"
        media.touch()
        json_file = tmp_dir / "photo.jpg.supplemental-metadata.json"
        json_file.write_text("{}")
        index = build_json_index(tmp_dir)
        result = find_json_for_media(media, index)
        assert result == json_file


# ---------------------------------------------------------------------------
# process_image tests
# ---------------------------------------------------------------------------


class TestProcessImage:
    def test_basic_merge(self, sample_jpg, make_json_sidecar, tmp_dir):
        json_path = make_json_sidecar(sample_jpg)
        out = tmp_dir / "merged.jpg"
        status = process_image(sample_jpg, json_path, out, write_xmp=False)
        assert status == "OK"
        assert out.exists()

        # Verify EXIF was written
        img = Image.open(out)
        exif_data = img.info.get("exif")
        assert exif_data is not None

    def test_xmp_sidecar_written(self, sample_jpg, make_json_sidecar, tmp_dir):
        json_path = make_json_sidecar(sample_jpg)
        out = tmp_dir / "merged.jpg"
        process_image(sample_jpg, json_path, out, write_xmp=True)
        xmp_path = tmp_dir / "merged.xmp"
        assert xmp_path.exists()
        xmp_content = xmp_path.read_text()
        assert "Test photo" in xmp_content

    def test_skip_existing_output(self, sample_jpg, make_json_sidecar, tmp_dir):
        json_path = make_json_sidecar(sample_jpg)
        out = tmp_dir / "merged.jpg"
        out.touch()  # create empty file
        status = process_image(sample_jpg, json_path, out, write_xmp=False)
        assert "SKIP" in status

    def test_png_merge(self, sample_png, tmp_dir):
        json_path = tmp_dir / "sample.png.supplemental-metadata.json"
        json_path.write_text(json.dumps({
            "title": "PNG test",
            "description": "",
            "imageViews": "0",
            "creationTime": {"timestamp": "1700000000", "formatted": ""},
            "photoTakenTime": {"timestamp": "1700000000", "formatted": ""},
            "geoData": {"latitude": 0, "longitude": 0, "altitude": 0, "latitudeSpan": 0, "longitudeSpan": 0},
            "geoDataExif": {"latitude": 0, "longitude": 0, "altitude": 0, "latitudeSpan": 0, "longitudeSpan": 0},
            "people": [],
            "url": "",
        }))
        out = tmp_dir / "merged.png"
        status = process_image(sample_png, json_path, out, write_xmp=False)
        assert status == "OK"

    def test_bad_json(self, sample_jpg, tmp_dir):
        bad_json = tmp_dir / "bad.json"
        bad_json.write_text("not valid json{{{")
        out = tmp_dir / "out.jpg"
        status = process_image(sample_jpg, bad_json, out, write_xmp=False)
        assert "SKIP" in status


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCli:
    SCRIPT = str(Path(__file__).resolve().parent.parent / "takeout_merge.py")

    def _run(self, *args, input_dir: Path | None = None) -> subprocess.CompletedProcess:
        cmd = [sys.executable, self.SCRIPT, str(input_dir)] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_dry_run(self, sample_jpg, make_json_sidecar):
        json_path = make_json_sidecar(sample_jpg)
        result = self._run("--dry-run", input_dir=sample_jpg.parent)
        assert result.returncode == 0
        assert "would merge" in result.stdout

    def test_basic_merge(self, sample_jpg, make_json_sidecar, tmp_dir):
        json_path = make_json_sidecar(sample_jpg)
        out_dir = tmp_dir / "merged"
        out_dir.mkdir()
        result = self._run("--output", str(out_dir), input_dir=sample_jpg.parent)
        assert result.returncode == 0
        assert "Merged: 1" in result.stdout

    def test_no_media(self, tmp_dir):
        result = self._run(input_dir=tmp_dir)
        assert result.returncode == 0
        assert "No supported media files found" in result.stdout

    def test_invalid_folder(self):
        result = self._run(input_dir=Path("/nonexistent/path"))
        assert result.returncode != 0
        assert "not a directory" in result.stdout

    def test_copy_flag(self, sample_jpg, make_json_sidecar, tmp_dir):
        """Files without JSON sidecars are copied when --copy is set."""
        # Create a jpg with no json sidecar
        lone = sample_jpg.parent / "lone.jpg"
        sample_jpg.rename(lone)
        # Keep the json for sample_jpg so it's not the only file
        out_dir = tmp_dir / "out"
        out_dir.mkdir()
        result = self._run("--output", str(out_dir), "--copy", input_dir=lone.parent)
        assert result.returncode == 0
