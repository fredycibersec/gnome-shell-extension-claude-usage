#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Builds dist/claude-usage@fredycibersec.shell-extension.zip (ready for extensions.gnome.org)
# and, with --install, installs it for the current user.
set -euo pipefail
cd "$(dirname "$0")"
UUID=$(python3 -c 'import json; print(json.load(open("src/metadata.json"))["uuid"])')

python3 tools/i18n.py check
python3 tools/i18n.py compile
glib-compile-schemas --strict src/schemas
mkdir -p dist
ZIP="dist/$UUID.shell-extension.zip"
rm -f "$ZIP"
python3 - "$ZIP" <<'PY'
import sys, zipfile
from pathlib import Path
src = Path("src")
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            info = zipfile.ZipInfo.from_file(f, f.relative_to(src).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
PY
echo "dist/$UUID.shell-extension.zip"

if [[ "${1:-}" == "--install" ]]; then
    gnome-extensions install --force "dist/$UUID.shell-extension.zip"
    echo "Installed. On Wayland, log out and back in, then: gnome-extensions enable $UUID"
fi
