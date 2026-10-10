#!/usr/bin/env python3
"""Build the installable Blender extension zip.

Usage:  python tools/build_zip.py
Output: dist/kaggle_render-<version>.zip   (install it in Blender: Install from Disk)

بيعمل ملف الـ zip اللي بيتثبّت في Blender. الـ blender_manifest.toml لازم يبقى في أول الـ zip،
عشان كده بنضغط محتوى فولدر kaggle_render/ نفسه (مش الفولدر).
"""
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "kaggle_render"
manifest = (SRC / "blender_manifest.toml").read_text(encoding="utf-8")
version = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.M).group(1)

out_dir = ROOT / "dist"
out_dir.mkdir(exist_ok=True)
out = out_dir / f"kaggle_render-{version}.zip"

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(SRC.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            z.write(p, p.relative_to(SRC).as_posix())
print(f"Built {out} ({out.stat().st_size/1024:.0f} KB)")
sys.exit(0)
