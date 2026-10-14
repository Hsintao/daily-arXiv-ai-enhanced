/**
 * Theme runtime for Daily arXiv AI Enhanced.
 *
 * Loaded synchronously in <head> (no defer/async) so data-theme is set before
 * first paint and there is no flash of the wrong theme.
 *
 * Contract:
 *   - localStorage key 'themePreference': 'light' | 'dark' | 'system' (default 'system')
 *   - document.documentElement gets data-theme=<resolved light|dark>, plus the
 *     `dark` class, plus style.colorScheme for native form controls.
 *   - Any element with [data-theme-toggle] gets a click handler that calls toggle().
 *   - document receives a 'themechange' CustomEvent { detail: { theme, preference } }.
 *
 * This file must never throw: it runs before index.html installs window.onerror
 * (which paints a red error box), and storage access throws in Safari private mode.
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'themePreference';
    var VALID_MODES = { light: true, dark: true, system: true };
    var LIGHT = 'light';
    var DARK = 'dark';
    var SYSTEM = 'system';
    var DARK_QUERY = '(prefers-color-scheme: dark)';
    var doc = document;
    var root = doc.documentElement;

    var mql = null;
    try {
        if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
            mql = window.matchMedia(DARK_QUERY);
        }
    } catch (e) {
        mql = null;
    }

    function warn(e) {
        try {
            if (typeof console !== 'undefined' && console.warn) {
                console.warn('[theme]', e);
            }
        } catch (ignored) { /* console unavailable */ }
    }

    function readStored() {
        try {
            var v = window.localStorage.getItem(STORAGE_KEY);
            return VALID_MODES[v] ? v : null;
        } catch (e) {
            return null;
        }
    }

    function writeStored(mode) {
        try {
            window.localStorage.setItem(STORAGE_KEY, mode);
        } catch (e) {
            /* storage unavailable (private mode / disabled) - keep in-memory state */
        }
    }

    function systemPrefersDark() {
        try {
            return !!(mql && mql.matches);
        } catch (e) {
            return false;
        }
    }

    function resolve(pref) {
        if (pref === DARK || pref === LIGHT) return pref;
        return systemPrefersDark() ? DARK : LIGHT;
    }

    function currentPreference() {
        return readStored() || SYSTEM;
    }

    function syncToggleButtons(theme, pref) {
        var next = theme === DARK ? LIGHT : DARK;
        var label = 'Switch to ' + next + ' mode';
        var nodes = doc.querySelectorAll('[data-theme-toggle]');
        for (var i = 0; i < nodes.length; i++) {
            var btn = nodes[i];
            try {
                btn.setAttribute('aria-pressed', theme === DARK ? 'true' : 'false');
                btn.setAttribute('aria-label', label);
                btn.setAttribute('title', label);
                btn.setAttribute('data-theme-preference', pref);
            } catch (e) {
                /* detached/inert node - ignore */
            }
        }
    }

    function syncSelects(pref) {
        var selects = doc.querySelectorAll('#themeSelect');
        for (var i = 0; i < selects.length; i++) {
            try {
                if (selects[i].value !== pref) selects[i].value = pref;
            } catch (e) { /* ignore */ }
        }
    }

    function dispatchChange(theme, pref) {
        try {
            if (typeof window !== 'undefined' && typeof window.CustomEvent !== 'function') return;
            doc.dispatchEvent(new CustomEvent('themechange', {
                detail: { theme: theme, preference: pref }
            }));
        } catch (e) {
            /* older engines without CustomEvent constructor - non-fatal */
        }
    }

    /**
     * Resolve + paint the theme. Called synchronously at parse time and on change.
     * @param {string} [mode] optional preference override to store first.
     * @returns {string} the resolved theme ('light' | 'dark')
     */
    function apply(mode) {
        if (typeof mode === 'string' && VALID_MODES[mode]) {
            writeStored(mode);
        }
        var pref = currentPreference();
        var theme = resolve(pref);

        try {
            root.setAttribute('data-theme', theme);
            if (theme === DARK) {
                root.classList.add('dark');
            } else {
                root.classList.remove('dark');
            }
            root.style.colorScheme = theme;
        } catch (e) {
            warn(e);
        }

        syncToggleButtons(theme, pref);
        syncSelects(pref);
        dispatchChange(theme, pref);
        return theme;
    }

    function get() {
        return currentPreference();
    }

    function set(mode) {
        return apply(VALID_MODES[mode] ? mode : SYSTEM);
    }

    function toggle() {
        // Simple, predictable two-state cycle: light <-> dark.
        return set(resolve(currentPreference()) === DARK ? LIGHT : DARK);
    }

    function onSystemChange() {
        // Only follow the OS while the user has not chosen an explicit theme.
        if (currentPreference() === SYSTEM) apply();
    }

    function wireToggles() {
        var nodes = doc.querySelectorAll('[data-theme-toggle]');
        for (var i = 0; i < nodes.length; i++) {
            (function (btn) {
                if (btn.__themeWired) return;
                btn.__themeWired = true;
                btn.addEventListener('click', function (ev) {
                    if (ev && ev.preventDefault) ev.preventDefault();
                    toggle();
                });
            })(nodes[i]);
        }
        // refresh accessible state for whatever we just wired
        var pref = currentPreference();
        syncToggleButtons(resolve(pref), pref);
    }

    // --- synchronous anti-FOUC paint (parse time) ------------------------------
    apply();

    if (mql) {
        try {
            if (typeof mql.addEventListener === 'function') {
                mql.addEventListener('change', onSystemChange);
            } else if (typeof mql.addListener === 'function') {
                mql.addListener(onSystemChange); // Safari < 14
            }
        } catch (e) {
            warn(e);
        }
    }

    function boot() {
        wireToggles();
        var selects = doc.querySelectorAll('#themeSelect');
        for (var i = 0; i < selects.length; i++) {
            (function (sel) {
                if (sel.__themeWired) return;
                sel.__themeWired = true;
                sel.addEventListener('change', function () { set(sel.value); });
            })(selects[i]);
        }
    }

    if (doc.readyState === 'loading') {
        doc.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }

    window.Theme = {
        MODES: [LIGHT, DARK, SYSTEM],
        STORAGE_KEY: STORAGE_KEY,
        get: get,
        set: set,
        toggle: toggle,
        apply: apply,
        resolved: function () { return resolve(currentPreference()); }
    };
})();
