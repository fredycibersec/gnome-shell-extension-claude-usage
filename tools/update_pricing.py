#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Regenerates src/helper/pricing.json from Anthropic's pricing page (Markdown version).

  tools/update_pricing.py            write src/helper/pricing.json if prices changed
  tools/update_pricing.py --check    exit 1 if the file is out of date (no write)
  tools/update_pricing.py --file X   parse a local copy of the page instead of downloading it

Only the first-party Claude API table ("Model pricing") and the fast mode table are used.
The result is validated before writing, so a page layout change fails loudly instead of
publishing wrong prices.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing.md"
OUT = Path(__file__).resolve().parent.parent / "src" / "helper" / "pricing.json"
COLUMNS = ["input", "cache_write_5m", "cache_write_1h", "cache_read", "output"]
FAMILIES = ("fable", "mythos", "opus", "sonnet", "haiku")


def model_id(name):
    """'Claude Opus 5.5 ([retired…](…))' → 'claude-opus-5-5' (None if not a model name)."""
    name = re.sub(r"\(.*", "", name).strip()
    m = re.fullmatch(r"Claude ([A-Za-z]+) (\d+(?:\.\d+)?)", name)
    if not m or m.group(1).lower() not in FAMILIES:
        return None
    return f"claude-{m.group(1).lower()}-{m.group(2).replace('.', '-')}"


def money(cell):
    m = re.search(r"\$([\d.,]+)", cell)
    if not m:
        raise ValueError(f"no price in {cell!r}")
    return float(m.group(1).replace(",", ""))


def table_after(lines, heading):
    """Rows (lists of cells) of the first Markdown table after a heading."""
    start = next(i for i, l in enumerate(lines) if l.strip().lstrip("#").strip() == heading)
    rows, seen = [], False
    for l in lines[start + 1:]:
        if l.startswith("|"):
            seen = True
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if not set("".join(cells)) <= set("-: "):
                rows.append(cells)
        elif seen:
            break
        elif l.startswith("#"):
            break
    return rows[1:]          # drop the header row


def parse(md):
    lines = md.splitlines()
    models, long_context = {}, {}
    for cells in table_after(lines, "Model pricing"):
        name = cells[0]
        mid = model_id(name)
        if not mid:
            continue
        prices = dict(zip(COLUMNS, (money(c) for c in cells[1:6])))
        over = re.search(r"prompts over ([\d,]+) tokens", name)
        if over:
            long_context[mid] = {"threshold": int(over.group(1).replace(",", "")), **prices}
        elif mid not in models:
            models[mid] = prices

    fast = {}
    for cells in table_after(lines, "Fast mode pricing"):
        for part in cells[0].split("/"):
            mid = model_id(part.strip())
            if mid:
                fast[mid] = {"input": money(cells[1]), "output": money(cells[2])}

    geo = re.search(r"inference_geo: \"us\"`[^.]*?([\d.]+)x pricing multiplier", md)
    return {
        "source": SOURCE,
        "unit": "USD per million tokens",
        "models": dict(sorted(models.items())),
        "long_context": long_context,
        "fast": dict(sorted(fast.items())),
        "us_geo_multiplier": float(geo.group(1)) if geo else 1.1,
        "web_search_per_request": 0.01,
    }


def validate(p):
    errors = []
    if len(p["models"]) < 10:
        errors.append(f"only {len(p['models'])} models parsed")
    for mid, pr in {**p["models"], **p["long_context"]}.items():
        if set(pr) - {"threshold"} != set(COLUMNS):
            errors.append(f"{mid}: missing columns")
            continue
        if not all(0 < pr[c] < 1000 for c in COLUMNS):
            errors.append(f"{mid}: price out of range {pr}")
        if not pr["cache_read"] < pr["input"] < pr["cache_write_5m"] <= pr["cache_write_1h"]:
            errors.append(f"{mid}: inconsistent cache/input ordering {pr}")
        if pr["output"] < pr["input"]:
            errors.append(f"{mid}: output cheaper than input {pr}")
    for mid, f in p["fast"].items():
        base = p["models"].get(mid)
        if not base:
            errors.append(f"fast mode for unknown model {mid}")
        elif f["input"] <= base["input"]:
            errors.append(f"{mid}: fast mode not more expensive than standard")
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--file")
    args = ap.parse_args()
    if args.file:
        md = Path(args.file).read_text()
    else:
        req = urllib.request.Request(SOURCE, headers={"User-Agent": "claude-usage-pricing-updater"})
        with urllib.request.urlopen(req, timeout=30) as r:
            md = r.read().decode()

    new = parse(md)
    errors = validate(new)
    if errors:
        print("Validation failed, pricing.json left untouched:\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(2)

    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    comparable = lambda d: {k: v for k, v in d.items() if k != "updated"}
    if comparable(old) == comparable(new):
        print(f"pricing.json is up to date ({len(new['models'])} models)")
        return
    if args.check:
        print("pricing.json is out of date", file=sys.stderr)
        sys.exit(1)

    for mid in sorted(set(new["models"]) | set(old.get("models", {}))):
        a, b = old.get("models", {}).get(mid), new["models"].get(mid)
        if a != b:
            print(f"  {mid}: {a} → {b}")
    new = {"updated": date.today().isoformat(), **new}
    OUT.write_text(json.dumps(new, indent=2) + "\n")
    print(f"pricing.json updated ({len(new['models'])} models)")


if __name__ == "__main__":
    main()
