#!/usr/bin/env python3
"""claude-usage — collects Claude (Claude Code) usage and spend for the GNOME extension.

SPDX-License-Identifier: GPL-2.0-or-later

Sources:
  1. Subscription limits: GET https://api.anthropic.com/api/oauth/usage with the OAuth token
     Claude Code stores in ~/.claude/.credentials.json. The token is only READ: it is NEVER
     refreshed here (refreshing would rotate the refresh token and could sign Claude Code out).
     If it has expired, a warning is returned along with the last saved data.
  2. Local usage: Claude Code's JSONL logs (~/.claude/projects/**/*.jsonl), read incrementally
     (only what was appended since the last pass), priced at API-equivalent rates per model,
     day and project.

Output: JSON on stdout (for the extension) or a readable summary with --summary.
Usage:  claude-usage [--json | --summary] [--max-age SECONDS] [--no-api] [--rescan]
"""

import argparse
import gettext
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "claude-usage-bar"
LOG_CACHE = CACHE_DIR / "logs-v1.json"
USAGE_CACHE = CACHE_DIR / "usage-v1.json"
CREDENTIALS = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")) / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USER_AGENT = "claude-usage-bar/1.0"
LOG_CACHE_VERSION = 1

# Translations share the extension's gettext domain and locale/ folder
_LOCALE_DIR = Path(__file__).resolve().parent.parent / "locale"
_ = gettext.translation("claude-usage@fredycibersec", localedir=_LOCALE_DIR, fallback=True).gettext

# Precios en USD por millón de tokens: input, cache write 5m, cache write 1h, cache read, output.
# Fuente: https://platform.claude.com/docs/en/about-claude/pricing (consultada el 2026-11-11).
PRICING = {
    "claude-fable-5-2":   (8.0, 10.0, 16.0, 0.20, 40.0),
    "claude-mythos-5-2":  (8.0, 10.0, 16.0, 0.20, 40.0),
    "claude-fable-5-1":   (10.0, 12.5, 20.0, 0.25, 50.0),
    "claude-mythos-5-1":  (10.0, 12.5, 20.0, 0.25, 50.0),
    "claude-fable-5":     (10.0, 12.5, 20.0, 1.00, 50.0),
    "claude-mythos-5":    (10.0, 12.5, 20.0, 1.00, 50.0),
    "claude-opus-5-5":    (4.0, 5.0, 8.0, 0.20, 20.0),
    "claude-opus-5":      (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-8":    (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-7":    (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-6":    (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-5":    (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-1":    (15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-opus-4":      (15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-sonnet-5-5":  (2.0, 2.5, 4.0, 0.10, 10.0),
    "claude-sonnet-5":    (2.0, 2.5, 4.0, 0.20, 10.0),
    "claude-sonnet-4-6":  (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4-5":  (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4":    (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-haiku-5-5":   (0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-4-5":   (1.0, 1.25, 2.0, 0.10, 5.0),
}
# Claude Haiku 5.5 cobra más si el prompt (input + caché) supera 100.000 tokens
HAIKU_55_LONG = (0.50, 0.625, 1.0, 0.05, 2.50)
FAST_MULTIPLIER = 2.0          # fast mode: el doble en todos los conceptos
US_GEO_MULTIPLIER = 1.1        # inference_geo = "us"
WEB_SEARCH_USD = 10.0 / 1000   # por búsqueda

PRETTY_MODEL = {
    "fable": "Fable", "mythos": "Mythos", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku",
}


def norm_model(model):
    """claude-sonnet-4-5-20250929 → claude-sonnet-4-5"""
    return re.sub(r"-\d{8}$", "", model or "")


def pretty_model(model):
    m = re.match(r"claude-([a-z]+)-(\d+(?:-\d+)?)$", model)
    if not m:
        return model
    return f"{PRETTY_MODEL.get(m.group(1), m.group(1).title())} {m.group(2).replace('-', '.')}"


def project_roots():
    roots = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        roots += [Path(p) / "projects" for p in env.split(",") if p]
    roots += [HOME / ".config" / "claude" / "projects", HOME / ".claude" / "projects"]
    seen, out = set(), []
    for r in roots:
        try:
            rp = r.resolve()
        except OSError:
            continue
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------------------------
# Registros locales
# --------------------------------------------------------------------------------------------

def parse_record(line):
    """Devuelve (clave, entrada) para una línea con uso de un mensaje del asistente, o None.

    entrada = [timestamp_epoch, modelo, input, cache_w5m, cache_w1h, cache_read, output,
               fast(0/1), geo_us(0/1), web_searches, proyecto]
    """
    if '"usage"' not in line or '"assistant"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message") or {}
    usage = msg.get("usage")
    model = norm_model(msg.get("model"))
    if not isinstance(usage, dict) or not model or model == "<synthetic>":
        return None
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    # Fragmentos incompletos de streaming: stop_reason null, sin salida ni caché
    if msg.get("stop_reason") is None and out == 0 and cw == 0 and cr == 0 and "stop_reason" in msg:
        return None
    if inp == out == cw == cr == 0:
        return None
    cc = usage.get("cache_creation") or {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    if w1h + w5m != cw:          # desglose ausente o incoherente → el resto como escritura de 5 min
        w1h = min(w1h, cw)
        w5m = cw - w1h
    stu = usage.get("server_tool_use") or {}
    ts = rec.get("timestamp")
    try:
        epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None
    mid = msg.get("id") or ""
    rid = rec.get("requestId") or rec.get("request_id") or ""
    key = f"{mid}:{rid}" if (mid or rid) else f"{rec.get('sessionId', '')}:{rec.get('uuid', '')}"
    cwd = rec.get("cwd") or ""
    project = os.path.basename(cwd.rstrip("/")) or "?"
    return key, [
        round(epoch, 3), model, inp, w5m, w1h, cr, out,
        1 if usage.get("speed") == "fast" else 0,
        1 if usage.get("inference_geo") == "us" else 0,
        int(stu.get("web_search_requests") or 0),
        project,
    ]


def scan_logs(rescan=False):
    cache = load_json(LOG_CACHE, {})
    if rescan or cache.get("version") != LOG_CACHE_VERSION:
        cache = {"version": LOG_CACHE_VERSION, "files": {}}
    files = cache["files"]
    present = set()
    errors = []

    for root in project_roots():
        for path in root.rglob("*.jsonl"):
            p = str(path)
            present.add(p)
            try:
                st = path.stat()
            except OSError:
                continue
            fc = files.get(p)
            if fc and fc.get("ino") == st.st_ino and fc.get("size") == st.st_size \
                    and fc.get("mtime") == st.st_mtime:
                continue
            # Fichero nuevo, reemplazado o truncado → desde el principio
            if not fc or fc.get("ino") != st.st_ino or st.st_size < fc.get("offset", 0):
                fc = {"ino": st.st_ino, "offset": 0, "entries": {}}
            try:
                with open(path, "rb") as f:
                    f.seek(fc["offset"])
                    data = f.read()
            except OSError as e:
                errors.append(f"{p}: {e}")
                continue
            end = data.rfind(b"\n")
            if end >= 0:
                for raw in data[:end].split(b"\n"):
                    r = parse_record(raw.decode("utf-8", "replace"))
                    if r:
                        fc["entries"][r[0]] = r[1]      # el último fragmento de un mensaje gana
                fc["offset"] += end + 1
            fc["size"], fc["mtime"] = st.st_size, st.st_mtime
            files[p] = fc

    for p in list(files):           # ficheros borrados: se olvidan
        if p not in present:
            del files[p]
    try:
        atomic_write_json(LOG_CACHE, cache)
    except OSError as e:
        errors.append(f"cache: {e}")

    merged = {}
    for fc in files.values():       # deduplicación global (sesiones reanudadas copian el historial)
        merged.update(fc["entries"])
    return list(merged.values()), errors


def entry_cost(e):
    _, model, inp, w5m, w1h, cr, out, fast, geo, web, _ = e
    price = PRICING.get(model)
    if price is None:
        return None
    if model == "claude-haiku-5-5" and inp + w5m + w1h + cr > 100_000:
        price = HAIKU_55_LONG
    pi, p5, p1, pr, po = price
    cost = (inp * pi + w5m * p5 + w1h * p1 + cr * pr + out * po) / 1_000_000
    if fast:
        cost *= FAST_MULTIPLIER
    if geo:
        cost *= US_GEO_MULTIPLIER
    return cost + web * WEB_SEARCH_USD


def aggregate(entries, days=30):
    now = datetime.now().astimezone()
    today = now.date()
    start_day = today - timedelta(days=days - 1)
    month_start = today.replace(day=1)
    hour_ago = time.time() - 3600
    five_h_ago = time.time() - 5 * 3600

    def blank():
        return {"cost": 0.0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "requests": 0}

    def add(b, e, c):
        b["cost"] += c or 0.0
        b["input"] += e[2]
        b["cache_write"] += e[3] + e[4]
        b["cache_read"] += e[5]
        b["output"] += e[6]
        b["requests"] += 1

    daily = {(start_day + timedelta(days=i)).isoformat(): blank() for i in range(days)}
    hourly = [blank() for _ in range(24)]          # hoy, por hora local
    periods = {k: blank() for k in ("today", "yesterday", "last_hour", "last_5h", "week", "month",
                                    "days30", "all")}
    by_model = defaultdict(blank)
    by_project = defaultdict(blank)
    unpriced = set()
    first_ts = None

    for e in entries:
        c = entry_cost(e)
        if c is None:
            unpriced.add(e[1])
        dt = datetime.fromtimestamp(e[0]).astimezone()
        d = dt.date()
        first_ts = e[0] if first_ts is None else min(first_ts, e[0])
        add(periods["all"], e, c)
        if d == today:
            add(periods["today"], e, c)
            add(hourly[dt.hour], e, c)
        elif d == today - timedelta(days=1):
            add(periods["yesterday"], e, c)
        if e[0] >= hour_ago:
            add(periods["last_hour"], e, c)
        if e[0] >= five_h_ago:
            add(periods["last_5h"], e, c)
        if d > today - timedelta(days=7):
            add(periods["week"], e, c)
        if d >= month_start:
            add(periods["month"], e, c)
        if d >= start_day:
            add(periods["days30"], e, c)
            add(daily[d.isoformat()], e, c)
            add(by_model[e[1]], e, c)
            add(by_project[e[10]], e, c)

    models = sorted(({"id": m, "name": pretty_model(m), **v} for m, v in by_model.items()),
                    key=lambda x: -x["cost"])
    projects = sorted(({"name": p, **v} for p, v in by_project.items()), key=lambda x: -x["cost"])
    return {
        "periods": periods,
        "daily": [{"date": k, **v} for k, v in daily.items()],
        "hourly_today": hourly,
        "models": models,
        "projects": projects[:8],
        "unpriced_models": sorted(unpriced),
        "first_seen": first_ts,
        "entries": len(entries),
    }


# --------------------------------------------------------------------------------------------
# Límites de la suscripción (API OAuth)
# --------------------------------------------------------------------------------------------

def read_credentials():
    data = load_json(CREDENTIALS, None)
    if not data or "claudeAiOauth" not in data:
        return None, _("No Claude Code credentials found (sign in with `claude`)")
    oauth = data["claudeAiOauth"]
    return oauth, None


def fetch_usage(max_age):
    cached = load_json(USAGE_CACHE, {})
    now = time.time()
    oauth, err = read_credentials()
    plan = {}
    if oauth:
        plan = {"subscription": oauth.get("subscriptionType"), "tier": oauth.get("rateLimitTier")}
    if cached.get("data") and now - cached.get("fetched_at", 0) < max_age:
        return {**cached, "plan": plan, "stale": False, "error": None, "from_cache": True}

    if oauth and (oauth.get("expiresAt") or 0) / 1000 < now + 30:
        err = _("The Claude Code token has expired: open `claude` to renew it")
        oauth = None
    if oauth:
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {oauth.get('accessToken', '')}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            result = {"data": data, "fetched_at": now}
            try:
                atomic_write_json(USAGE_CACHE, result)
            except OSError:
                pass
            return {**result, "plan": plan, "stale": False, "error": None, "from_cache": False}
        except urllib.error.HTTPError as e:
            err = {401: _("Token rejected (401): open `claude` to renew the session"),
                   403: _("Not allowed to query usage (403)"),
                   429: _("Too many requests (429): will retry later")}.get(
                e.code, _("HTTP error {code} querying usage").format(code=e.code))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            err = _("Cannot reach api.anthropic.com ({reason})").format(reason=getattr(e, "reason", e))
        except ValueError:
            err = _("Invalid response from the usage API")
    # Fallback: últimos datos buenos, marcados como antiguos
    if cached.get("data"):
        return {**cached, "plan": plan, "stale": True, "error": err, "from_cache": True}
    return {"data": None, "fetched_at": None, "plan": plan, "stale": True, "error": err,
            "from_cache": False}


def summarize_limits(usage):
    """Extrae de la respuesta los límites que interesan, con nombres estables."""
    d = usage.get("data") or {}
    limits = []

    def win(key, label, kind, model=None):
        w = d.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            limits.append({"id": key, "label": label, "kind": kind, "model": model,
                           "percent": float(w["utilization"]), "resets_at": w.get("resets_at")})

    weekly_model = _("Weekly {model}")
    win("five_hour", _("Session (5 h)"), "session")
    win("seven_day", _("Weekly"), "weekly")
    if not any(l["id"] == "seven_day" for l in limits):
        win("seven_day_oauth_apps", _("Weekly"), "weekly")
    win("seven_day_opus", weekly_model.format(model="Opus"), "weekly_model", "Opus")
    win("seven_day_sonnet", weekly_model.format(model="Sonnet"), "weekly_model", "Sonnet")
    for l in d.get("limits") or []:          # per-model weekly limits (e.g. Fable)
        if l.get("kind") == "weekly_scoped":
            name = (((l.get("scope") or {}).get("model") or {}).get("display_name")) or "model"
            if not any(x.get("model") == name for x in limits):
                limits.append({"id": f"weekly_{name.lower()}", "label": weekly_model.format(model=name),
                               "kind": "weekly_model", "model": name,
                               "percent": float(l.get("percent") or 0),
                               "resets_at": l.get("resets_at")})

    extra = None
    sp = d.get("spend")
    if isinstance(sp, dict) and sp.get("enabled"):
        def money(m):
            return (m or {}).get("amount_minor", 0) / (10 ** (m or {}).get("exponent", 2)) if m else None
        extra = {"used": money(sp.get("used")), "limit": money(sp.get("limit")),
                 "currency": (sp.get("used") or sp.get("limit") or {}).get("currency", "USD"),
                 "percent": sp.get("percent"), "limit_reached": bool(
                     (d.get("extra_usage") or {}).get("spend_limit_reached"))}
    elif isinstance(d.get("extra_usage"), dict) and d["extra_usage"].get("is_enabled"):
        eu = d["extra_usage"]
        div = 10 ** (eu.get("decimal_places") or 2)
        extra = {"used": (eu.get("used_credits") or 0) / div if eu.get("used_credits") else 0.0,
                 "limit": (eu.get("monthly_limit") or 0) / div, "currency": eu.get("currency", "USD"),
                 "percent": eu.get("utilization"), "limit_reached": bool(eu.get("spend_limit_reached"))}
    return limits, extra


# --------------------------------------------------------------------------------------------

WINDOW_LEN = {"session": 5 * 3600, "weekly": 7 * 86400, "weekly_model": 7 * 86400}


def attach_window_models(limits, entries):
    """Añade a cada límite el consumo local por modelo dentro de su ventana actual.

    La API solo da el % global (y, para algunos modelos como Fable, un % propio); el reparto
    por modelo se estima con el peso de cada modelo en el coste equivalente de la ventana.
    """
    now = time.time()
    for l in limits:
        length = WINDOW_LEN.get(l["kind"])
        if not length:
            continue
        start = now - length
        if l.get("resets_at"):
            try:
                start = datetime.fromisoformat(l["resets_at"]).timestamp() - length
            except ValueError:
                pass
        scope = (l.get("model") or "").lower() if l["kind"] == "weekly_model" else None
        by_model = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "requests": 0})
        for e in entries:
            if e[0] < start:
                continue
            name = pretty_model(e[1])
            if scope and not name.lower().startswith(scope):
                continue
            b = by_model[e[1]]
            b["cost"] += entry_cost(e) or 0.0
            b["tokens"] += e[2] + e[3] + e[4] + e[5] + e[6]
            b["requests"] += 1
        total = sum(b["cost"] for b in by_model.values())
        models = []
        for m, b in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
            share = b["cost"] / total if total > 0 else 0.0
            models.append({"id": m, "name": pretty_model(m), **b, "share": share,
                           "est_percent": l["percent"] * share})
        l["window_start"] = start
        l["models"] = models


def collect(args):
    out = {"generated_at": time.time(), "version": 1, "errors": []}
    if args.no_api:
        usage = {"data": None, "plan": {}, "stale": True, "error": _("API query disabled")}
    else:
        usage = fetch_usage(args.max_age)
    limits, extra = summarize_limits(usage)
    out["subscription"] = {
        "plan": usage.get("plan"), "limits": limits, "extra_usage": extra,
        "fetched_at": usage.get("fetched_at"), "stale": usage.get("stale"),
        "error": usage.get("error"),
    }
    try:
        entries, errs = scan_logs(args.rescan)
        out["local"] = aggregate(entries)
        attach_window_models(limits, entries)
        out["errors"] += errs[:5]
    except Exception as e:      # nunca romper la salida JSON: la extensión mostrará el error
        out["local"] = None
        out["errors"].append(_("Error reading the local logs: {error}").format(error=e))
    return out


def fmt_tokens(n):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return str(int(n))


def fmt_reset(iso):
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    secs = (t - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return _("now")
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    when = t.astimezone().strftime("%a %H:%M")
    return (f"{d}d {h}h" if d else f"{h}h {m:02d}m") + f" ({when})"


def print_summary(o):
    s = o["subscription"]
    plan = (s.get("plan") or {}).get("subscription") or "?"
    print(_("Claude — {plan} plan").format(plan=plan))
    if s.get("error"):
        print(f"  ⚠ {s['error']}" + (_("  (saved data)") if s.get("limits") else ""))
    for l in s["limits"]:
        bar = "█" * round(l["percent"] / 5) + "░" * (20 - round(l["percent"] / 5))
        print(f"  {l['label']:<16} {bar} {l['percent']:5.1f}%   " + _("resets in {when}").format(when=fmt_reset(l['resets_at'])))
        for m in l.get("models") or []:
            pct = "" if l["kind"] == "weekly_model" else f"  ≈{m['est_percent']:4.1f}%"
            print(f"      {m['name']:<12} ${m['cost']:>8.2f}  {fmt_tokens(m['tokens']):>7} tokens"
                  f"  {m['requests']:>5} " + _("req.") + pct)
        if l["kind"] == "weekly_model" and "models" in l and not l["models"]:
            print("      " + _("no usage in this window"))
    ex = s.get("extra_usage")
    if ex:
        print(f"  {_('Extra usage'):<16} {ex['used'] or 0:.2f} / {ex['limit'] or 0:.2f} {ex['currency']}")
    loc = o.get("local")
    if loc:
        p = loc["periods"]
        print("\n" + _("Local usage (API-equivalent cost, USD)"))
        for k, label in (("last_hour", _("Last hour")), ("today", _("Today")), ("yesterday", _("Yesterday")),
                         ("week", _("7 days")), ("month", _("This month")), ("days30", _("30 days"))):
            b = p[k]
            tok = b["input"] + b["cache_write"] + b["cache_read"] + b["output"]
            print(f"  {label:<12} ${b['cost']:>9.2f}   {fmt_tokens(tok):>7} tokens   {b['requests']:>6} " + _("requests"))
        if loc["models"]:
            print("\n  " + _("By model (30 days): ") + ", ".join(
                f"{m['name']} ${m['cost']:.2f}" for m in loc["models"]))
        if loc["unpriced_models"]:
            print("  " + _("Models with no known price: ") + ", ".join(loc["unpriced_models"]))
    for e in o["errors"]:
        print(f"  ⚠ {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=_("Claude usage and spend (for the GNOME top bar)"))
    ap.add_argument("--json", action="store_true", help=_("JSON output (default when not a terminal)"))
    ap.add_argument("--summary", action="store_true", help=_("human-readable summary"))
    ap.add_argument("--max-age", type=int, default=120,
                    help=_("seconds to reuse the last API query (default 120)"))
    ap.add_argument("--no-api", action="store_true", help=_("do not query the API, local logs only"))
    ap.add_argument("--rescan", action="store_true", help=_("re-read all logs"))
    args = ap.parse_args()
    o = collect(args)
    if args.summary or (not args.json and sys.stdout.isatty()):
        print_summary(o)
    else:
        json.dump(o, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
