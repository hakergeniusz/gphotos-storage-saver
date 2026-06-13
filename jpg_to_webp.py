#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, hakergeniusz
"""
Parallel Image Converter (WebP/AVIF/HEIC/JXL/JPG/PNG → WebP / AVIF / HEIC / JXL)
===============================================================
Converts every supported image (JPG, PNG, WebP, AVIF, HEIC, JXL) to a
modern format while keeping all EXIF metadata intact.

Supported output formats:
  webp  — Pillow built-in. Lossy or lossless.
  avif  — Requires pillow-avif-plugin OR a Pillow build with libavif.
            pip install pillow-avif-plugin
  heic  — Requires pillow-heif.
            pip install pillow-heif
  jxl   — Requires pillow-jxl-plugin (also needs libjxl on your system).
            pip install pillow-jxl-plugin

JPEG XL lossless mode (--format jxl --lossless):
  For .jpg/.jpeg files, uses cjxl --lossless_jpeg=1 to store the original
  JPEG bitstream verbatim inside the JXL container — no pixel decoding.
    • Output is ~20% smaller than the source JPEG.
    • Perfectly reversible: djxl file.jxl restored.jpg gives the exact
      original JPEG byte-for-byte.
    • .png files fall back to pixel-lossless JXL automatically.
  Requires: cjxl (from the libjxl package)
    Arch:   sudo pacman -S libjxl
    Ubuntu: sudo apt install libjxl-tools
    macOS:  brew install jpeg-xl

Base requirements:
    pip install Pillow piexif

Usage:
    python jpg_to_webp.py /path/to/folder
    python jpg_to_webp.py /path/to/folder --format avif
    python jpg_to_webp.py /path/to/folder --format avif --include-same-fmt
    python jpg_to_webp.py /path/to/folder --format heic  --quality 80
    python jpg_to_webp.py /path/to/folder --format webp  --lossless
    python jpg_to_webp.py /path/to/folder --format jxl   --lossless
    python jpg_to_webp.py /path/to/folder --output /path/to/out --workers 8
    python jpg_to_webp.py /path/to/folder --dry-run
"""

import argparse
import os
import shutil
import subprocess
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:
    import piexif
    from PIL import Image, ImageOps
except ImportError:
    print("Missing dependencies. Run:  pip install Pillow piexif")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Format registry
# ---------------------------------------------------------------------------

# Maps CLI name → (Pillow format string, file extension, supports_lossless)
FORMATS: dict[str, tuple[str, str, bool]] = {
    "webp": ("WEBP", ".webp", True),
    "avif": ("AVIF", ".avif", True),   # lossless via quality=-1
    "heic": ("HEIC", ".heic", False),  # pillow-heif doesn't expose lossless
    "jxl":  ("JXL",  ".jxl",  True),  # lossless via lossless=True kwarg
}

JPEG_EXTS = {".jpg", ".jpeg"}

# All image extensions accepted as input
SRC_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".jxl"}



def check_format_support(fmt: str) -> str | None:
    """Return None if supported, or an install-hint string if not."""
    pillow_fmt = FORMATS[fmt][0]
    if pillow_fmt in Image.registered_extensions().values():
        return None
    if pillow_fmt in [f.upper() for f in Image.SAVE]:
        return None
    hints = {
        "avif": "pip install pillow-avif-plugin  (also needs libavif on your system)",
        "heic": "pip install pillow-heif",
        "jxl":  "pip install pillow-jxl-plugin  (also needs libjxl on your system)",
    }
    return hints.get(fmt, f"Pillow format '{pillow_fmt}' not available")


def check_cjxl() -> str | None:
    """Return None if cjxl is on PATH, or an install-hint string if not."""
    if shutil.which("cjxl"):
        return None
    return (
        "cjxl not found on PATH.\n"
        "  Arch:   sudo pacman -S libjxl\n"
        "  Ubuntu: sudo apt install libjxl-tools\n"
        "  macOS:  brew install jpeg-xl"
    )


def register_plugins(fmt: str) -> None:
    """Import format-specific Pillow plugins so they self-register.
    Always registers all input-format plugins too, since sources may
    include WebP/AVIF/HEIC/JXL files regardless of the output format.
    """
    # Output format plugin
    _register_single_plugin(fmt)
    # Input format plugins — always needed since any source type may appear
    for f in ("heic", "avif", "jxl"):
        if f != fmt:
            _register_single_plugin(f)


def _register_single_plugin(fmt: str) -> None:
    """Register one Pillow plugin by format name."""
    if fmt == "heic":
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
    elif fmt == "avif":
        try:
            import pillow_avif  # noqa: F401
        except ImportError:
            pass
    elif fmt == "jxl":
        try:
            import pillow_jxl  # noqa: F401
        except ImportError:
            pass


# ---------------------------------------------------------------------------
# Conversion workers (run inside child processes)
# ---------------------------------------------------------------------------

def _transcode_jpeg_to_jxl(src_path: Path, out_path: Path) -> tuple[str, str] | None:
    """
    Use cjxl to losslessly transcode a JPEG into a JXL container.
    The original JPEG bitstream is stored verbatim — no pixel decoding.
    djxl can reconstruct the exact original JPEG byte-for-byte.

    Returns None if cjxl rejects the file (CMYK, corrupt, unsupported),
    so the caller can fall back to pixel-lossless encoding via Pillow.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cjxl", str(src_path), str(out_path), "--lossless_jpeg=1"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Clean up any partial output file cjxl may have created
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None  # signal to caller: fall back to Pillow

    src_kb = src_path.stat().st_size / 1024
    dst_kb = out_path.stat().st_size / 1024
    saving = 100 * (1 - dst_kb / src_kb) if src_kb else 0
    return ("OK", f"{src_path.name} → {out_path.name}  "
                  f"({src_kb:.0f} KB → {dst_kb:.0f} KB, {saving:+.0f}%)  [JPEG transcode]")


def convert_one(args: tuple) -> tuple[str, str]:
    """
    Convert a single image to the target format, preserving EXIF.
    Must be a plain top-level function so ProcessPoolExecutor can pickle it.
    """
    src_path, out_path, fmt, quality, lossless = args

    # ── JXL lossless: try JPEG transcode first, fall back to pixel-lossless ──
    if fmt == "jxl" and lossless and src_path.suffix.lower() in JPEG_EXTS:
        result = _transcode_jpeg_to_jxl(src_path, out_path)
        if result is not None:
            return result
        # cjxl rejected the file (CMYK, corrupt, etc.) — fall through to
        # Pillow pixel-lossless encoding below, which handles these cases.
        lossless = True  # keep lossless flag for the Pillow path

    register_plugins(fmt)  # registers input plugins too

    try:
        img = Image.open(src_path)
        img.load()

        # ── Normalise orientation ─────────────────────────────────────────
        # Pillow does NOT auto-apply EXIF orientation for all formats
        # (notably JXL).  Use exif_transpose to physically rotate the
        # pixels so they are stored upright, and strip the Orientation
        # tag so viewers don't double-rotate.
        img = ImageOps.exif_transpose(img)

        # ── Extract and sanitise EXIF ─────────────────────────────────────
        raw_exif = img.info.get("exif", b"")
        if raw_exif:
            try:
                exif_dict = piexif.load(raw_exif)
                exif_dict["Exif"].pop(piexif.ExifIFD.MakerNote, None)
                # Coerce plain-int values to the types piexif.dump expects.
                # Pillow's JXL decoder can surface EXIF tags as plain int
                # (e.g. tag 41729 ExifVersion as int 1) which piexif.dump
                # rejects.  Numeric IFD types need a tuple; Undefined needs
                # bytes.
                _numeric_types = {
                    piexif.TYPES.Byte, piexif.TYPES.Short,
                    piexif.TYPES.Long, piexif.TYPES.SLong,
                    piexif.TYPES.Rational, piexif.TYPES.SRational,
                }
                for ifd_name in ("0th", "Exif", "GPS", "Interop"):
                    tag_table = piexif.TAGS.get(ifd_name, {})
                    d = exif_dict[ifd_name]
                    for k in list(d):
                        v = d[k]
                        if isinstance(v, int) and not isinstance(v, bool):
                            expected = tag_table.get(k, {}).get("type")
                            if expected in _numeric_types:
                                d[k] = (v,)
                            elif expected == piexif.TYPES.Undefined:
                                d[k] = v.to_bytes(
                                    max(1, (v.bit_length() + 7) // 8),
                                    "little",
                                )
                raw_exif = piexif.dump(exif_dict)
            except Exception:
                # piexif.dump can still fail for other reasons; fall back to
                # the original raw EXIF bytes so metadata isn't lost.
                raw_exif = img.info.get("exif", b"")

        # ── Mode normalisation ────────────────────────────────────────────
        if img.mode == "P":
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        elif img.mode not in ("RGB", "RGBA", "L", "LA"):
            img = img.convert("RGB")

        # HEIC doesn't support alpha — flatten onto white
        if fmt == "heic" and img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg

        # For JXL pixel-lossless, force decoded pixels so pillow-jxl-plugin
        # doesn't attempt its own JPEG reconstruction on the raw file bytes.
        if fmt == "jxl" and lossless:
            img = img.convert(img.mode if img.mode in ("RGB", "RGBA", "L", "LA") else "RGB")

        # ── Build save kwargs ─────────────────────────────────────────────
        pillow_fmt = FORMATS[fmt][0]
        save_kwargs: dict = {"format": pillow_fmt}

        if raw_exif:
            save_kwargs["exif"] = raw_exif

        # Preserve XMP metadata (present in some JXL/AVIF files)
        raw_xmp = img.info.get("xmp")
        if raw_xmp:
            save_kwargs["xmp"] = raw_xmp

        if fmt == "webp":
            save_kwargs["lossless"] = lossless
            if not lossless:
                save_kwargs["quality"] = quality

        elif fmt == "avif":
            save_kwargs["quality"] = -1 if lossless else quality

        elif fmt == "heic":
            save_kwargs["quality"] = quality

        elif fmt == "jxl":
            save_kwargs["lossless"] = lossless
            if lossless:
                # Disable pillow-jxl-plugin's own JPEG reconstruction —
                # we handle that via cjxl; letting the plugin try it causes
                # "bitstream reconstruction" / "Input is invalid" errors.
                save_kwargs["lossless_jpeg"] = False
            else:
                save_kwargs["quality"] = quality

        # ── Save ──────────────────────────────────────────────────────────
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, **save_kwargs)

        src_kb = src_path.stat().st_size / 1024
        dst_kb = out_path.stat().st_size / 1024
        saving = 100 * (1 - dst_kb / src_kb) if src_kb else 0
        is_jxl_lossless = fmt == "jxl" and lossless
        is_jpeg = src_path.suffix.lower() in JPEG_EXTS
        if is_jxl_lossless and is_jpeg:
            suffix = "  [pixel-lossless fallback]"  # cjxl rejected, used Pillow
        elif is_jxl_lossless:
            suffix = "  [pixel-lossless]"            # PNG, no JPEG bitstream
        else:
            suffix = ""
        return ("OK", f"{src_path.name} → {out_path.name}  "
                      f"({src_kb:.0f} KB → {dst_kb:.0f} KB, {saving:+.0f}%){suffix}")

    except Exception as e:
        return ("ERR", f"{src_path.name}: {e}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert images (JPG/PNG/WebP/AVIF/HEIC/JXL) to WebP, AVIF, HEIC, or JXL in parallel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "folder",
        help="Folder containing image files (searched recursively). Accepts JPG, PNG, WebP, AVIF, HEIC, JXL.",
    )
    parser.add_argument(
        "--format", "-f",
        choices=list(FORMATS.keys()),
        default="webp",
        help="Output format: webp (default), avif, heic, or jxl.",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output folder. If omitted, converted files are written next to sources.",
    )
    parser.add_argument(
        "--quality", "-q", type=int, default=85,
        help="Lossy quality 1–100 (default: 85). Ignored with --lossless.",
    )
    parser.add_argument(
        "--lossless",
        action="store_true",
        help="Lossless compression. For JXL + JPEG sources this uses cjxl "
             "bitstream transcoding (perfectly reversible, ~20%% smaller). "
             "For JXL + PNG and other formats, pixel-lossless encoding is used.",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=os.cpu_count(),
        help=f"Parallel worker processes (default: {os.cpu_count()} = all cores).",
    )
    parser.add_argument(
        "--include-same-fmt",
        action="store_true",
        default=False,
        help="Also convert files that are already in the output format "  
             "(e.g. convert .avif → .avif). Disabled by default — same-format "  
             "files are skipped unless this flag is set.",
    )
    parser.add_argument(
        "--delete-src", action="store_true",
        help="Delete original source files after successful conversion.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be converted without writing anything.",
    )
    args = parser.parse_args()

    fmt = args.format
    _, out_ext, supports_lossless = FORMATS[fmt]

    # ── Validate flag combinations ────────────────────────────────────────
    if args.lossless and not supports_lossless:
        print(f"Warning: {fmt.upper()} does not support lossless — ignoring --lossless.")
        args.lossless = False

    # ── Register plugins before checking availability ────────────────────
    register_plugins(fmt)

    # ── Check tool/codec availability ────────────────────────────────────
    if not args.dry_run:
        if fmt == "jxl" and args.lossless:
            hint = check_cjxl()
            if hint:
                print(f"Error: JXL lossless requires cjxl.\n{hint}")
                sys.exit(1)
        hint = check_format_support(fmt)
        if hint:
            print(f"Error: {fmt.upper()} output is not available.\n  Install hint: {hint}")
            sys.exit(1)

    input_dir  = Path(args.folder).resolve()
    output_dir = Path(args.output).resolve() if args.output else None
    in_place   = output_dir is None

    if not input_dir.is_dir():
        print(f"Error: '{input_dir}' is not a directory.")
        sys.exit(1)

    src_files_all = sorted(
        p for p in input_dir.rglob("*")
        if p.suffix.lower() in SRC_EXTS
    )

    # Skip same-format files unless --include-same-fmt is set
    skipped_same = [p for p in src_files_all if p.suffix.lower() == out_ext]
    src_files = [p for p in src_files_all
                 if args.include_same_fmt or p.suffix.lower() != out_ext]

    if not src_files_all:
        print("No supported image files found (JPG, PNG, WebP, AVIF, HEIC, JXL).")
        sys.exit(0)

    if not src_files:
        print(f"All {len(skipped_same)} file(s) are already {out_ext.upper()} — "
              f"nothing to convert. Use --include-same-fmt to convert them anyway.")
        sys.exit(0)

    by_ext: dict[str, int] = {}
    for p in src_files:
        by_ext[p.suffix.lower()] = by_ext.get(p.suffix.lower(), 0) + 1
    ext_str = "  ".join(f"{e}: {n}" for e, n in sorted(by_ext.items()))

    if args.lossless and fmt == "jxl":
        mode_str = "lossless (JPEG → cjxl transcode, PNG → pixel-lossless)"
    elif args.lossless:
        mode_str = "pixel-lossless"
    else:
        mode_str = f"lossy q={args.quality}"

    print(f"Found      : {len(src_files)} file(s) to convert  [{ext_str}]")
    if skipped_same:
        print(f"Skipped    : {len(skipped_same)} already-{out_ext} file(s) "
              f"(use --include-same-fmt to include them)")
    print(f"Format     : {fmt.upper()}  ({mode_str})")
    print(f"Workers    : {args.workers}")
    print(f"Output     : {'next to source' if in_place else output_dir}")
    print(f"Delete src : {args.delete_src}")
    print()

    if args.dry_run:
        for p in src_files:
            rel = p.relative_to(input_dir)
            out = (output_dir / rel if output_dir else p).with_suffix(out_ext)
            method = "JPEG transcode" if (fmt == "jxl" and args.lossless and p.suffix.lower() in JPEG_EXTS) else "pixel encode"
            print(f"  would convert ({method}): {rel} → {out}")
        for p in skipped_same:
            rel = p.relative_to(input_dir)
            print(f"  would skip (same fmt)  : {rel}")
        print("\n(dry run — nothing written)")
        return

    tasks = []
    for src in src_files:
        rel = src.relative_to(input_dir)
        out = ((output_dir / rel) if output_dir else src).with_suffix(out_ext)
        tasks.append((src, out, fmt, args.quality, args.lossless))

    ok = err = 0
    to_delete = []

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(convert_one, t): t[0] for t in tasks}
        for future in as_completed(futures):
            status, msg = future.result()
            src = futures[future]
            if status == "OK":
                ok += 1
                print(f"  [OK ]  {msg}")
                if args.delete_src:
                    to_delete.append(src)
            else:
                err += 1
                print(f"  [ERR]  {msg}")

    if to_delete:
        print(f"\nDeleting {len(to_delete)} original source file(s)...")
        for p in to_delete:
            try:
                p.unlink()
            except Exception as e:
                print(f"  could not delete {p.name}: {e}")

    print(f"\nDone.  Converted: {ok}  |  Errors: {err}")

    if fmt == "jxl" and args.lossless:
        print()
        print("To verify a file is perfectly reversible:")
        print("  djxl photo.jxl restored.jpg && diff photo.jpg restored.jpg")


if __name__ == "__main__":
    main()
