#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, hakergeniusz
"""
Google Takeout Metadata Merger
================================
Merges metadata from Google Takeout .json sidecar files into the
corresponding media files (JPG, PNG, WebP, AVIF, HEIC, JXL, MP4).

Supported media types:
  • JPG / JPEG        — EXIF + XMP sidecar
  • PNG               — EXIF + XMP sidecar
  • WebP / AVIF / JXL — EXIF + XMP sidecar (via Pillow)
  • HEIC              — EXIF + XMP sidecar (requires pillow-heif)
  • MP4               — XMP sidecar only (use exiftool for in-container MP4 atoms)

Extra dependencies for non-JPEG/PNG formats:
    pip install pillow-heif        # HEIC
    pip install pillow-jxl-plugin  # JXL
    pip install pillow-avif-plugin # AVIF

JSON sidecar matching strategy:
  Google Takeout uses several naming conventions, and truncates long names.
  This script tries every known exact pattern first, then falls back to a
  prefix scan so truncated names like:
      airport_vibes_2.jpg.supplemental-met.json
  correctly match:
      airport_vibes_2.jpg

Requirements:
    pip install Pillow piexif

Usage:
    python takeout_merge.py /path/to/takeout/folder
    python takeout_merge.py /path/to/folder --output /path/to/output --copy
    python takeout_merge.py /path/to/folder --no-xmp --dry-run
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import piexif
    from PIL import Image
except ImportError:
    print("Missing dependencies. Run:  pip install Pillow piexif")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Supported extensions
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".jxl"}
VIDEO_EXTS = {".mp4"}
ALL_MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# Formats that need an optional Pillow plugin registered before use
_PLUGIN_MAP = {
    ".heic": "pillow_heif",
    ".jxl":  "pillow_jxl",
    ".avif": "pillow_avif",
}


# ---------------------------------------------------------------------------
# JSON sidecar discovery
# ---------------------------------------------------------------------------

def _json_candidates(media_path: Path) -> list[Path]:
    """
    Return candidate JSON sidecar paths in priority order.
    Covers all known Google Takeout naming conventions.
    """
    name   = media_path.name    # photo.jpg
    stem   = media_path.stem    # photo
    ext    = media_path.suffix  # .jpg
    ext_no = ext.lstrip(".")    # jpg
    parent = media_path.parent

    candidates = [
        # ── Full-length names ─────────────────────────────────────────────
        parent / f"{name}.supplemental-metadata.json",   # photo.jpg.supplemental-metadata.json
        parent / f"{name}.json",                          # photo.jpg.json
        parent / f"{stem}.supplemental-metadata.json",   # photo.supplemental-metadata.json
        parent / f"{stem}.json",                          # photo.json
        # ── Explicit extension repeated in stem ───────────────────────────
        parent / f"{stem}{ext}.supplemental-metadata.json",
        parent / f"{stem}{ext}.json",
        # ── Extension written in parens (rare) ────────────────────────────
        parent / f"{stem}({ext_no}).json",
    ]

    # ── "(N)" duplicate-counter suffixes Google appends ──────────────────
    m = re.match(r"^(.+?)(\(\d+\))$", stem)
    if m:
        base, num = m.group(1), m.group(2)
        for pat in [
            f"{base}{num}.supplemental-metadata.json",
            f"{base}{num}.json",
            f"{base}.supplemental-metadata.json",
            f"{base}.json",
        ]:
            candidates.append(parent / pat)

    return candidates


def build_json_index(input_dir: Path) -> dict[Path, list[Path]]:
    """
    Walk input_dir once and build a dict mapping each directory to a
    sorted list of all .json files in it.  Used so find_json_for_media
    never has to call iterdir() repeatedly — O(1) lookup per file.
    """
    index: dict[Path, list[Path]] = {}
    for entry in input_dir.rglob("*.json"):
        index.setdefault(entry.parent, []).append(entry)
    return index


def find_json_for_media(media_path: Path,
                        json_index: dict[Path, list[Path]] | None = None) -> Path | None:
    """
    Find the JSON sidecar for a media file.

    Step 1 — try every known exact candidate path (fast dict lookups).
    Step 2 — scan the pre-built json_index for the parent directory and
             look for any .json whose name starts with the full media
             filename, catching arbitrary truncations such as
             .supplemental-met.json or any other shortened suffix.
             Falls back to iterdir() only when no index is provided
             (e.g. called standalone in tests).
    """
    # Step 1: exact candidates
    for c in _json_candidates(media_path):
        if c.exists():
            return c

    # Step 2: prefix scan against pre-built index (O(n) over jsons in dir,
    # but called only once per file and avoids repeated iterdir() syscalls)
    prefix = media_path.name.lower()
    candidates_in_dir: list[Path]
    if json_index is not None:
        candidates_in_dir = json_index.get(media_path.parent, [])
    else:
        try:
            candidates_in_dir = [e for e in media_path.parent.iterdir()
                                  if e.suffix.lower() == ".json"]
        except PermissionError:
            return None

    for entry in candidates_in_dir:
        if entry.name.lower().startswith(prefix):
            return entry

    return None


# ---------------------------------------------------------------------------
# EXIF helpers
# ---------------------------------------------------------------------------

_U32_MAX = 4_294_967_295


def ts_to_exif_str(timestamp_str: str) -> str | None:
    try:
        ts = int(timestamp_str)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        return dt.strftime("%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return None


def to_rational(value: float, denom: int = 100) -> tuple[int, int]:
    """Float → (numerator, denominator) safe for 32-bit EXIF rational."""
    if value == 0:
        return (0, 1)
    numer = min(int(round(abs(value) * denom)), _U32_MAX)
    return (numer, denom)


def decimal_to_dms(decimal: float) -> list[tuple[int, int]]:
    """Decimal degrees → [(deg,1), (min,1), (sec*1000,1000)] rationals."""
    decimal = abs(decimal)
    degrees = int(decimal)
    minutes_f = (decimal - degrees) * 60
    minutes = int(minutes_f)
    seconds = (minutes_f - minutes) * 60
    seconds_numer = min(int(round(seconds * 1000)), _U32_MAX)
    return [(degrees, 1), (minutes, 1), (seconds_numer, 1000)]


def safe_ascii(text: str, max_len: int = 0) -> bytes:
    encoded = text.encode("ascii", errors="replace")
    return encoded[:max_len] if max_len else encoded


def build_exif(meta: dict, existing_exif: dict) -> dict:
    """Merge Google Takeout JSON metadata into a piexif EXIF dict."""
    exif = existing_exif.copy()
    for ifd in ("0th", "Exif", "GPS", "1st", "Interop"):
        exif.setdefault(ifd, {})

    zeroth   = exif["0th"]
    exif_ifd = exif["Exif"]

    # Title
    title = meta.get("title", "")
    if title and piexif.ImageIFD.ImageDescription not in zeroth:
        zeroth[piexif.ImageIFD.ImageDescription] = safe_ascii(title)
    if title:
        zeroth[piexif.ImageIFD.XPTitle] = title.encode("utf-16-le") + b"\x00\x00"

    # Description
    description = meta.get("description", "")
    if description:
        zeroth[piexif.ImageIFD.XPComment] = description.encode("utf-16-le") + b"\x00\x00"
        exif_ifd[piexif.ExifIFD.UserComment] = b"ASCII\x00\x00\x00" + safe_ascii(description, 500)

    # People
    people = meta.get("people", [])
    if people:
        names = "; ".join(p.get("name", "") for p in people if p.get("name"))
        if names:
            zeroth[piexif.ImageIFD.XPAuthor] = names.encode("utf-16-le") + b"\x00\x00"

    # Software
    zeroth[piexif.ImageIFD.Software] = b"Google Photos Takeout Merger"

    # Dates
    photo_taken = meta.get("photoTakenTime", {})
    if photo_taken.get("timestamp"):
        dt_str = ts_to_exif_str(photo_taken["timestamp"])
        if dt_str:
            exif_ifd[piexif.ExifIFD.DateTimeOriginal]  = dt_str.encode()
            exif_ifd[piexif.ExifIFD.DateTimeDigitized] = dt_str.encode()
            zeroth[piexif.ImageIFD.DateTime]            = dt_str.encode()
    elif meta.get("creationTime", {}).get("timestamp"):
        dt_str = ts_to_exif_str(meta["creationTime"]["timestamp"])
        if dt_str:
            exif_ifd.setdefault(piexif.ExifIFD.DateTimeOriginal, dt_str.encode())
            zeroth.setdefault(piexif.ImageIFD.DateTime, dt_str.encode())

    # GPS
    geo = meta.get("geoDataExif") or meta.get("geoData") or {}
    lat = geo.get("latitude", 0.0)
    lon = geo.get("longitude", 0.0)
    alt = geo.get("altitude", 0.0)
    if lat != 0.0 or lon != 0.0:
        gps = exif["GPS"]
        gps[piexif.GPSIFD.GPSVersionID]    = (2, 3, 0, 0)
        gps[piexif.GPSIFD.GPSLatitudeRef]  = b"N" if lat >= 0 else b"S"
        gps[piexif.GPSIFD.GPSLatitude]     = decimal_to_dms(lat)
        gps[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
        gps[piexif.GPSIFD.GPSLongitude]    = decimal_to_dms(lon)
        if alt:
            gps[piexif.GPSIFD.GPSAltitudeRef] = b"\x00" if alt >= 0 else b"\x01"
            gps[piexif.GPSIFD.GPSAltitude]    = to_rational(alt)

    # Views (MakerNote)
    views = meta.get("imageViews")
    if views:
        try:
            exif_ifd[piexif.ExifIFD.MakerNote] = f"GooglePhotosViews={views}".encode("ascii")
        except Exception:
            pass

    return exif


# ---------------------------------------------------------------------------
# XMP sidecar (all media types)
# ---------------------------------------------------------------------------

def write_xmp_sidecar(media_path: Path, meta: dict, output_path: Path) -> None:
    """Write a .xmp sidecar next to output_path with all JSON metadata."""

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))

    title       = esc(meta.get("title", ""))
    description = esc(meta.get("description", ""))
    url         = esc(meta.get("url", ""))
    views       = meta.get("imageViews", "")

    photo_taken = meta.get("photoTakenTime", {})
    creation    = meta.get("creationTime", {})
    taken_fmt   = esc(photo_taken.get("formatted", ""))
    created_fmt = esc(creation.get("formatted", ""))

    people_names = [esc(p.get("name", "")) for p in meta.get("people", []) if p.get("name")]

    geo      = meta.get("geoDataExif") or meta.get("geoData") or {}
    lat      = geo.get("latitude", 0.0)
    lon      = geo.get("longitude", 0.0)
    alt      = geo.get("altitude", 0.0)
    lat_span = geo.get("latitudeSpan", 0.0)
    lon_span = geo.get("longitudeSpan", 0.0)

    xmp_taken = ""
    if photo_taken.get("timestamp"):
        try:
            ts = int(photo_taken["timestamp"])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            xmp_taken = dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        except Exception:
            pass

    people_xml = "\n".join(f'            <rdf:li>{n}</rdf:li>' for n in people_names)

    xmp = f"""<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:xmp="http://ns.adobe.com/xap/1.0/"
        xmlns:exif="http://ns.adobe.com/exif/1.0/"
        xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
        xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
        xmlns:google="http://ns.google.com/photos/1.0/photo/">

      <dc:title>
        <rdf:Alt><rdf:li xml:lang="x-default">{title}</rdf:li></rdf:Alt>
      </dc:title>
      <dc:description>
        <rdf:Alt><rdf:li xml:lang="x-default">{description}</rdf:li></rdf:Alt>
      </dc:description>
      <dc:creator>
        <rdf:Seq>
{people_xml}
        </rdf:Seq>
      </dc:creator>

      <xmp:CreateDate>{xmp_taken}</xmp:CreateDate>
      <xmp:ModifyDate>{xmp_taken}</xmp:ModifyDate>
      <photoshop:DateCreated>{xmp_taken}</photoshop:DateCreated>
      <exif:DateTimeOriginal>{xmp_taken}</exif:DateTimeOriginal>

      <exif:GPSLatitude>{lat}</exif:GPSLatitude>
      <exif:GPSLongitude>{lon}</exif:GPSLongitude>
      <exif:GPSAltitude>{alt}</exif:GPSAltitude>

      <google:views>{views}</google:views>
      <google:url>{url}</google:url>
      <google:photoTakenTime>{taken_fmt}</google:photoTakenTime>
      <google:creationTime>{created_fmt}</google:creationTime>
      <google:latitudeSpan>{lat_span}</google:latitudeSpan>
      <google:longitudeSpan>{lon_span}</google:longitudeSpan>

    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""

    output_path.with_suffix(".xmp").write_text(xmp, encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-type processors
# ---------------------------------------------------------------------------

# Maps file extension to the Pillow format string used when saving
_EXT_TO_PILLOW_FMT = {
    ".jpg":  "JPEG",
    ".jpeg": "JPEG",
    ".png":  "PNG",
    ".webp": "WEBP",
    ".avif": "AVIF",
    ".heic": "HEIC",
    ".jxl":  "JXL",
}

# Formats that support EXIF natively via Pillow's exif= kwarg
_EXIF_CAPABLE = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".jxl"}

# Formats that can store alpha — don't flatten these unnecessarily
_ALPHA_CAPABLE = {".png", ".webp", ".avif", ".jxl"}


def _register_plugin(ext: str) -> None:
    """Import optional Pillow plugin for ext if needed, so it self-registers."""
    plugin = _PLUGIN_MAP.get(ext)
    if not plugin:
        return
    try:
        if plugin == "pillow_heif":
            import pillow_heif
            pillow_heif.register_heif_opener()
        else:
            __import__(plugin)
    except ImportError:
        pass  # caller will get a clear error from Pillow when saving


def process_image(media_path: Path, json_path: Path, output_path: Path,
                  write_xmp: bool = True) -> str:
    """
    Merge JSON into EXIF of any Pillow-supported image format and save.
    Handles JPG, PNG, WebP, AVIF, HEIC, JXL.
    """
    # Skip if already processed
    if output_path.exists() and output_path != media_path:
        return "SKIP  (already exists)"

    ext = media_path.suffix.lower()
    _register_plugin(ext)

    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"SKIP  (bad JSON: {e})"

    try:
        img = Image.open(media_path)
        img.load()
    except Exception as e:
        return f"SKIP  (cannot open image: {e})"

    try:
        raw_exif = img.info.get("exif", b"")
        existing_exif = piexif.load(raw_exif) if raw_exif else {}
    except Exception:
        existing_exif = {}

    new_exif = build_exif(meta, existing_exif)

    try:
        exif_bytes = piexif.dump(new_exif)
    except Exception:
        try:
            exif_bytes = piexif.dump(build_exif(meta, {}))
        except Exception as e:
            return f"SKIP  (EXIF dump failed: {e})"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    pillow_fmt = _EXT_TO_PILLOW_FMT.get(ext, "JPEG")
    is_jpeg    = ext in {".jpg", ".jpeg"}

    # Flatten alpha for formats that can't store it
    if ext not in _ALPHA_CAPABLE and img.mode in ("RGBA", "LA", "PA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    elif is_jpeg and img.mode not in ("RGB", "L", "CMYK", "YCbCr"):
        img = img.convert("RGB")

    # Build save kwargs — keep original quality for JPEG, lossless for others
    save_kwargs: dict = {"format": pillow_fmt}
    if ext in _EXIF_CAPABLE:
        save_kwargs["exif"] = exif_bytes
    if is_jpeg:
        try:
            img.save(output_path, **save_kwargs, quality="keep", subsampling="keep")
        except TypeError:
            img.save(output_path, **save_kwargs, quality=95)
    else:
        img.save(output_path, **save_kwargs)

    if write_xmp:
        write_xmp_sidecar(media_path, meta, output_path)

    return "OK"


def _ffmpeg_available() -> bool:
    """Return True if ffmpeg is on PATH."""
    import shutil as _shutil
    return _shutil.which("ffmpeg") is not None


def _meta_to_ffmpeg_tags(meta: dict) -> list[str]:
    """
    Convert Google Takeout JSON metadata to ffmpeg -metadata key=value args.
    Returns a flat list ready to be spliced into a subprocess command.
    """
    tags: dict[str, str] = {}

    # Title
    title = meta.get("title", "")
    if title:
        tags["title"] = title

    # Description / comment
    description = meta.get("description", "")
    if description:
        tags["comment"] = description
        tags["description"] = description

    # People → artist tag (semicolon-separated)
    people = meta.get("people", [])
    if people:
        names = "; ".join(p.get("name", "") for p in people if p.get("name"))
        if names:
            tags["artist"] = names

    # Date — prefer photoTakenTime, fall back to creationTime
    for key in ("photoTakenTime", "creationTime"):
        ts_str = meta.get(key, {}).get("timestamp")
        if ts_str:
            try:
                from datetime import datetime, timezone
                ts = int(ts_str)
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                # ISO 8601 — ffmpeg accepts this for creation_time
                tags["creation_time"] = dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
                tags["date"] = dt.strftime("%Y-%m-%d")
            except Exception:
                pass
            break

    # GPS — stored as a QuickTime-compatible location string ±DD.DDDD±DDD.DDDD/
    geo = meta.get("geoDataExif") or meta.get("geoData") or {}
    lat = geo.get("latitude", 0.0)
    lon = geo.get("longitude", 0.0)
    if lat != 0.0 or lon != 0.0:
        lat_str = f"{lat:+.4f}"
        lon_str = f"{lon:+.4f}"
        tags["location"] = f"{lat_str}{lon_str}/"
        tags["location-eng"] = tags["location"]

    # Google Photos URL
    url = meta.get("url", "")
    if url:
        tags["purl"] = url

    # Flatten to ffmpeg -metadata k=v list
    result = []
    for k, v in tags.items():
        result += ["-metadata", f"{k}={v}"]
    return result


def process_video(media_path: Path, json_path: Path, output_path: Path,
                  write_xmp: bool = True) -> str:
    """
    Embed metadata into the MP4 container using ffmpeg (stream copy, no
    re-encoding), then write an XMP sidecar with all fields.

    Falls back to plain copy + XMP if ffmpeg is not on PATH.
    """
    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"SKIP  (bad JSON: {e})"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if _ffmpeg_available():
        # Use a temp file so we never overwrite the source on failure
        tmp = output_path.with_suffix(".tmp.mp4")
        try:
            tags = _meta_to_ffmpeg_tags(meta)
            cmd = [
                "ffmpeg", "-y",
                "-i", str(media_path),
                "-c", "copy",          # stream copy — no re-encoding
                "-map_metadata", "0",  # keep existing container metadata
                *tags,
                "-movflags", "use_metadata_tags",
                str(tmp),
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True
            )
            if result.returncode != 0:
                tmp.unlink(missing_ok=True)
                # Fall back to plain copy on ffmpeg failure
                if media_path != output_path:
                    shutil.copy2(media_path, output_path)
                if write_xmp:
                    write_xmp_sidecar(media_path, meta, output_path)
                return "OK (XMP, ffmpeg failed — plain copy)"

            tmp.replace(output_path)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return f"SKIP  (ffmpeg error: {e})"

        if write_xmp:
            write_xmp_sidecar(media_path, meta, output_path)
        return "OK (ffmpeg)"

    else:
        # ffmpeg not available — plain copy + XMP sidecar
        if media_path != output_path:
            try:
                shutil.copy2(media_path, output_path)
            except Exception as e:
                return f"SKIP  (copy failed: {e})"
        if write_xmp:
            write_xmp_sidecar(media_path, meta, output_path)
        return "OK (XMP)"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Merge Google Takeout JSON metadata into JPG / PNG / WebP / AVIF / HEIC / JXL / MP4."
    )
    parser.add_argument("folder",
        help="Path to your Google Takeout folder (searched recursively).")
    parser.add_argument("--output", "-o", default=None,
        help="Output folder. If omitted, files are modified IN PLACE.")
    parser.add_argument("--copy", action="store_true",
        help="Copy files with no JSON sidecar to output unchanged.")
    parser.add_argument("--no-xmp", action="store_true",
        help="Skip writing XMP sidecar files.")
    parser.add_argument("--dry-run", action="store_true",
        help="Report what would happen without writing any files.")
    args = parser.parse_args()

    input_dir  = Path(args.folder).resolve()
    output_dir = Path(args.output).resolve() if args.output else None
    in_place   = output_dir is None
    write_xmp  = not args.no_xmp

    if not input_dir.is_dir():
        print(f"Error: '{input_dir}' is not a directory.")
        sys.exit(1)

    media_files = sorted(
        p for p in input_dir.rglob("*")
        if p.suffix.lower() in ALL_MEDIA_EXTS
        and "json" not in {s.lstrip(".") for s in p.suffixes}
    )

    if not media_files:
        print("No supported media files found (JPG, PNG, MP4).")
        sys.exit(0)

    by_type: dict[str, int] = {}
    for p in media_files:
        by_type[p.suffix.lower()] = by_type.get(p.suffix.lower(), 0) + 1
    type_str = "  ".join(f"{ext}: {n}" for ext, n in sorted(by_type.items()))

    print(f"Found  : {len(media_files)} file(s)  [{type_str}]")
    print(f"Images : JPG/PNG/WebP/AVIF/HEIC/JXL — EXIF embedded")
    _ffmpeg = "ffmpeg (metadata embedded)" if __import__("shutil").which("ffmpeg") else "ffmpeg not found — XMP sidecar only"
    print(f"Videos : MP4 — {_ffmpeg}")
    print(f"Mode   : {'dry-run' if args.dry_run else ('in-place' if in_place else f'output → {output_dir}')}")
    print()

    print("Indexing JSON files...", end=" ", flush=True)
    json_index = build_json_index(input_dir)
    total_jsons = sum(len(v) for v in json_index.values())
    print(f"{total_jsons} found.")
    print()

    ok = skipped = no_json = 0
    has_video = False

    for media_path in media_files:
        rel      = media_path.relative_to(input_dir)
        json_path = find_json_for_media(media_path, json_index)
        out_path  = media_path if in_place else output_dir / rel
        ext       = media_path.suffix.lower()

        if json_path is None:
            status = "NO JSON"
            no_json += 1
            if args.copy and not in_place and not args.dry_run:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(media_path, out_path)
        else:
            if args.dry_run:
                status = f"would merge ← {json_path.name}"
                ok += 1
            else:
                try:
                    if ext in IMAGE_EXTS:
                        status = process_image(media_path, json_path, out_path, write_xmp)
                    else:
                        status = process_video(media_path, json_path, out_path, write_xmp)
                        has_video = True
                except Exception as e:
                    import traceback
                    status = f"ERROR: {e}"
                    print(f"  [{status:45s}]  {rel}")
                    print(f"    {traceback.format_exc().strip()}")
                    skipped += 1
                    continue

                if status.startswith("OK"):
                    ok += 1
                else:
                    skipped += 1

        print(f"  [{status:45s}]  {rel}")

    print()
    print(f"Done.  Merged: {ok}  |  Skipped/errors: {skipped}  |  No JSON found: {no_json}")

    if has_video and not __import__("shutil").which("ffmpeg") and write_xmp:
        print()
        print("Note: ffmpeg was not found — MP4 files received XMP sidecars only.")
        print("      Install ffmpeg and re-run to embed metadata into MP4 atoms.")
        print("      Or run manually:")
        print(f"        exiftool -tagsfromfile %d%f.xmp -all:all -ext mp4 \"{output_dir or input_dir}\"")


if __name__ == "__main__":
    main()
