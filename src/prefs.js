// SPDX-License-Identifier: GPL-2.0-or-later
// Claude Usage — preferences window
import Adw from 'gi://Adw';
import Gio from 'gi://Gio';

import {ExtensionPreferences, gettext as _} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class ClaudeUsagePreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage({title: _('General'), icon_name: 'preferences-system-symbolic'});
        const panel = new Adw.PreferencesGroup({
            title: _('Top bar'),
            description: _('What the panel indicator shows'),
        });

        const pct = new Adw.SwitchRow({
            title: _('Percentage'),
            subtitle: _('5-hour session usage (or today’s cost when plan data is unavailable)'),
        });
        settings.bind('show-percent', pct, 'active', Gio.SettingsBindFlags.DEFAULT);
        panel.add(pct);

        page.add(panel);

        const data = new Adw.PreferencesGroup({
            title: _('Prices'),
            description: _('Costs are estimated at Claude API list prices'),
        });
        const prices = new Adw.SwitchRow({
            title: _('Update price table automatically'),
            subtitle: _('Once a day from the extension’s GitHub repository, which tracks Anthropic’s pricing page. Off: use the bundled table only.'),
        });
        settings.bind('update-prices', prices, 'active', Gio.SettingsBindFlags.DEFAULT);
        data.add(prices);
        page.add(data);
        window.add(page);
        window.set_default_size(560, 440);
        window._settings = settings;
    }
}
