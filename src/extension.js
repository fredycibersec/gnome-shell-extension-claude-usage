// SPDX-License-Identifier: GPL-2.0-or-later
// Claude Usage — GNOME top bar indicator for Claude subscription limits and spend.
//
// Data is collected by helper/claude-usage.py (Claude OAuth usage API + Claude Code logs); this
// file only launches it asynchronously and draws the result. The menu opens on hover (and closes
// when the pointer leaves) or on click (and then stays open).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const REFRESH_SECS = 180;          // refresco periódico
const API_MAX_AGE = 150;           // el helper reutiliza la consulta a la API si es más reciente
const OPEN_REFRESH_AGE = 60;       // al abrir el menú, refrescar si los datos tienen más de esto
const HOVER_OPEN_MS = 250;
const HOVER_CLOSE_MS = 500;
const HELPER_TIMEOUT_SECS = 60;
const USAGE_URL = 'https://claude.ai/settings/usage';

const ACCENT = [0.851, 0.467, 0.341];          // #D97757
const OK = [0.341, 0.890, 0.537];              // #57e389
const WARN = [0.965, 0.827, 0.176];            // #f6d32d
const CRIT = [1.0, 0.482, 0.388];              // #ff7b63
const TOKEN_COLORS = {                          // tokens por tipo
    input: [0.384, 0.627, 0.918],              // #62a0ea
    cache_write: [0.753, 0.380, 0.796],        // #c061cb
    cache_read: [0.420, 0.420, 0.470],
    output: ACCENT,
};
const TOKEN_LABELS = () => ({input: _('Input'), cache_write: _('Cache write'), cache_read: _('Cache read'), output: _('Output')});
const WINDOW_SECS = {session: 5 * 3600, weekly: 7 * 86400, weekly_model: 7 * 86400};

const MODEL_COLORS = {fable: '#e5a50a', mythos: '#dc8add', opus: '#D97757', sonnet: '#62a0ea', haiku: '#57e389'};

function modelColor(name) {
    return MODEL_COLORS[(name ?? '').split(' ')[0].toLowerCase()] ?? '#9a9996';
}

// ---------------------------------------------------------------------------------------------
// Formato
// ---------------------------------------------------------------------------------------------

function fmtUsd(v) {
    if (v === null || v === undefined)
        return '—';
    if (v >= 1000)
        return `$${(v / 1000).toFixed(1)}k`;
    if (v >= 100)
        return `$${v.toFixed(0)}`;
    return `$${v.toFixed(2)}`;
}

function fmtMoney(v, currency) {
    try {
        return new Intl.NumberFormat(undefined, {style: 'currency', currency}).format(v ?? 0);
    } catch {
        return `${(v ?? 0).toFixed(2)} ${currency}`;
    }
}

function fmtTokens(n) {
    for (const [div, suf] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'k']]) {
        if (n >= div)
            return `${(n / div).toFixed(n / div >= 100 ? 0 : 1)}${suf}`;
    }
    return `${Math.round(n)}`;
}

function totalTokens(b) {
    return b.input + b.cache_write + b.cache_read + b.output;
}

function parseIso(iso) {
    if (!iso)
        return null;
    try {
        return GLib.DateTime.new_from_iso8601(iso, null);
    } catch {
        return null;
    }
}

function fmtCountdown(iso) {
    const dt = parseIso(iso);
    if (!dt)
        return '';
    const secs = dt.to_unix() - GLib.get_real_time() / 1e6;
    if (secs <= 0)
        return _('resetting…');
    const d = Math.floor(secs / 86400);
    const h = Math.floor((secs % 86400) / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const left = d ? `${d} d ${h} h` : h ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min`;
    // TRANSLATORS: %s is a duration like "3 h 05 min", then the local reset time
    return `${_('resets in %s').format(left)} · ${dt.to_local().format(d ? '%a %H:%M' : '%H:%M')}`;
}

function elapsedFraction(iso, kind) {
    const dt = parseIso(iso);
    const win = WINDOW_SECS[kind];
    if (!dt || !win)
        return null;
    const left = dt.to_unix() - GLib.get_real_time() / 1e6;
    return Math.min(1, Math.max(0, 1 - left / win));
}

function fmtAgo(epoch) {
    if (!epoch)
        return _('never');
    const s = Math.max(0, GLib.get_real_time() / 1e6 - epoch);
    if (s < 60)
        return _('just now');
    if (s < 3600)
        return _('%d min ago').format(Math.floor(s / 60));
    return _('%d h ago').format(Math.floor(s / 3600));
}

function fmtDay(iso, fmt = '%a %-d %b') {
    const [y, mo, d] = iso.split('-').map(Number);
    return GLib.DateTime.new_local(y, mo, d, 0, 0, 0).format(fmt);
}

function severityColor(pct) {
    return pct >= 90 ? CRIT : pct >= 70 ? WARN : OK;
}

function severityClass(pct) {
    return pct >= 90 ? 'cu-crit' : pct >= 70 ? 'cu-warn' : '';
}

function rgba(cr, c, a = 1) {
    cr.setSourceRGBA(c[0], c[1], c[2], a);
}

function roundedRect(cr, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    if (w <= 0 || h <= 0)
        return;
    cr.newSubPath();
    cr.arc(x + w - r, y + r, r, -Math.PI / 2, 0);
    cr.arc(x + w - r, y + h - r, r, 0, Math.PI / 2);
    cr.arc(x + r, y + h - r, r, Math.PI / 2, Math.PI);
    cr.arc(x + r, y + r, r, Math.PI, 1.5 * Math.PI);
    cr.closePath();
}

function label(text, styleClass = '', props = {}) {
    return new St.Label({text, style_class: styleClass, y_align: Clutter.ActorAlign.CENTER, ...props});
}

function vbox(props = {}) {
    return new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, ...props});
}

function hbox(props = {}) {
    return new St.BoxLayout({orientation: Clutter.Orientation.HORIZONTAL, ...props});
}

// ---------------------------------------------------------------------------------------------
// Widgets dibujados con Cairo
// ---------------------------------------------------------------------------------------------

// Medidor del panel: dos barras finas (sesión arriba, semanal abajo)
const PanelMeter = GObject.registerClass(
class PanelMeter extends St.DrawingArea {
    _init() {
        super._init({width: 18, height: 16, y_align: Clutter.ActorAlign.CENTER});
        this._values = [null, null];
        this._stale = false;
    }

    update(session, weekly, stale) {
        this._values = [session, weekly];
        this._stale = stale;
        this.queue_repaint();
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const [w, h] = this.get_surface_size();
        const fg = this.get_theme_node().get_foreground_color();
        const fgc = [fg.red / 255, fg.green / 255, fg.blue / 255];
        const barH = Math.max(3, Math.round(h * 0.26));
        const ys = [Math.round(h * 0.18), Math.round(h * 0.58)];
        this._values.forEach((v, i) => {
            rgba(cr, fgc, 0.28);
            roundedRect(cr, 0, ys[i], w, barH, barH / 2);
            cr.fill();
            if (v !== null) {
                const c = this._stale ? fgc : i === 0 ? severityColor(v) : fgc;
                rgba(cr, c, this._stale ? 0.5 : 0.95);
                roundedRect(cr, 0, ys[i], Math.max(barH, w * Math.min(v, 100) / 100), barH, barH / 2);
                cr.fill();
            }
        });
        cr.$dispose();
    }
});

// Barra de progreso de un límite, con marca del ritmo uniforme (tiempo transcurrido de la ventana)
const LimitBar = GObject.registerClass(
class LimitBar extends St.DrawingArea {
    _init(pct, pace, color = null) {
        super._init({height: 8, x_expand: true});
        this._pct = pct;
        this._pace = pace;
        this._color = color;
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const [w, h] = this.get_surface_size();
        rgba(cr, [1, 1, 1], 0.1);
        roundedRect(cr, 0, 0, w, h, h / 2);
        cr.fill();
        const c = this._color ?? severityColor(this._pct);
        rgba(cr, c, 0.95);
        if (this._pct > 0) {
            roundedRect(cr, 0, 0, Math.max(h, w * Math.min(this._pct, 100) / 100), h, h / 2);
            cr.fill();
        }
        if (this._pace !== null && this._pace > 0.01 && this._pace < 0.99) {
            const x = Math.round(w * this._pace) + 0.5;
            rgba(cr, [1, 1, 1], 0.85);
            cr.setLineWidth(2);
            cr.moveTo(x, -1);
            cr.lineTo(x, h + 1);
            cr.stroke();
        }
        cr.$dispose();
    }
});

// Gráfico de barras (coste por día u hora) con resaltado al pasar el ratón
const BarChart = GObject.registerClass(
class BarChart extends St.DrawingArea {
    _init(values, {height = 90, highlightLast = true, onHover = null} = {}) {
        super._init({height, x_expand: true, reactive: true, track_hover: true});
        this._values = values;
        this._highlightLast = highlightLast;
        this._hover = -1;
        this._onHover = onHover;
        this.connect('motion-event', (_a, ev) => {
            const [x] = ev.get_coords();
            const [ax] = this.get_transformed_position();
            const w = this.get_width();
            const i = Math.min(this._values.length - 1, Math.max(0, Math.floor((x - ax) / w * this._values.length)));
            if (i !== this._hover) {
                this._hover = i;
                this.queue_repaint();
                this._onHover?.(i);
            }
            return Clutter.EVENT_PROPAGATE;
        });
        this.connect('leave-event', () => {
            this._hover = -1;
            this.queue_repaint();
            this._onHover?.(-1);
            return Clutter.EVENT_PROPAGATE;
        });
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const [w, h] = this.get_surface_size();
        const n = this._values.length;
        const max = Math.max(...this._values, 0);
        const top = 4;
        const plotH = h - top - 1;
        // líneas guía (máximo y mitad)
        rgba(cr, [1, 1, 1], 0.08);
        cr.setLineWidth(1);
        for (const f of [0, 0.5]) {
            const y = Math.round(top + plotH * f) + 0.5;
            cr.moveTo(0, y);
            cr.lineTo(w, y);
        }
        cr.stroke();
        const slot = w / n;
        const gap = Math.max(1, Math.min(3, slot * 0.22));
        this._values.forEach((v, i) => {
            const bh = max > 0 ? Math.max(v > 0 ? 2 : 0, plotH * v / max) : 0;
            const isLast = this._highlightLast && i === n - 1;
            const hovered = i === this._hover;
            rgba(cr, ACCENT, hovered ? 1 : isLast ? 0.95 : 0.6);
            roundedRect(cr, i * slot + gap / 2, top + plotH - bh, slot - gap, bh, 2);
            cr.fill();
        });
        // eje
        rgba(cr, [1, 1, 1], 0.25);
        cr.moveTo(0, h - 0.5);
        cr.lineTo(w, h - 0.5);
        cr.stroke();
        cr.$dispose();
    }
});

// Barra apilada horizontal (tokens por tipo)
const StackBar = GObject.registerClass(
class StackBar extends St.DrawingArea {
    _init(parts) {
        super._init({height: 10, x_expand: true});
        this._parts = parts;     // [[valor, color], ...]
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const [w, h] = this.get_surface_size();
        const total = this._parts.reduce((a, [v]) => a + v, 0);
        roundedRect(cr, 0, 0, w, h, h / 2);
        cr.clip();
        rgba(cr, [1, 1, 1], 0.1);
        cr.paint();
        let x = 0;
        for (const [v, c] of this._parts) {
            if (total <= 0)
                break;
            const pw = w * v / total;
            rgba(cr, c, 0.95);
            cr.rectangle(x, 0, pw + 0.5, h);
            cr.fill();
            x += pw;
        }
        cr.$dispose();
    }
});

// ---------------------------------------------------------------------------------------------
// Indicador
// ---------------------------------------------------------------------------------------------

const ClaudeUsageIndicator = GObject.registerClass(
class ClaudeUsageIndicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.5, 'Claude Usage', false);
        this._ext = ext;
        this._settings = ext.getSettings();
        this._data = null;
        this._lastOk = 0;
        this._running = false;
        this._cancellable = new Gio.Cancellable();
        this._hoverOpenId = 0;
        this._hoverPollId = 0;
        this._openedByHover = false;
        this._outsideSince = 0;

        const box = hbox({style_class: 'panel-status-indicators-box'});
        this._meter = new PanelMeter();
        this._label = label('…', 'cu-panel-label');
        box.add_child(this._meter);
        box.add_child(this._label);
        this.add_child(box);
        this._settings.bind('show-percent', this._label, 'visible', Gio.SettingsBindFlags.GET);

        this.menu.box.add_style_class_name('cu-menu');
        this._item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false,
            style_class: 'cu-popup-item'});
        this._content = vbox({style_class: 'cu-popup', x_expand: true});
        this._scroll = new St.ScrollView({
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: true,
            x_expand: true, y_expand: true,
            child: this._content,
        });
        this._item.add_child(this._scroll);
        this.menu.addMenuItem(this._item);
        this._renderMenu();

        this.menu.connect('open-state-changed', (_m, open) => {
            if (open) {
                this._renderMenu();
                if (GLib.get_real_time() / 1e6 - this._lastOk > OPEN_REFRESH_AGE)
                    this.refresh(OPEN_REFRESH_AGE);
            } else {
                this._stopHoverPoll();
                this._openedByHover = false;
            }
        });

        // Apertura al pasar el ratón
        this.connect('enter-event', () => {
            if (this.menu.isOpen || this._hoverOpenId)
                return Clutter.EVENT_PROPAGATE;
            this._hoverOpenId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, HOVER_OPEN_MS, () => {
                this._hoverOpenId = 0;
                if (!this.menu.isOpen && this.hover) {
                    this._openedByHover = true;
                    this.menu.open();
                    this._startHoverPoll();
                }
                return GLib.SOURCE_REMOVE;
            });
            return Clutter.EVENT_PROPAGATE;
        });
        this.connect('leave-event', () => {
            this._clearHoverOpen();
            return Clutter.EVENT_PROPAGATE;
        });

        this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, REFRESH_SECS, () => {
            this.refresh(API_MAX_AGE);
            return GLib.SOURCE_CONTINUE;
        });
        this.refresh(API_MAX_AGE);
    }

    // Un clic con el menú abierto por hover lo "fija" en vez de cerrarlo
    vfunc_event(event) {
        const t = event.type();
        if (this._openedByHover && this.menu.isOpen &&
            (t === Clutter.EventType.BUTTON_PRESS || t === Clutter.EventType.TOUCH_BEGIN)) {
            this._openedByHover = false;
            this._stopHoverPoll();
            return Clutter.EVENT_STOP;
        }
        return super.vfunc_event(event);
    }

    _clearHoverOpen() {
        if (this._hoverOpenId) {
            GLib.source_remove(this._hoverOpenId);
            this._hoverOpenId = 0;
        }
    }

    _pointerInside(actor) {
        if (!actor || !actor.mapped)
            return false;
        const [px, py] = global.get_pointer();
        const [x, y] = actor.get_transformed_position();
        const [w, h] = actor.get_transformed_size();
        return px >= x && px < x + w && py >= y && py < y + h;
    }

    // Mientras el menú está abierto hay un grab, así que los enter/leave no llegan: se sondea
    _startHoverPoll() {
        this._stopHoverPoll();
        this._outsideSince = 0;
        this._hoverPollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 120, () => {
            if (!this.menu.isOpen || !this._openedByHover) {
                this._hoverPollId = 0;
                return GLib.SOURCE_REMOVE;
            }
            const inside = this._pointerInside(this) || this._pointerInside(this.menu.box);
            const now = GLib.get_monotonic_time() / 1000;
            if (inside) {
                this._outsideSince = 0;
            } else if (!this._outsideSince) {
                this._outsideSince = now;
            } else if (now - this._outsideSince > HOVER_CLOSE_MS) {
                this._hoverPollId = 0;
                this.menu.close();
                return GLib.SOURCE_REMOVE;
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopHoverPoll() {
        if (this._hoverPollId) {
            GLib.source_remove(this._hoverPollId);
            this._hoverPollId = 0;
        }
    }

    // ---- Datos ----------------------------------------------------------------------------

    refresh(maxAge) {
        if (this._running)
            return;
        this._running = true;
        const helper = GLib.build_filenamev([this._ext.path, 'helper', 'claude-usage.py']);
        let proc;
        try {
            const argv = ['python3', '-I', helper, '--json', '--max-age', String(maxAge)];
            if (!this._settings.get_boolean('update-prices'))
                argv.push('--no-remote-pricing');
            proc = Gio.Subprocess.new(argv,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            this._running = false;
            this._setError(_('Could not start the collector: %s').format(e.message));
            return;
        }
        // Si el recolector se cuelga (p. ej. red muy lenta), se mata
        const killId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, HELPER_TIMEOUT_SECS, () => {
            proc.force_exit();
            return GLib.SOURCE_REMOVE;
        });
        proc.communicate_utf8_async(null, this._cancellable, (p, res) => {
            GLib.source_remove(killId);
            this._running = false;
            let stdout, stderr;
            try {
                [, stdout, stderr] = p.communicate_utf8_finish(res);
            } catch (e) {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    this._setError(_('Collector error: %s').format(e.message));
                return;
            }
            if (!p.get_successful() || !stdout?.trim()) {
                const msg = (stderr || '').trim().split('\n').pop() || _('exit code %d').format(p.get_exit_status());
                this._setError(_('The collector failed: %s').format(msg));
                return;
            }
            try {
                this._data = JSON.parse(stdout);
                this._data._helperError = null;
                this._lastOk = this._data.generated_at;
            } catch (e) {
                this._setError(_('Invalid collector output: %s').format(e.message));
                return;
            }
            this._update();
        });
    }

    _setError(msg) {
        console.warn(`[claude-usage] ${msg}`);
        if (!this._data)
            this._data = {subscription: {limits: []}, local: null, errors: []};
        this._data._helperError = msg;
        this._update();
    }

    _limit(kind) {
        return this._data?.subscription?.limits?.find(l => l.kind === kind) ?? null;
    }

    _update() {
        const sub = this._data?.subscription ?? {};
        const session = this._limit('session');
        const weekly = this._limit('weekly');
        const stale = !!sub.stale || !!this._data?._helperError;
        this._meter.update(session?.percent ?? null, weekly?.percent ?? null, stale);

        let text;
        if (session)
            text = `${Math.round(session.percent)}%`;
        else if (this._data?.local)
            text = fmtUsd(this._data.local.periods.today.cost);
        else
            text = '—';
        this._label.text = text;
        this._label.style_class = 'cu-panel-label';
        if (stale)
            this._label.add_style_class_name('cu-stale');
        else if (session)
            this._label.add_style_class_name(severityClass(Math.max(session.percent, weekly?.percent ?? 0)) || 'cu-ok');

        if (this.menu.isOpen)
            this._renderMenu();
    }

    // ---- Menú -----------------------------------------------------------------------------

    _renderMenu() {
        this._content.destroy_all_children();
        const d = this._data;
        const sub = d?.subscription ?? {};
        const loc = d?.local;

        // Cabecera
        const header = hbox({style_class: 'cu-header'});
        header.add_child(new St.Icon({
            gicon: Gio.FileIcon.new(this._ext.dir.get_child('icons').get_child('claude-symbolic.svg')),
            icon_size: 18, style_class: 'cu-logo', y_align: Clutter.ActorAlign.CENTER,
        }));
        header.add_child(label('Claude', 'cu-title'));
        const plan = sub.plan?.subscription;
        if (plan)
            header.add_child(label(plan.charAt(0).toUpperCase() + plan.slice(1), 'cu-badge'));
        header.add_child(new St.Widget({x_expand: true}));
        header.add_child(label(d ? _('Updated %s').format(fmtAgo(d.generated_at)) : _('Loading…'), 'cu-muted'));
        const refreshBtn = new St.Button({style_class: 'cu-button', can_focus: true,
            child: new St.Icon({icon_name: 'view-refresh-symbolic', icon_size: 14})});
        refreshBtn.connect('clicked', () => this.refresh(0));
        header.add_child(refreshBtn);
        const prefsBtn = new St.Button({style_class: 'cu-button', can_focus: true,
            child: new St.Icon({icon_name: 'emblem-system-symbolic', icon_size: 14})});
        prefsBtn.connect('clicked', () => {
            this.menu.close();
            this._ext.openPreferences();
        });
        header.add_child(prefsBtn);
        this._content.add_child(header);

        // Avisos
        const warnings = [];
        if (d?._helperError)
            warnings.push(d._helperError);
        if (sub.error)
            warnings.push(sub.limits?.length ? _('%s — showing the last data (%s)').format(sub.error, fmtAgo(sub.fetched_at)) : sub.error);
        for (const e of d?.errors ?? [])
            warnings.push(e);
        for (const w of warnings) {
            const l = label(`⚠ ${w}`, 'cu-error');
            l.clutter_text.line_wrap = true;
            this._content.add_child(l);
        }

        if (!d)
            return;

        // Límites de la suscripción
        if (sub.limits?.length) {
            this._content.add_child(label(_('PLAN LIMITS'), 'cu-section'));
            for (const l of sub.limits)
                this._content.add_child(this._limitRow(l));
        }
        const ex = sub.extra_usage;
        if (ex) {
            const pct = ex.limit ? 100 * (ex.used ?? 0) / ex.limit : 0;
            this._content.add_child(this._limitRow({
                label: _('Extra usage (month)'), percent: pct, kind: 'extra',
                right: `${fmtMoney(ex.used, ex.currency)} / ${fmtMoney(ex.limit, ex.currency)}`,
                sub: ex.limit_reached ? _('spend limit reached') : _('real spend beyond the plan'),
                color: ACCENT,
            }));
        }

        if (!loc)
            return;

        // Gasto (equivalente a precios de la API)
        this._content.add_child(label(_('USAGE · API-EQUIVALENT COST (USD)'), 'cu-section'));
        const p = loc.periods;
        const tiles = hbox({style_class: 'cu-tiles', x_expand: true});
        for (const [key, name] of [['today', _('Today')], ['week', _('7 days')], ['days30', _('30 days')], ['month', _('This month')]]) {
            const t = vbox({style_class: 'cu-tile', x_expand: true});
            t.add_child(label(fmtUsd(p[key].cost), 'cu-tile-value'));
            t.add_child(label(name, 'cu-tile-label'));
            t.add_child(label(_('%s · %d req.').format(fmtTokens(totalTokens(p[key])), p[key].requests), 'cu-tile-sub'));
            tiles.add_child(t);
        }
        this._content.add_child(tiles);

        const rate = p.last_hour.cost;
        const rateLine = hbox({style_class: 'cu-row'});
        rateLine.add_child(label(_('Last hour %s · last 5 h %s · yesterday %s').format(fmtUsd(rate), fmtUsd(p.last_5h.cost), fmtUsd(p.yesterday.cost)), 'cu-muted'));
        this._content.add_child(rateLine);

        // Gráfico diario (30 días)
        const daily = loc.daily;
        const caption = label('', 'cu-chart-caption');
        const defaultCaption = () => {
            const max = daily.reduce((a, b) => (b.cost > a.cost ? b : a), daily[0]);
            caption.text = _('Daily cost, 30 days · max. %s (%s)').format(fmtUsd(max.cost), fmtDay(max.date));
        };
        defaultCaption();
        this._content.add_child(caption);
        this._content.add_child(new BarChart(daily.map(x => x.cost), {
            height: 66,
            onHover: i => {
                if (i < 0) {
                    defaultCaption();
                    return;
                }
                const b = daily[i];
                caption.text = _('%s · %s · %s tokens · %d requests').format(fmtDay(b.date, '%A %-d %b'), fmtUsd(b.cost), fmtTokens(totalTokens(b)), b.requests);
            },
        }));
        const axis = hbox({x_expand: true});
        axis.add_child(label(fmtDay(daily[0].date, '%-d %b'), 'cu-muted'));
        axis.add_child(new St.Widget({x_expand: true}));
        axis.add_child(label(_('today'), 'cu-muted'));
        this._content.add_child(axis);

        // Hoy por horas
        const hourly = loc.hourly_today;
        if (p.today.requests > 0) {
            const hcap = label('', 'cu-chart-caption');
            const hDefault = () => {
                hcap.text = _('Today by hour · %s').format(fmtUsd(p.today.cost));
            };
            hDefault();
            this._content.add_child(hcap);
            this._content.add_child(new BarChart(hourly.map(x => x.cost), {
                height: 30,
                highlightLast: false,
                onHover: i => {
                    if (i < 0) {
                        hDefault();
                        return;
                    }
                    const b = hourly[i];
                    hcap.text = `${String(i).padStart(2, '0')}:00–${String(i + 1).padStart(2, '0')}:00 · ${fmtUsd(b.cost)} · ${fmtTokens(totalTokens(b))} tokens`;
                },
            }));
        }

        // Tokens por tipo (30 días)
        const t30 = p.days30;
        if (totalTokens(t30) > 0) {
            this._content.add_child(label(_('TOKENS · 30 DAYS · %s').format(fmtTokens(totalTokens(t30))), 'cu-section'));
            const keys = ['input', 'cache_write', 'cache_read', 'output'];
            this._content.add_child(new StackBar(keys.map(k => [t30[k], TOKEN_COLORS[k]])));
            const legend = hbox({style_class: 'cu-legend'});
            for (const k of keys) {
                const it = hbox({style: 'spacing: 4px;'});
                const c = TOKEN_COLORS[k].map(v => Math.round(v * 255));
                it.add_child(new St.Widget({style_class: 'cu-swatch', y_align: Clutter.ActorAlign.CENTER,
                    style: `background-color: rgb(${c.join(',')});`}));
                it.add_child(label(`${TOKEN_LABELS()[k]} ${fmtTokens(t30[k])}`));
                legend.add_child(it);
            }
            this._content.add_child(legend);
        }

        // Por modelo y por proyecto (30 días)
        this._breakdown(_('BY MODEL · 30 DAYS'), loc.models.map(m => [m.name, m.cost, totalTokens(m)]));
        this._breakdown(_('BY PROJECT · 30 DAYS'), loc.projects.slice(0, 4).map(m => [m.name, m.cost, totalTokens(m)]));
        if (loc.unpriced_models?.length)
            this._content.add_child(label(_('No known price: %s').format(loc.unpriced_models.join(', ')), 'cu-muted'));

        if (d.pricing?.updated)
            this._content.add_child(label(_('API price table of %s').format(d.pricing.updated), 'cu-muted'));

        // Pie
        const footer = hbox({style_class: 'cu-footer'});
        footer.add_child(label(_('Total recorded %s · %s tokens').format(fmtUsd(p.all.cost), fmtTokens(totalTokens(p.all))), 'cu-muted'));
        footer.add_child(new St.Widget({x_expand: true}));
        const web = new St.Button({style_class: 'cu-button', label: _('Usage on claude.ai'), can_focus: true});
        web.connect('clicked', () => {
            this.menu.close();
            Gio.AppInfo.launch_default_for_uri_async(USAGE_URL, null, null, null);
        });
        footer.add_child(web);
        this._content.add_child(footer);
    }

    _limitRow(l) {
        const row = vbox({style: 'spacing: 3px; padding-top: 2px;'});
        const top = hbox({style_class: 'cu-row'});
        top.add_child(label(l.label, 'cu-row-label'));
        top.add_child(new St.Widget({x_expand: true}));
        if (l.right)
            top.add_child(label(l.right, 'cu-num'));
        else
            top.add_child(label(`${Math.round(l.percent)}%`, `cu-pct ${severityClass(l.percent)}`));
        row.add_child(top);
        const pace = l.kind in WINDOW_SECS ? elapsedFraction(l.resets_at, l.kind) : null;
        row.add_child(new LimitBar(l.percent, pace, l.color ?? null));
        let subText = l.sub ?? fmtCountdown(l.resets_at);
        if (pace !== null && l.percent > 0) {
            const ahead = l.percent - pace * 100;
            if (Math.abs(ahead) >= 5)
                subText += ahead > 0 ? ` · ${_('%d pts ahead of pace').format(Math.round(ahead))}` : ` · ${_('%d pts below pace').format(Math.round(-ahead))}`;
        }
        row.add_child(label(subText, 'cu-muted'));
        if (Array.isArray(l.models))
            this._windowModels(row, l);
        return row;
    }

    // Consumo local por modelo dentro de la ventana del límite (sesión, semana o modelo concreto)
    _windowModels(row, l) {
        const box = vbox({style_class: 'cu-models'});
        if (!l.models.length) {
            box.add_child(label(_('No usage in this window'), 'cu-muted'));
            row.add_child(box);
            return;
        }
        const scoped = l.kind === 'weekly_model';
        for (const m of l.models) {
            const r = hbox({style_class: 'cu-row'});
            const dot = new St.Widget({style_class: 'cu-swatch', y_align: Clutter.ActorAlign.CENTER,
                style: `background-color: ${modelColor(m.name)};`});
            r.add_child(dot);
            r.add_child(label(m.name, 'cu-model-name'));
            r.add_child(new St.Widget({x_expand: true}));
            r.add_child(label(_('%s · %s · %d req.').format(fmtUsd(m.cost), fmtTokens(m.tokens), m.requests), 'cu-muted'));
            if (!scoped && l.models.length > 1)
                r.add_child(label(`≈${Math.round(m.est_percent)}%`, 'cu-num cu-model-pct'));
            box.add_child(r);
        }
        row.add_child(box);
    }

    _breakdown(title, rows) {
        if (!rows.length)
            return;
        this._content.add_child(label(title, 'cu-section'));
        const max = Math.max(...rows.map(r => r[1]), 1e-9);
        for (const [name, cost, tok] of rows) {
            const r = hbox({style_class: 'cu-row'});
            r.add_child(label(name, '', {style: 'min-width: 120px; max-width: 120px;'}));
            const bar = new LimitBar(100 * cost / max, null, ACCENT);
            bar.y_align = Clutter.ActorAlign.CENTER;
            r.add_child(bar);
            r.add_child(label(`${fmtUsd(cost)}`, 'cu-num', {style: 'min-width: 52px; text-align: right;'}));
            r.add_child(label(fmtTokens(tok), 'cu-muted', {style: 'min-width: 44px; text-align: right;'}));
            this._content.add_child(r);
        }
    }

    destroy() {
        this._clearHoverOpen();
        this._stopHoverPoll();
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
        this._cancellable.cancel();
        Gio.Settings.unbind(this._label, 'visible');
        this._settings = null;
        super.destroy();
    }
});

export default class ClaudeUsageExtension extends Extension {
    enable() {
        this._indicator = new ClaudeUsageIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
