# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, hakergeniusz
"""Shared fixtures for all tests."""

import json
import os
import tempfile
from pathlib import Path

from PIL import Image
import piexif
import pytest


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def make_image(tmp_dir):
    """Factory fixture that creates a small image with EXIF metadata."""

    def _make(
        name: str = "photo.jpg",
        size: tuple[int, int] = (64, 48),
        color: tuple[int, int, int] = (128, 64, 32),
        exif: dict | None = None,
    ) -> Path:
        path = tmp_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", size, color)

        if exif is not None:
            exif_bytes = piexif.dump(exif)
            img.save(path, format="JPEG", exif=exif_bytes)
        else:
            img.save(path, format="JPEG")

        return path

    return _make


@pytest.fixture
def make_json_sidecar(tmp_dir):
    """Factory fixture that creates a Google Takeout-style JSON sidecar."""

    def _make(
        image_path: Path,
        title: str = "Test photo",
        description: str = "A test description",
        lat: float = 52.2297,
        lon: float = 21.0122,
        alt: float = 110.0,
        people: list[dict] | None = None,
        timestamp: str = "1700000000",
    ) -> Path:
        stem = image_path.stem
        ext = image_path.suffix
        json_path = image_path.parent / f"{stem}{ext}.supplemental-metadata.json"

        meta = {
            "title": title,
            "description": description,
            "imageViews": "42",
            "creationTime": {"timestamp": timestamp, "formatted": "Nov 14, 2023, 11:33:20 PM UTC"},
            "photoTakenTime": {"timestamp": timestamp, "formatted": "Nov 14, 2023, 11:33:20 PM UTC"},
            "photoLastModifiedTime": {"timestamp": timestamp, "formatted": "Nov 14, 2023, 11:33:20 PM UTC"},
            "geoData": {
                "latitude": lat,
                "longitude": lon,
                "altitude": alt,
                "latitudeSpan": 0.005,
                "longitudeSpan": 0.005,
            },
            "geoDataExif": {
                "latitude": lat,
                "longitude": lon,
                "altitude": alt,
                "latitudeSpan": 0.005,
                "longitudeSpan": 0.005,
            },
            "people": people or [],
            "url": "https://photos.google.com/photo/abc123",
        }

        json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return json_path

    return _make


@pytest.fixture
def sample_jpg(make_image):
    """A single sample JPEG with basic EXIF."""
    exif = {
        "0th": {piexif.ImageIFD.Make: b"TestCamera"},
        "Exif": {},
        "GPS": {},
        "1st": {},
    }
    return make_image("sample.jpg", exif=exif)


@pytest.fixture
def sample_png(tmp_dir):
    """A sample PNG without EXIF."""
    path = tmp_dir / "sample.png"
    img = Image.new("RGB", (64, 48), (200, 100, 50))
    img.save(path, format="PNG")
    return path


@pytest.fixture
def sample_webp(tmp_dir):
    """A sample WebP without EXIF."""
    path = tmp_dir / "sample.webp"
    img = Image.new("RGB", (64, 48), (50, 100, 200))
    img.save(path, format="WEBP")
    return path
