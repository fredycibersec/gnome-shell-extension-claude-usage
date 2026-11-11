# Claude Usage — GNOME Shell extension

Unofficial top-bar indicator for your **Claude subscription limits** and the **API-equivalent cost** of
your **Claude Code** usage.

<img src="docs/screenshot.png" alt="Claude Usage menu" width="420">

- **Plan limits**: 5-hour session, weekly, per-model weekly (e.g. Fable) and extra usage, with a pace
  marker showing how far into each window you are.
- **Per-model breakdown inside each limit window**: cost, tokens and requests per model in the current
  session/week, plus an estimated share of the limit (`≈%`) when several models were used.
- **Spend at API prices** from Claude Code's local logs: today / 7 days / 30 days / this month, daily and
  hourly charts, tokens by type, by model and by project.
- The menu opens on hover and stays open on click. Preferences: show/hide the percentage in the top bar.
- English and Spanish.

## Requirements

- GNOME Shell 50
- `python3` (standard library only)
- [Claude Code](https://claude.com/claude-code) signed in with a Claude subscription

## How it works

`helper/claude-usage.py` is launched asynchronously every few minutes:

1. **Limits** — `GET https://api.anthropic.com/api/oauth/usage` using the OAuth token Claude Code stores in
   `~/.claude/.credentials.json`. The token is only *read*; it is never refreshed or sent anywhere else.
   When it expires, the extension shows the last saved data until you open `claude` again.
   This is the endpoint Claude Code itself uses; it is not a documented public API and may change.
2. **Spend** — Claude Code's JSONL logs in `~/.claude/projects/` are parsed incrementally and priced with
   the table in the helper (USD per million tokens). This is an *API-equivalent* figure, not what your
   subscription costs.

Nothing leaves your machine except the request to `api.anthropic.com`. Cached data lives in
`~/.cache/claude-usage-bar/`.

The helper also works on its own:

```sh
python3 src/helper/claude-usage.py --summary
```

## Install from source

```sh
./build.sh --install
# Wayland: log out and back in, then
gnome-extensions enable claude-usage@fredycibersec
```

## Translations

```sh
python3 tools/i18n.py extract   # refresh po/claude-usage.pot
# copy it to po/<lang>.po and translate
python3 tools/i18n.py check     # msgids missing from each .po
```

## License

GPL-2.0-or-later. Not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic, PBC.
