#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Minimal gettext tooling (no xgettext/msgfmt needed).

  tools/i18n.py extract   → po/claude-usage.pot from the _('…') / _("…") calls in src/
  tools/i18n.py compile   → src/locale/<lang>/LC_MESSAGES/<domain>.mo from po/<lang>.po
  tools/i18n.py check     → list msgids missing from each .po
"""
import ast
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, PO = ROOT / "src", ROOT / "po"
DOMAIN = json.loads((SRC / "metadata.json").read_text())["gettext-domain"]
CALL = re.compile(r"""\b_\(\s*('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")\s*\)""")


def extract():
    ids = {}
    for f in sorted(list(SRC.rglob("*.js")) + list(SRC.rglob("*.py"))):
        text = f.read_text()
        for m in CALL.finditer(text):
            msgid = ast.literal_eval(m.group(1))
            line = text.count("\n", 0, m.start()) + 1
            ids.setdefault(msgid, []).append(f"{f.relative_to(ROOT)}:{line}")
    return ids


def po_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def parse_po(path):
    entries, msgid, msgstr, cur = {}, None, None, None
    def flush():
        if msgid is not None and msgstr:
            entries[msgid] = msgstr
    for raw in path.read_text().splitlines() + [""]:
        line = raw.strip()
        if line.startswith("msgid "):
            flush()
            msgid, msgstr, cur = ast.literal_eval(line[6:]), None, "id"
        elif line.startswith("msgstr "):
            msgstr, cur = ast.literal_eval(line[7:]), "str"
        elif line.startswith('"') and cur:
            if cur == "id":
                msgid += ast.literal_eval(line)
            else:
                msgstr += ast.literal_eval(line)
        elif not line:
            flush()
            msgid = msgstr = cur = None
    return entries


def write_mo(entries, path):
    keys = sorted(entries)
    ids = b"".join(k.encode() + b"\0" for k in keys)
    strs = b"".join(entries[k].encode() + b"\0" for k in keys)
    n = len(keys)
    o_ids, o_strs = 28, 28 + n * 8
    base = 28 + n * 16
    id_tab, s_tab, off = [], [], 0
    for k in keys:
        b = k.encode()
        id_tab += [len(b), base + off]
        off += len(b) + 1
    base2, off = base + len(ids), 0
    for k in keys:
        b = entries[k].encode()
        s_tab += [len(b), base2 + off]
        off += len(b) + 1
    data = struct.pack("Iiiiiii", 0x950412DE, 0, n, o_ids, o_strs, 0, 0)
    data += struct.pack(f"{2 * n}i", *id_tab) + struct.pack(f"{2 * n}i", *s_tab) + ids + strs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compile"
    if cmd == "extract":
        out = ['msgid ""', 'msgstr ""', '"Content-Type: text/plain; charset=UTF-8\\n"', ""]
        for msgid, refs in extract().items():
            out += ["#: " + " ".join(refs), "msgid " + po_quote(msgid), 'msgstr ""', ""]
        (PO / "claude-usage.pot").write_text("\n".join(out))
        print(f"po/claude-usage.pot: {len(extract())} strings")
    elif cmd == "compile":
        for po in sorted(PO.glob("*.po")):
            entries = parse_po(po)
            entries[""] = "Content-Type: text/plain; charset=UTF-8\n"
            write_mo(entries, SRC / "locale" / po.stem / "LC_MESSAGES" / f"{DOMAIN}.mo")
            print(f"{po.name}: {len(entries) - 1} translations")
    elif cmd == "check":
        ids = extract()
        bad = False
        for po in sorted(PO.glob("*.po")):
            missing = [m for m in ids if m not in parse_po(po)]
            bad |= bool(missing)
            print(f"{po.name}: {len(missing)} missing" + "".join(f"\n  {m}" for m in missing))
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
