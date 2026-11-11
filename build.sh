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

# make_zip <zip> <include-compiled-schemas: 0|1>
# extensions.gnome.org compiles the schemas itself (GNOME 44+), so the upload zip leaves
# gschemas.compiled out; the local install zip keeps it because the shell needs it there.
make_zip() {
    rm -f "$1"
    python3 - "$1" "$2" <<'PY'
import sys, zipfile
from pathlib import Path
src = Path("src")
keep_compiled = sys.argv[2] == "1"
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.rglob("*")):
        if f.name == "gschemas.compiled" and not keep_compiled:
            continue
        if f.is_file() and "__pycache__" not in f.parts:
            info = zipfile.ZipInfo.from_file(f, f.relative_to(src).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
PY
}

ZIP="dist/$UUID.shell-extension.zip"
make_zip "$ZIP" 0
echo "$ZIP  (for extensions.gnome.org)"

if [[ "${1:-}" == "--install" ]]; then
    LOCAL_ZIP="dist/$UUID.local.shell-extension.zip"
    make_zip "$LOCAL_ZIP" 1
    gnome-extensions install --force "$LOCAL_ZIP"
    echo "Installed. On Wayland, log out and back in, then: gnome-extensions enable $UUID"
fi
