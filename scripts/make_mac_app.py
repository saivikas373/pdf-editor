"""Build "PDF Editor.app" (macOS) next to this project.

Run:  .venv/bin/python scripts/make_mac_app.py
The app starts the editor in "app mode" (no Terminal window, quits by itself
a few minutes after you close the editor tab).
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile

import pymupdf as fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "PDF Editor.app")

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect x="92" y="92" width="840" height="840" rx="190" fill="#4f46e5"/>
  <path d="M332 236h262l158 158v392a48 48 0 0 1-48 48H332a48 48 0 0 1-48-48V284a48 48 0 0 1 48-48z" fill="#ffffff"/>
  <path d="M594 236v110a48 48 0 0 0 48 48h110z" fill="#c7d2fe"/>
  <rect x="352" y="470" width="236" height="26" rx="13" fill="#c7d2fe"/>
  <rect x="352" y="538" width="300" height="26" rx="13" fill="#c7d2fe"/>
  <rect x="352" y="606" width="170" height="26" rx="13" fill="#c7d2fe"/>
  <path d="M530 760l30-106 186-186 76 76-186 186z" fill="#f59e0b"/>
  <path d="M746 468l40-40a38 38 0 0 1 54 0l22 22a38 38 0 0 1 0 54l-40 40z" fill="#fbbf24"/>
  <path d="M530 760l30-106 76 76z" fill="#1f2937"/>
</svg>"""

LAUNCHER = """#!/bin/bash
# PDF Editor launcher: starts the local editor and opens it in your web browser.
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
PROJECT="$HERE"
[ -f "$PROJECT/run.py" ] || PROJECT="__PROJECT__"
PORT="${PDF_EDITOR_PORT:-8765}"
LOG="$HOME/Library/Logs/PDF Editor.log"
PY="$PROJECT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  /usr/bin/osascript -e 'display alert "PDF Editor needs a one-time setup" message "Open the pdf-editor folder and double-click start.command, then use this app." as warning'
  exit 1
fi

# already running? just open another editor tab
if /usr/bin/curl -s -m 2 "http://127.0.0.1:$PORT/api/config" | /usr/bin/grep -q '"fonts"'; then
  /usr/bin/open "http://127.0.0.1:$PORT/"
  exit 0
fi

exec "$PY" "$PROJECT/run.py" --app --port "$PORT" >>"$LOG" 2>&1
"""

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>PDF Editor</string>
  <key>CFBundleDisplayName</key><string>PDF Editor</string>
  <key>CFBundleIdentifier</key><string>local.pdf-editor</string>
  <key>CFBundleVersion</key><string>1.0.0</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>PDF Editor</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""


def build_icon(resources: str) -> None:
    if not shutil.which("iconutil"):
        print("iconutil not found; skipping icon")
        return
    svg = fitz.open("svg", ICON_SVG.encode())
    page = svg[0]
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "AppIcon.iconset")
        os.makedirs(iconset)
        for size in (16, 32, 128, 256, 512):
            for factor in (1, 2):
                px = size * factor
                pix = page.get_pixmap(matrix=fitz.Matrix(px / page.rect.width, px / page.rect.height), alpha=True)
                suffix = "" if factor == 1 else "@2x"
                pix.save(os.path.join(iconset, f"icon_{size}x{size}{suffix}.png"))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(resources, "AppIcon.icns")], check=True)


def main() -> int:
    if sys.platform != "darwin":
        print("This builds a macOS app; on other systems use start.command / start.bat.")
        return 1
    if os.path.exists(APP):
        shutil.rmtree(APP)
    macos = os.path.join(APP, "Contents", "MacOS")
    resources = os.path.join(APP, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(resources)
    with open(os.path.join(APP, "Contents", "Info.plist"), "w") as f:
        f.write(PLIST)
    exe = os.path.join(macos, "PDF Editor")
    with open(exe, "w") as f:
        f.write(LAUNCHER.replace("__PROJECT__", ROOT))
    os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    build_icon(resources)
    print("Built", APP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
