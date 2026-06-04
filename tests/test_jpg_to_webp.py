# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, hakergeniusz
"""Tests for jpg_to_webp.py — the parallel image converter."""

import subprocess
import sys
from pathlib import Path

from PIL import Image
import piexif
import pytest

# Import functions under test
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jpg_to_webp import convert_one, check_format_support


# ---------------------------------------------------------------------------
# Unit tests for convert_one
# ---------------------------------------------------------------------------


class TestConvertOneWebP:
    """convert_one() with --format webp (the default)."""

    def test_jpg_to_webp_basic(self, sample_jpg, tmp_dir):
        out = tmp_dir / "out.webp"
        status, msg = convert_one((sample_jpg, out, "webp", 85, False))
        assert status == "OK", msg
        assert out.exists()
        # Verify it's a valid WebP
        img = Image.open(out)
        assert img.format == "WEBP"

    def test_png_to_webp_basic(self, sample_png, tmp_dir):
        out = tmp_dir / "out.webp"
        status, msg = convert_one((sample_png, out, "webp", 85, False))
        assert status == "OK", msg
        assert out.exists()

    def test_webp_to_webp(self, sample_webp, tmp_dir):
        out = tmp_dir / "out.webp"
        status, msg = convert_one((sample_webp, out, "webp", 85, False))
        assert status == "OK", msg
        assert out.exists()

    def test_lossless_webp(self, sample_jpg, tmp_dir):
        out = tmp_dir / "out.webp"
        status, msg = convert_one((sample_jpg, out, "webp", 85, True))
        assert status == "OK", msg
        assert out.exists()

    def test_exif_preserved(self, sample_jpg, tmp_dir):
        out = tmp_dir / "out.webp"
        status, msg = convert_one((sample_jpg, out, "webp", 85, False))
        assert status == "OK", msg
        # WebP should carry EXIF
        img = Image.open(out)
        exif = img.info.get("exif")
        assert exif is not None, "EXIF data was not preserved"

    def test_quality_parameter(self, sample_jpg, tmp_dir):
        out_low = tmp_dir / "low.webp"
        out_high = tmp_dir / "high.webp"
        convert_one((sample_jpg, out_low, "webp", 10, False))
        convert_one((sample_jpg, out_high, "webp", 100, False))
        # Higher quality should generally produce a larger file
        assert out_high.stat().st_size >= out_low.stat().st_size

    def test_output_path_creates_dirs(self, sample_jpg, tmp_dir):
        out = tmp_dir / "sub" / "dir" / "out.webp"
        status, msg = convert_one((sample_jpg, out, "webp", 85, False))
        assert status == "OK", msg
        assert out.exists()

    def test_nonexistent_source(self, tmp_dir):
        fake = tmp_dir / "does_not_exist.jpg"
        out = tmp_dir / "out.webp"
        status, msg = convert_one((fake, out, "webp", 85, False))
        assert status == "ERR"


class TestConvertOneAVIF:
    """convert_one() with --format avif (if supported)."""

    @pytest.fixture(autouse=True)
    def _check_avif(self):
        hint = check_format_support("avif")
        if hint:
            pytest.skip(f"AVIF not available: {hint}")

    def test_jpg_to_avif(self, sample_jpg, tmp_dir):
        out = tmp_dir / "out.avif"
        status, msg = convert_one((sample_jpg, out, "avif", 85, False))
        assert status == "OK", msg
        assert out.exists()
        img = Image.open(out)
        assert img.format == "AVIF"


class TestConvertOneHEIC:
    """convert_one() with --format heic (if supported)."""

    @pytest.fixture(autouse=True)
    def _check_heic(self):
        hint = check_format_support("heic")
        if hint:
            pytest.skip(f"HEIC not available: {hint}")

    def test_jpg_to_heic(self, sample_jpg, tmp_dir):
        out = tmp_dir / "out.heic"
        status, msg = convert_one((sample_jpg, out, "heic", 85, False))
        assert status == "OK", msg
        assert out.exists()


class TestConvertOneJXL:
    """convert_one() with --format jxl (if supported)."""

    @pytest.fixture(autouse=True)
    def _check_jxl(self):
        hint = check_format_support("jxl")
        if hint:
            pytest.skip(f"JXL not available: {hint}")

    def test_jpg_to_jxl_lossy(self, sample_jpg, tmp_dir):
        out = tmp_dir / "out.jxl"
        status, msg = convert_one((sample_jpg, out, "jxl", 85, False))
        assert status == "OK", msg
        assert out.exists()


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCli:
    """End-to-end tests via subprocess."""

    SCRIPT = str(Path(__file__).resolve().parent.parent / "jpg_to_webp.py")

    def _run(self, *args, input_dir: Path | None = None) -> subprocess.CompletedProcess:
        cmd = [sys.executable, self.SCRIPT, str(input_dir)] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_dry_run(self, sample_jpg):
        result = self._run("--dry-run", input_dir=sample_jpg.parent)
        assert result.returncode == 0
        assert "would convert" in result.stdout

    def test_basic_conversion(self, sample_jpg, tmp_dir):
        out_dir = tmp_dir / "converted"
        out_dir.mkdir()
        result = self._run("--output", str(out_dir), input_dir=sample_jpg.parent)
        assert result.returncode == 0
        assert "Converted: 1" in result.stdout
        webp_files = list(out_dir.rglob("*.webp"))
        assert len(webp_files) == 1

    def test_no_images(self, tmp_dir):
        result = self._run(input_dir=tmp_dir)
        assert result.returncode == 0
        assert "No supported image files found" in result.stdout

    def test_invalid_folder(self):
        result = self._run(input_dir=Path("/nonexistent/path"))
        assert result.returncode != 0
        assert "not a directory" in result.stdout

    def test_format_avif_cli(self, sample_jpg, tmp_dir):
        hint = check_format_support("avif")
        if hint:
            pytest.skip(f"AVIF not available: {hint}")
        out_dir = tmp_dir / "avif_out"
        out_dir.mkdir()
        result = self._run("--format", "avif", "--output", str(out_dir), input_dir=sample_jpg.parent)
        assert result.returncode == 0
        avif_files = list(out_dir.rglob("*.avif"))
        assert len(avif_files) == 1
