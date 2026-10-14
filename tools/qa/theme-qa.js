#!/usr/bin/env node
/**
 * tools/qa/theme-qa.js — INDEPENDENT end-to-end verification of the dark mode.
 *
 * Owned by teammate `theme-qa` (task-3). Write scope: tools/qa/ ONLY.
 * This script is an adversary: it tries to falsify the dark-mode implementation.
 * It never edits css/*, js/* or *.html.
 *
 * Usage:
 *   node tools/qa/theme-qa.js                 # full run, prints PASS/FAIL table
 *   node tools/qa/theme-qa.js --port 8799     # pick a specific port
 *   node tools/qa/theme-qa.js --rev <git-ref> # record the revision under test
 *   node tools/qa/theme-qa.js --quiet         # only the summary table
 *
 * Exit code: 0 when every required check passes, 1 otherwise.
 *
 * Graceful degradation: if js/theme.js or [data-theme-toggle] does not exist
 * yet, the server and pages are still exercised and each check reports the
 * missing precondition instead of crashing.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const http = require('http');
const { execFileSync } = require('child_process');

const PW_CORE =
  '/Applications/ChatGPT.app/Contents/Resources/cua_node/lib/node_modules/playwright-core';
const CHROME =
  '/Users/guoxintao/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell';

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const OUT_DIR = __dirname;
const SHOT_DIR = path.join(OUT_DIR, 'screenshots');
const REPORT_JSON = path.join(OUT_DIR, 'theme-qa-report.json');

const PAGES = ['index.html', 'statistic.html', 'settings.html'];
const VIEWPORT = { width: 1440, height: 1000 };
const NAV_TIMEOUT = 20000;
const SETTLE_MS = 1200;

// Near-white backgrounds that would signal a leftover light-mode surface.
const NEAR_WHITE = new Set([
  'rgb(255, 255, 255)',
  'rgb(250, 251, 255)',
  'rgb(246, 248, 250)',
  'rgb(243, 244, 246)',
]);
const WHITE_AREA_THRESHOLD = 2000; // px^2

// ---------------------------------------------------------------- utilities

function parseArgs(argv) {
  const out = { port: 8791, rev: null, quiet: false };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--port') out.port = parseInt(argv[++i], 10);
    else if (a === '--rev') out.rev = argv[++i];
    else if (a === '--quiet') out.quiet = true;
  }
  return out;
}

const ARGS = parseArgs(process.argv);

function log(...args) {
  if (!ARGS.quiet) console.log(...args);
}

/** Dedupe an array by a key function, preserving first occurrence. */
function dedupeBy(arr, keyFn) {
  const seen = new Set();
  const out = [];
  for (const item of arr) {
    const k = keyFn(item);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(item);
  }
  return out;
}

function nowIso() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function git(cmd) {
  try {
    return execFileSync('git', cmd, { cwd: REPO_ROOT, encoding: 'utf8' }).trim();
  } catch (e) {
    return `<git ${cmd.join(' ')} failed: ${e.message.split('\n')[0]}>`;
  }
}

/** sha256 of every artifact under audit, for a citable frozen revision. */
function getArtifactHashes() {
  const files = [
    'css/styles.css',
    'css/statistic.css',
    'css/settings.css',
    'js/theme.js',
    'index.html',
    'statistic.html',
    'settings.html',
  ];
  const out = {};
  for (const f of files) {
    const p = path.join(REPO_ROOT, f);
    try {
      out[f] = require('crypto').createHash('sha256').update(fs.readFileSync(p)).digest('hex').slice(0, 16);
    } catch (e) {
      out[f] = '<missing>';
    }
  }
  return out;
}

// ---------------------------------------------------------- contrast helpers

function parseRgb(str) {
  if (!str) return null;
  const m = String(str).match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const parts = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
  if (parts.length < 3) return null;
  return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
}

/** src over dst (dst assumed opaque). */
function composite(src, dst) {
  const a = src.a === undefined ? 1 : src.a;
  return {
    r: src.r * a + dst.r * (1 - a),
    g: src.g * a + dst.g * (1 - a),
    b: src.b * a + dst.b * (1 - a),
    a: 1,
  };
}

function relLuminance(c) {
  const f = (v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
}

function contrastRatio(fg, bg) {
  const l1 = relLuminance(fg);
  const l2 = relLuminance(bg);
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

function rgbCss(c) {
  return `rgb(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)})`;
}

function isDark(c) {
  return relLuminance(c) < 0.18;
}

// ------------------------------------------------------------------ server

function startServer(root, port) {
  const MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.ico': 'image/x-icon',
    '.txt': 'text/plain; charset=utf-8',
  };
  const server = http.createServer((req, res) => {
    try {
      let urlPath = decodeURIComponent(req.url.split('?')[0]);
      if (urlPath === '/') urlPath = '/index.html';
      const abs = path.normalize(path.join(root, urlPath));
      if (!abs.startsWith(root)) {
        res.writeHead(403);
        return res.end('forbidden');
      }
      if (!fs.existsSync(abs) || fs.statSync(abs).isDirectory()) {
        res.writeHead(404, { 'content-type': 'text/plain' });
        return res.end('not found');
      }
      res.writeHead(200, { 'content-type': MIME[path.extname(abs)] || 'application/octet-stream' });
      fs.createReadStream(abs).pipe(res);
    } catch (e) {
      res.writeHead(500);
      res.end(String(e));
    }
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => resolve(server));
  });
}

// ---------------------------------------------------------- page eval code

/**
 * Collects per-element foreground/background/ratio data from the live DOM.
 * Runs in the browser. `NEAR_WHITE_SENTINEL` is passed in.
 */
/* eslint-disable no-undef */
const COLLECT_FN = function (opts) {
  const NEAR_WHITE = new Set(opts.nearWhite);
  const MIN_AREA = opts.minArea;

  function parseRgb(str) {
    if (!str) return null;
    const m = String(str).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const parts = m[1]
      .split(/[,\s/]+/)
      .filter(Boolean)
      .map(Number);
    if (parts.length < 3) return null;
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  }
  function composite(src, dst) {
    const a = src.a === undefined ? 1 : src.a;
    return {
      r: src.r * a + dst.r * (1 - a),
      g: src.g * a + dst.g * (1 - a),
      b: src.b * a + dst.b * (1 - a),
      a: 1,
    };
  }
  function relLum(c) {
    const f = (v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function ratio(a, b) {
    const l1 = relLum(a), l2 = relLum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }
  function css(c) {
    return 'rgb(' + Math.round(c.r) + ', ' + Math.round(c.g) + ', ' + Math.round(c.b) + ')';
  }

  // Root canvas colour. <html> usually has a fully transparent background even
  // when the theme is dark, so a transparent candidate must NOT win the `||`
  // chain: that would composite everything over white and report phantom
  // failures. Take the first candidate that actually paints.
  function firstPainted(candidates, fallback) {
    for (const c of candidates) {
      if (c && c.a > 0.02) return c;
    }
    return fallback;
  }
  const pageBgRaw = firstPainted(
    [
      parseRgb(getComputedStyle(document.documentElement).backgroundColor),
      parseRgb(getComputedStyle(document.body).backgroundColor),
    ],
    { r: 255, g: 255, b: 255, a: 1 }
  );
  const pageBg = pageBgRaw.a < 1 ? composite(pageBgRaw, { r: 255, g: 255, b: 255, a: 1 }) : pageBgRaw;

  /** Walk ancestors for the stacked opaque background behind `el`. */
  function effectiveBg(el) {
    const layers = [];
    let node = el;
    while (node && node.nodeType === 1) {
      const bg = parseRgb(getComputedStyle(node).backgroundColor);
      if (bg && bg.a > 0.02) {
        layers.push(bg);
        if (bg.a >= 0.999) {
          let acc = bg;
          for (let i = layers.length - 2; i >= 0; i--) acc = composite(layers[i], acc);
          return { color: acc, sourcedFrom: describe(node) };
        }
      }
      node = node.parentElement;
    }
    let acc = pageBg;
    for (let i = layers.length - 1; i >= 0; i--) acc = composite(layers[i], acc);
    return { color: acc, sourcedFrom: 'html/body' };
  }

  function describe(el) {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (el.classList && el.classList.length) {
      s += '.' + Array.from(el.classList).slice(0, 3).join('.');
    }
    return s;
  }

  function selectorPath(el) {
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 4) {
      let s = node.tagName.toLowerCase();
      if (node.id) {
        s += '#' + node.id;
        parts.unshift(s);
        break;
      }
      if (node.classList && node.classList.length) {
        s += '.' + Array.from(node.classList).slice(0, 2).join('.');
      }
      parts.unshift(s);
      node = node.parentElement;
      depth++;
    }
    return parts.join(' > ');
  }

  function visible(el, rect, cs) {
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
    if (parseFloat(cs.opacity) < 0.05) return false;
    if (rect.width < 1 || rect.height < 1) return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    // Chromium headless-shell leaves external stylesheets pending indefinitely,
    // so flatpickr's own calendar renders unstyled (its CDN CSS never applies).
    // Its ~40 day cells would then all be scored against rules we do not own.
    // Keep the container (a real app-styled surface) but drop the widget internals.
    if (el.closest('.flatpickr-calendar')) return false;
    return true;
  }

  /** Direct text nodes only, so a wrapper is not blamed for its children. */
  function ownText(el) {
    let t = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) t += n.nodeValue;
    }
    return t.replace(/\s+/g, ' ').trim();
  }

  const all = Array.from(document.querySelectorAll('body *'));
  const textIssues = [];
  const whiteIssues = [];
  const inspected = { elements: 0, textElements: 0 };

  for (const el of all) {
    const cs = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (!visible(el, rect, cs)) continue;
    inspected.elements++;

    const bgInfo = effectiveBg(el);
    const fg = parseRgb(cs.color);
    if (!fg) continue;
    const fgEff = fg.a < 1 ? composite(fg, bgInfo.color) : fg;

    // --- check 6: near-white painted surface
    const ownBg = parseRgb(cs.backgroundColor);
    const area = rect.width * rect.height;
    if (ownBg && ownBg.a > 0.5 && NEAR_WHITE.has(css(ownBg)) && area > MIN_AREA) {
      whiteIssues.push({
        selector: selectorPath(el),
        tag: describe(el),
        backgroundColor: css(ownBg),
        rawBackground: cs.backgroundColor,
        area: Math.round(area),
        text: ownText(el).slice(0, 60),
      });
    }

    // --- check 5: contrast for elements that render their own text
    const text = ownText(el);
    if (!text) continue;
    inspected.textElements++;
    const fontSize = parseFloat(cs.fontSize) || 16;
    const weight = parseInt(cs.fontWeight, 10) || 400;
    const isLarge = fontSize >= 24 || (fontSize >= 18.66 && weight >= 700);
    const threshold = isLarge ? 3.0 : 4.5;
    const r = ratio(fgEff, bgInfo.color);

    if (r < threshold) {
      textIssues.push({
        selector: selectorPath(el),
        tag: describe(el),
        text: text.slice(0, 70),
        fontSize: Math.round(fontSize * 10) / 10,
        fontWeight: weight,
        isLarge,
        threshold,
        ratio: Math.round(r * 100) / 100,
        color: css(fgEff),
        rawColor: cs.color,
        background: css(bgInfo.color),
        bgFrom: bgInfo.sourcedFrom,
        sameSideInvisible: r < 1.3,
        fgIsDark: relLum(fgEff) < 0.18,
        bgIsDark: relLum(bgInfo.color) < 0.18,
        textSource: 'direct-text-node',
      });
    }
  }

  // --- check 5b: "both dark" / "both light" invisible text.
  // Some painted text is not a direct text node: inline <span>s, <a> wrappers,
  // and prose elements whose text lives in descendants. Walk every element that
  // has ANY rendered textContent and test the element's own colour against the
  // first painted background behind it. This catches a card whose background
  // went dark while its text colour stayed dark (the classic inverted-token bug).
  const invisibleIssues = [];
  for (const el of all) {
    const cs = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (!visible(el, rect, cs)) continue;
    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!txt) continue;
    // Only score elements that actually paint glyphs themselves: a block with
    // text children would otherwise be compared using an inherited colour.
    const hasDirectText = ownText(el).length > 0;
    const isLeaf = el.children.length === 0;
    if (!hasDirectText && !isLeaf) continue;

    const bgInfo = effectiveBg(el);
    const fg = parseRgb(cs.color);
    if (!fg) continue;
    const fgEff = fg.a < 1 ? composite(fg, bgInfo.color) : fg;
    const r = ratio(fgEff, bgInfo.color);
    if (r >= 1.3) continue;

    const fontSize = parseFloat(cs.fontSize) || 16;
    const weight = parseInt(cs.fontWeight, 10) || 400;
    const isLarge = fontSize >= 24 || (fontSize >= 18.66 && weight >= 700);
    const threshold = isLarge ? 3.0 : 4.5;
    const fgIsDark = relLum(fgEff) < 0.18;
    const bgIsDark = relLum(bgInfo.color) < 0.18;
    invisibleIssues.push({
      selector: selectorPath(el),
      tag: describe(el),
      text: txt.slice(0, 70),
      ratio: Math.round(r * 100) / 100,
      threshold,
      isLarge,
      color: css(fgEff),
      rawColor: cs.color,
      background: css(bgInfo.color),
      bgFrom: bgInfo.sourcedFrom,
      fgIsDark,
      bgIsDark,
      bothDark: fgIsDark && bgIsDark,
      bothLight: !fgIsDark && !bgIsDark,
      textSource: hasDirectText ? 'direct-text-node' : 'descendant-text',
    });
  }

  return {
    dataTheme: document.documentElement.getAttribute('data-theme'),
    colorScheme: getComputedStyle(document.documentElement).colorScheme,
    htmlClass: document.documentElement.className,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    pageBg: css(pageBg),
    toggleCount: document.querySelectorAll('[data-theme-toggle]').length,
    errorBoxes: Array.from(document.querySelectorAll('div')).filter(
      (d) => d.style && d.style.position === 'fixed' && /rgba\(255,\s*0,\s*0/.test(d.style.backgroundColor || '')
    ).length,
    inspected,
    textIssues,
    invisibleIssues,
    whiteIssues,
  };
};
/* eslint-enable no-undef */

// ------------------------------------------------------- synthetic fixture

/**
 * Injects representative components into the live DOM so the dark CSS is
 * exercised even when the GitHub data branch / CDNs are unreachable.
 * Mirrors the real markup produced by js/app.js renderPapers().
 */
/* eslint-disable no-undef */
const INJECT_FIXTURE_FN = function () {
  const NS = 'qa-synthetic';
  const host = document.createElement('div');
  host.id = 'qa-synthetic-host';
  host.setAttribute('data-qa', NS);

  const cats = ['cs.CV', 'cs.LG', 'eess.IV'];
  const topics = ['Low-level Vision', 'Computational Photography'];
  let cards = '';
  for (let i = 0; i < 3; i++) {
    cards += `
      <div class="paper-card matched-paper" data-qa-card="${i}">
        <div class="paper-card-index">${i + 1}</div>
        <div class="match-badge" title="matched"></div>
        <div class="paper-card-header">
          <h3 class="paper-card-title">Synthetic dark-mode probe title ${i + 1} <span class="keyword-highlight">highlight</span> <span class="topic-highlight">topic</span></h3>
          <p class="paper-card-authors">Ada Lovelace, Alan Turing, Grace Hopper</p>
          <div class="topic-badges"><span class="topic-badge">${topics[i % topics.length]}</span></div>
          <div class="paper-card-categories">${cats
            .map((c) => `<span class="category-tag">${c}</span>`)
            .join('')}</div>
        </div>
        <div class="paper-card-body">
          <p class="paper-card-summary">Synthetic summary text used to measure contrast on the card body. It is long enough to wrap onto several lines.</p>
          <div class="paper-card-footer">
            <div class="footer-left"><span class="paper-card-date">2025-01-0${i + 1}</span></div>
            <span class="paper-card-link">Details</span>
          </div>
        </div>
      </div>`;
  }

  host.innerHTML = `
    <div class="qa-section" style="padding:16px">
      <div class="category-nav">
        <div class="category-label-container">
          <span class="category-nav-label">Category</span>
          <div class="category-scroll">
            <button class="category-button active" data-category="all">All<span class="category-count">3</span></button>
            <button class="category-button" data-category="cs.CV">cs.CV<span class="category-count">2</span></button>
            <button class="category-button keyword-button active" data-keyword="super resolution">super resolution</button>
            <button class="category-button author-button" data-author="Ada Lovelace">Ada Lovelace</button>
          </div>
        </div>
        <div class="filter-label-container">
          <span class="filter-nav-label">Filter</span>
          <div class="filter-scroll" id="qaFilterTags">
            <span class="category-button author-button active">Ada Lovelace</span>
            <span class="category-button keyword-button active">super resolution</span>
          </div>
        </div>
      </div>

      <div class="paper-container">
        ${cards}
        <div class="loading-container"><div class="loading-spinner"></div><p>Loading papers...</p></div>
      </div>

      <div class="date-picker-modal" style="display:block;position:static">
        <div class="date-picker-content">
          <div class="date-picker-body">
            <h3 class="date-picker-title">Select Date</h3>
            <div class="flatpickr-container"><input type="text" id="qaDatepicker" placeholder="Select Date"></div>
            <div class="date-range-selector">
              <span class="toggle-label">Range</span>
              <label class="toggle-switch"><input type="checkbox"><span class="toggle-slider"></span></label>
            </div>
          </div>
        </div>
      </div>

      <div class="paper-modal" style="display:block;position:static">
        <div class="paper-modal-content">
          <div class="paper-modal-header"><h2>Synthetic modal title</h2>
            <button class="icon-button">x</button></div>
          <div class="paper-modal-body">
            <p>Synthetic modal body text for contrast measurement.</p>
            <div class="paper-detail-section"><h3>Motivation</h3><p>Why it matters.</p></div>
            <pre><code>print("dark mode")</code></pre>
          </div>
          <div class="paper-modal-footer">
            <div class="paper-navigation"><span class="navigation-hint">arrows to navigate</span>
              <span class="paper-position">1 / 3</span></div>
            <div class="paper-modal-buttons">
              <a href="#" class="button icon-button primary">Open in arXiv</a>
              <a href="#" class="button icon-button">Open PDF</a>
            </div>
          </div>
        </div>
      </div>

      <div class="qa-controls" style="padding:16px">
        <button class="button">Default button</button>
        <button class="button primary">Primary button</button>
        <button class="icon-button" title="icon">i</button>
        <input type="text" placeholder="Plain input" />
        <input type="text" class="settings-input" placeholder="Settings input" />
        <textarea placeholder="textarea"></textarea>
        <select><option>choose</option></select>
        <span class="tag">plain tag</span>
        <div class="empty-tag-message">No keywords added yet. Add some keywords below.</div>
      </div>
    </div>`;

  const container = document.getElementById('paperContainer');
  if (container) {
    // index.html: replace the "Loading papers..." placeholder with the fixture so
    // the real container position/size is used.
    container.innerHTML = '';
    container.appendChild(host);
  } else {
    const main = document.querySelector('main') || document.body;
    main.appendChild(host);
  }
  return host.querySelectorAll('.paper-card').length;
};
/* eslint-enable no-undef */

// ------------------------------------------------------- defect-site probes

/**
 * The exact sites implicated by the task-4 defects (D1-D4). Each entry states
 * the assertion that must hold. `mode` selects the dark or light expectation.
 */
const DEFECT_SITES = [
  {
    id: 'D1',
    page: 'statistic.html',
    selector: '#paperSidebar',
    what: 'sidebar panel surface',
    cssFile: 'css/statistic.css',
    dark: { mustNotBeNearWhite: true },
    light: { mustBeNearWhite: true },
  },
  {
    id: 'D2',
    page: 'statistic.html',
    selector: '.keyword-list .keyword-item .keyword-count',
    what: 'keyword count pill text vs its own background',
    cssFile: 'css/statistic.css',
    dark: { minContrast: 4.5 },
    // The pill must stay a white-ish pill on a light page.
    light: { mustBeNearWhite: true },
  },
  {
    id: 'D3a',
    page: 'settings.html',
    selector: '#emptyTagMessage',
    what: 'empty keywords message',
    cssFile: 'css/settings.css',
    dark: { minContrast: 4.5 },
    light: { minContrast: 4.5 },
  },
  {
    id: 'D3b',
    page: 'settings.html',
    selector: '#emptyAuthorMessage',
    what: 'empty authors message',
    cssFile: 'css/settings.css',
    dark: { minContrast: 4.5 },
    light: { minContrast: 4.5 },
  },
  {
    id: 'D3c',
    page: 'settings.html',
    selector: '#selectedKeywords',
    what: 'selected-tags translucent well',
    cssFile: 'css/settings.css',
    dark: { mustCompositeDark: true },
    light: { mustCompositeLight: true },
  },
  {
    id: 'D4a',
    page: 'statistic.html',
    selector: '.keyword-highlight',
    what: 'keyword highlight chip in a paper card title',
    cssFile: 'css/statistic.css',
    dark: { minContrast: 4.5 },
    light: { minContrast: 4.5 },
  },
  {
    id: 'D4b',
    page: 'statistic.html',
    selector: '.keyword-line',
    what: 'keyword line text',
    cssFile: 'css/statistic.css',
    dark: { minContrast: 4.5 },
    light: { minContrast: 4.5 },
  },
];

/**
 * Measures the defect sites in the REAL DOM. Returns per-site color, effective
 * background, WCAG ratio and the assertion verdicts, so the same numbers can be
 * shown for the dark fix and the light-neutrality regression.
 */
/* eslint-disable no-undef */
const DEFECT_SITE_FN = function ({ sites, lightMode }) {
  function parse(str) {
    const m = String(str || '').match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function comp(src, dst) {
    const a = src.a === undefined ? 1 : src.a;
    return { r: src.r * a + dst.r * (1 - a), g: src.g * a + dst.g * (1 - a), b: src.b * a + dst.b * (1 - a), a: 1 };
  }
  function lum(c) {
    const f = (v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function ratio(a, b) {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }
  function css(c) {
    return 'rgb(' + Math.round(c.r) + ', ' + Math.round(c.g) + ', ' + Math.round(c.b) + ')';
  }
  function nearWhite(c) {
    return c.r > 240 && c.g > 240 && c.b > 240;
  }
  function rootBg() {
    const h = parse(getComputedStyle(document.documentElement).backgroundColor);
    const b = parse(getComputedStyle(document.body).backgroundColor);
    if (h && h.a > 0.02) return h;
    if (b && b.a > 0.02) return b;
    return { r: 255, g: 255, b: 255, a: 1 };
  }
  const ROOT = rootBg();

  /** Stacked opaque background behind el, starting at el itself. */
  function effBg(el) {
    const layers = [];
    let node = el;
    while (node && node.nodeType === 1) {
      const bg = parse(getComputedStyle(node).backgroundColor);
      if (bg && bg.a > 0.02) {
        layers.push(bg);
        if (bg.a >= 0.999) {
          let acc = bg;
          for (let i = layers.length - 2; i >= 0; i--) acc = comp(layers[i], acc);
          return acc;
        }
      }
      node = node.parentElement;
    }
    let acc = ROOT;
    for (let i = layers.length - 1; i >= 0; i--) acc = comp(layers[i], acc);
    return acc;
  }

  const out = [];
  // Card-scoped sites (D4a/D4b) only exist once a paper card is rendered. When
  // the data branch is unreachable the page has no card, so build a minimal one
  // with the same markup js/app.js uses; otherwise the probe would be
  // inconclusive and could hide a real regression.
  const cardScoped = sites.some((s) => /\.keyword-(highlight|line)\b/.test(s.selector));
  if (cardScoped && !document.querySelector('.keyword-highlight')) {
    const host = document.createElement('div');
    host.setAttribute('data-qa-card-probe', '1');
    host.innerHTML =
      '<div class="paper-card matched-paper">' +
      '<div class="paper-card-header"><h3 class="paper-card-title">' +
      '<span class="keyword-highlight">highlight</span></h3>' +
      '<p class="keyword-line">synthetic keyword line</p>' +
      '</div></div>';
    const c = document.getElementById('paperContainer') || document.querySelector('main') || document.body;
    c.appendChild(host);
  }

  // The keyword-count pill only renders once statistics have loaded from the
  // data branch. Build the same row markup so the pill is measurable; without
  // it the probe is inconclusive and a real contrast regression could hide.
  const needsCount = sites.some((s) => /\.keyword-count\b/.test(s.selector));
  if (needsCount && !document.querySelector('.keyword-count')) {
    const host = document.createElement('div');
    host.setAttribute('data-qa-count-probe', '1');
    host.innerHTML =
      '<div class="statistics-card"><div class="keyword-list">' +
      '<div class="keyword-item"><span class="keyword-text">synthetic keyword</span>' +
      '<span class="keyword-count">12</span></div>' +
      '</div></div>';
    const c =
      document.getElementById('papersList') ||
      document.getElementById('paperContainer') ||
      document.querySelector('main') ||
      document.body;
    c.appendChild(host);
  }
  for (const site of sites) {
    const isThisPage = new RegExp(site.page.replace('.html', '') + '\\.html$').test(
      location.pathname
    );
    if (!isThisPage) continue;
    const el = document.querySelector(site.selector);
    const rule = lightMode ? site.light : site.dark;
    if (!el) {
      out.push({
        id: site.id,
        page: site.page,
        selector: site.selector,
        what: site.what,
        mode: lightMode ? 'light' : 'dark',
        present: false,
        // An absent selector is a probe limitation, not a failure. It must not
        // count as a pass either, so mark it inconclusive and exclude it from
        // the aggregate instead of poisoning the verdict.
        inconclusive: true,
        pass: true,
        reason: 'SELECTOR NOT FOUND in the real DOM (probe inconclusive)',
      });
      continue;
    }
    const cs = getComputedStyle(el);
    const own = parse(cs.backgroundColor);
    const bg = effBg(el);
    const fg = parse(cs.color) || { r: 0, g: 0, b: 0, a: 1 };
    const fgEff = fg.a < 1 ? comp(fg, bg) : fg;
    const r = ratio(fgEff, bg);
    const ownPainted = own && own.a > 0.02 ? (own.a < 1 ? comp(own, bg) : own) : null;

    const verdicts = {};
    if (rule.minContrast !== undefined) verdicts.contrastOK = r >= rule.minContrast;
    if (rule.mustNotBeNearWhite) {
      verdicts.notNearWhite = own ? !nearWhite(own.a < 1 ? comp(own, bg) : own) : true;
    }
    if (rule.mustBeNearWhite) {
      verdicts.isNearWhite = own ? nearWhite(own.a < 1 ? comp(own, bg) : own) : false;
    }
    if (rule.mustCompositeDark) verdicts.compositeDark = lum(bg) < 0.25;
    if (rule.mustCompositeLight) verdicts.compositeLight = lum(bg) > 0.5;

    const rect = el.getBoundingClientRect();
    out.push({
      id: site.id,
      page: site.page,
      selector: site.selector,
      what: site.what,
      cssFile: site.cssFile,
      mode: lightMode ? 'light' : 'dark',
      present: true,
      rawColor: cs.color,
      rawBackground: cs.backgroundColor,
      color: css(fgEff),
      effectiveBackground: css(bg),
      ownPaintedBackground: ownPainted ? css(ownPainted) : null,
      ratio: Math.round(r * 100) / 100,
      opacity: cs.opacity,
      fontSize: cs.fontSize,
      visible: rect.width > 0 && rect.height > 0 && cs.display !== 'none',
      expected: rule,
      verdicts,
      pass: Object.values(verdicts).every(Boolean),
    });
  }
  return { mode: lightMode ? 'light' : 'dark', dataTheme: document.documentElement.getAttribute('data-theme'), sites: out };
};
/* eslint-enable no-undef */

/** Exactly one theme glyph must be visible, matching the resolved theme. */
/* eslint-disable no-undef */
const THEME_ICON_FN = function () {
  const btn = document.querySelector('[data-theme-toggle]');
  const theme = document.documentElement.getAttribute('data-theme');
  // Look for the sun/moon glyphs anywhere (button or a wrapper), since markup
  // differs per page. `fill="currentColor"` means they are painted, not hidden.
  const q = (sel) => Array.from(document.querySelectorAll(sel));
  const visible = (el) => {
    if (!el) return false;
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.05 && r.width > 0 && r.height > 0;
  };
  const suns = q('.theme-icon-sun');
  const moons = q('.theme-icon-moon');
  const sunVisible = suns.filter(visible).length;
  const moonVisible = moons.filter(visible).length;
  const expected = theme === 'dark' ? 'moon' : theme === 'light' ? 'sun' : 'unknown';
  const wantCount = expected === 'sun' ? sunVisible : moonVisible;
  const otherCount = expected === 'sun' ? moonVisible : sunVisible;
  return {
    theme,
    hasToggle: !!btn,
    sunCount: suns.length,
    moonCount: moons.length,
    sunVisible,
    moonVisible,
    expected,
    pass: !!btn && suns.length + moons.length > 0 && wantCount >= 1 && otherCount === 0,
    reason:
      suns.length + moons.length === 0
        ? 'no .theme-icon-sun/.theme-icon-moon glyphs present'
        : wantCount < 1
        ? `${expected} glyph not visible (sun=${sunVisible}, moon=${moonVisible})`
        : otherCount !== 0
        ? `both glyphs visible (sun=${sunVisible}, moon=${moonVisible}); exactly one expected`
        : 'ok',
  };
};
/* eslint-enable no-undef */

// --------------------------------------------------------------- navigation

async function gotoAndSettle(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
  await sleep(SETTLE_MS);
}

async function readTheme(page) {
  return page.evaluate(() => ({
    attr: document.documentElement.getAttribute('data-theme'),
    cls: document.documentElement.className,
    pref: (() => {
      try {
        return localStorage.getItem('themePreference');
      } catch (e) {
        return `<threw: ${e.name}>`;
      }
    })(),
    colorScheme: getComputedStyle(document.documentElement).colorScheme,
  }));
}

// ------------------------------------------------------------- main runner

async function main() {
  const runStart = nowIso();
  const report = {
    tool: 'tools/qa/theme-qa.js',
    task: 'task-3',
    startedAt: runStart,
    finishedAt: null,
    revision: ARGS.rev || git(['rev-parse', 'HEAD']),
    gitStatus: git(['status', '--short']),
    // Provenance: the exact bytes audited, so the Lead can cite a frozen tree.
    artifactHashes: getArtifactHashes(),
    url: `http://127.0.0.1:${ARGS.port}/`,
    preconditions: {},
    environment: {},
    checks: {},
    contrastOffenders: [],
    invisibleTextOffenders: [],
    whiteSurfaceOffenders: [],
    defects: [],
    screenshots: [],
    trusted: {},
    notes: [],
  };

  // ---- preconditions (graceful degradation) -------------------------------
  const themeJsPath = path.join(REPO_ROOT, 'js', 'theme.js');
  report.preconditions.themeJsExists = fs.existsSync(themeJsPath);
  const cssPath = path.join(REPO_ROOT, 'css', 'styles.css');
  const cssText = fs.existsSync(cssPath) ? fs.readFileSync(cssPath, 'utf8') : '';
  report.preconditions.darkTokenBlockInCss = /\[data-theme=["']dark["']\]/.test(cssText);
  const htmlHasToggle = {};
  for (const p of PAGES) {
    const f = path.join(REPO_ROOT, p);
    htmlHasToggle[p] = fs.existsSync(f)
      ? /data-theme-toggle/.test(fs.readFileSync(f, 'utf8'))
      : false;
  }
  report.preconditions.toggleMarkupInHtml = htmlHasToggle;
  report.preconditions.allHtmlHaveToggle = Object.values(htmlHasToggle).every(Boolean);
  report.preconditions.scriptsLoadThemeJs = {};
  for (const p of PAGES) {
    const f = path.join(REPO_ROOT, p);
    report.preconditions.scriptsLoadThemeJs[p] = fs.existsSync(f)
      ? /js\/theme\.js/.test(fs.readFileSync(f, 'utf8'))
      : false;
  }

  log('=== preconditions ===');
  log(JSON.stringify(report.preconditions, null, 2));

  for (const p of PAGES) {
    if (!htmlHasToggle[p]) report.notes.push(`PRECONDITION MISSING: no [data-theme-toggle] in ${p}`);
    if (!report.preconditions.scriptsLoadThemeJs[p])
      report.notes.push(`PRECONDITION MISSING: ${p} does not load js/theme.js`);
  }
  if (!report.preconditions.themeJsExists) {
    report.notes.push('PRECONDITION MISSING: js/theme.js does not exist');
  }
  if (!report.preconditions.darkTokenBlockInCss) {
    report.notes.push('PRECONDITION MISSING: css/styles.css has no [data-theme="dark"] block');
  }

  // ---- browser / server ---------------------------------------------------
  let pw;
  try {
    pw = require(PW_CORE);
  } catch (e) {
    report.fatal = `cannot require playwright-core at ${PW_CORE}: ${e.message}`;
    finishAndWrite(report);
    return 1;
  }
  if (!fs.existsSync(CHROME)) {
    report.fatal = `chromium headless shell not found at ${CHROME}`;
    finishAndWrite(report);
    return 1;
  }

  fs.mkdirSync(SHOT_DIR, { recursive: true });

  let server;
  try {
    server = await startServer(REPO_ROOT, ARGS.port);
  } catch (e) {
    report.fatal = `cannot listen on port ${ARGS.port}: ${e.message}`;
    finishAndWrite(report);
    return 1;
  }
  const base = `http://127.0.0.1:${ARGS.port}/`;
  log(`static server up on ${base}`);

  const browser = await pw.chromium.launch({ headless: true, executablePath: CHROME });

  const consoleErrors = {}; // page -> []
  const pageErrors = {};
  const networkFailures = []; // {status, url, where}
  function attachListeners(page, key) {
    consoleErrors[key] = consoleErrors[key] || [];
    pageErrors[key] = pageErrors[key] || [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors[key].push(msg.text().slice(0, 300));
    });
    page.on('pageerror', (err) => {
      pageErrors[key].push(String(err && err.message ? err.message : err).slice(0, 300));
    });
    // Chromium's console text omits the failing URL ("Failed to load resource"),
    // so capture the URL at the network layer where it is still available.
    page.on('response', (res) => {
      if (res.status() >= 400) {
        networkFailures.push({ status: res.status(), url: res.url().slice(0, 300), where: key });
      }
    });
    page.on('requestfailed', (req) => {
      networkFailures.push({
        status: 'requestfailed',
        url: req.url().slice(0, 300),
        failure: (req.failure() && req.failure().errorText) || '',
        where: key,
      });
    });
  }

  try {
  // ======================================================= context: light default
  {
    const ctx = await browser.newContext({ viewport: VIEWPORT, colorScheme: 'light' });
    const check = { name: '1. theme boots light by default', pages: {} };
    for (const p of PAGES) {
      const page = await ctx.newPage();
      attachListeners(page, `check1:${p}`);
      await gotoAndSettle(page, base + p);
      const theme = await readTheme(page);
      const errBox = await page.evaluate(
        () =>
          Array.from(document.querySelectorAll('div')).filter(
            (d) =>
              d.style &&
              d.style.position === 'fixed' &&
              /rgba\(255,\s*0,\s*0/.test(d.style.backgroundColor || '')
          ).length
      );
      check.pages[p] = {
        dataTheme: theme.attr,
        pass: theme.attr === 'light',
        onerrorBoxes: errBox,
        pageErrors: pageErrors[`check1:${p}`],
        consoleErrors: consoleErrors[`check1:${p}`],
      };
      await page.close();
    }
    // No page JS error on any page, and no visible window.onerror box.
    const jsErrFree = PAGES.every(
      (p) => (pageErrors[`check1:${p}`] || []).length === 0
    );
    const noBoxes = PAGES.every((p) => check.pages[p].onerrorBoxes === 0);
    check.jsErrorFree = jsErrFree;
    check.noOnErrorBox = noBoxes;
    check.pass = PAGES.every((p) => check.pages[p].pass) && jsErrFree && noBoxes;
    report.checks.bootLight = check;
    await ctx.close();
  }

  // ======================================================= context: toggle + shots
  const shotsTaken = new Set();
  async function shoot(page, name, fullPage) {
    const file = path.join(SHOT_DIR, `${name}.png`);
    await page.screenshot({ path: file, fullPage: !!fullPage });
    shotsTaken.add(path.relative(REPO_ROOT, file));
    return path.relative(REPO_ROOT, file);
  }

  {
    const ctx = await browser.newContext({ viewport: VIEWPORT, colorScheme: 'light' });
    const check = { name: '2. toggle works (click flips theme + storage + aria)', pages: {} };
    for (const p of PAGES) {
      const page = await ctx.newPage();
      attachListeners(page, `check2:${p}`);
      await gotoAndSettle(page, base + p);
      const hasToggle = (await page.locator('[data-theme-toggle]').count()) > 0;
      const entry = { hasToggle };
      if (!hasToggle) {
        entry.pass = false;
        entry.reason = 'PRECONDITION MISSING: no [data-theme-toggle] in this page';
        check.pages[p] = entry;
        await page.close();
        continue;
      }
      const before = await readTheme(page);
      const btn = page.locator('[data-theme-toggle]').first();
      const ariaBefore = await btn.getAttribute('aria-pressed');
      const titleBefore = await btn.getAttribute('title');
      await btn.click();
      await sleep(150);
      const afterDark = await readTheme(page);
      const ariaDark = await btn.getAttribute('aria-pressed');
      const titleDark = await btn.getAttribute('title');
      await btn.click();
      await sleep(150);
      const afterLight = await readTheme(page);
      const ariaLight = await btn.getAttribute('aria-pressed');
      const titleLight = await btn.getAttribute('title');

      entry.before = before;
      entry.afterFirstClick = afterDark;
      entry.afterSecondClick = afterLight;
      entry.aria = { before: ariaBefore, dark: ariaDark, light: ariaLight };
      entry.title = { before: titleBefore, dark: titleDark, light: titleLight };
      entry.pass =
        afterDark.attr === 'dark' &&
        afterDark.pref === 'dark' &&
        afterLight.attr === 'light' &&
        afterLight.pref === 'light' &&
        ariaDark === 'true' &&
        ariaLight === 'false' &&
        titleDark !== titleLight;
      check.pages[p] = entry;
      await page.close();
    }
    check.pass = PAGES.every((p) => check.pages[p].pass);
    report.checks.toggle = check;
    await ctx.close();
  }

  // ======================================================= check 3: persistence + FOUC
  {
    const ctx = await browser.newContext({ viewport: VIEWPORT, colorScheme: 'light' });
    const check = { name: '3. persistence + no FOUC', pages: {} };
    for (const p of PAGES) {
      const page = await ctx.newPage();
      attachListeners(page, `check3:${p}`);
      // Seed dark preference BEFORE any page script runs, and record the paint
      // sequence: what data-theme is at the very first observable moment, and
      // what the body background is at each animation frame.
      await page.addInitScript(() => {
        try {
          localStorage.setItem('themePreference', 'dark');
        } catch (e) {}
        window.__qaPaint = [];
        const sampler = () => {
          try {
            window.__qaPaint.push({
              t: Math.round(performance.now()),
              theme: document.documentElement.getAttribute('data-theme'),
              bg: document.body ? getComputedStyle(document.body).backgroundColor : null,
              htmlBg: getComputedStyle(document.documentElement).backgroundColor,
            });
          } catch (e) {}
        };
        sampler();
        document.addEventListener('readystatechange', sampler);
        document.addEventListener('DOMContentLoaded', sampler);
        const raf = () => {
          sampler();
          if (window.__qaPaint.length < 40) requestAnimationFrame(raf);
        };
        requestAnimationFrame(raf);
        window.addEventListener('load', sampler);
      });
      await page.goto(base + p, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
      // Read at domcontentloaded before sleeping: this is the "first paint" probe.
      const atDcl = await page.evaluate(() => ({
        theme: document.documentElement.getAttribute('data-theme'),
        bg: document.body ? getComputedStyle(document.body).backgroundColor : null,
      }));
      const shot60 = await page.screenshot({ clip: { x: 0, y: 0, width: 200, height: 200 } });
      await sleep(1400);
      const shot1500 = await page.screenshot({ clip: { x: 0, y: 0, width: 200, height: 200 } });
      const after = await readTheme(page);
      const paint = await page.evaluate(() => window.__qaPaint || []);

      const darkTheme = (v) => v === 'dark';
      const firstThemes = paint.filter((s) => s.theme !== null).slice(0, 3).map((s) => s.theme);
      const nonTransparent = paint
        .filter((s) => s.bg && s.bg !== 'rgba(0, 0, 0, 0)')
        .map((s) => s.bg);
      const firstBg = nonTransparent[0] || null;

      // Pixel check via PNG average (screenshot buffers compared byte-wise for
      // equality plus a decoded average through the browser).
      const avg = async (buf) => {
        const b64 = buf.toString('base64');
        return page.evaluate(async (data) => {
          const img = new Image();
          img.src = 'data:image/png;base64,' + data;
          await img.decode();
          const c = document.createElement('canvas');
          c.width = 20;
          c.height = 20;
          const g = c.getContext('2d');
          g.drawImage(img, 0, 0, 20, 20);
          const d = g.getImageData(0, 0, 20, 20).data;
          let r = 0, gg = 0, b = 0;
          for (let i = 0; i < d.length; i += 4) {
            r += d[i];
            gg += d[i + 1];
            b += d[i + 2];
          }
          const n = d.length / 4;
          return [Math.round(r / n), Math.round(gg / n), Math.round(b / n)];
        }, b64);
      };
      const avg60 = await avg(shot60);
      const avg1500 = await avg(shot1500);
      const isDarkPix = (a) => a[0] + a[1] + a[2] < 300; // luminance-ish threshold

      const entry = {
        atDomContentLoaded: atDcl,
        afterSettle: after,
        firstObservedThemes: firstThemes,
        firstNonTransparentBodyBg: firstBg,
        bodyBgTimeline: paint.slice(0, 12),
        avgPixel60ms: avg60,
        avgPixel1500ms: avg1500,
        pass:
          atDcl.theme === 'dark' &&
          after.attr === 'dark' &&
          after.pref === 'dark' &&
          darkTheme(firstThemes[0]) &&
          isDarkPix(avg60) &&
          isDarkPix(avg1500),
      };
      if (!entry.pass) {
        entry.failReasons = [];
        if (atDcl.theme !== 'dark') entry.failReasons.push(`data-theme at DCL was ${atDcl.theme}`);
        if (!darkTheme(firstThemes[0]))
          entry.failReasons.push(`first observed data-theme was ${firstThemes[0]}`);
        if (!isDarkPix(avg60)) entry.failReasons.push(`60ms screenshot not dark: rgb(${avg60})`);
        if (!isDarkPix(avg1500)) entry.failReasons.push(`1500ms screenshot not dark: rgb(${avg1500})`);
      }
      check.pages[p] = entry;
      await page.close();
    }
    check.pass = PAGES.every((p) => check.pages[p].pass);
    report.checks.persistenceNoFouc = check;
    await ctx.close();
  }

  // ======================================================= check 4: system preference
  {
    const check = { name: '4. system preference (prefers-color-scheme)', cases: {} };
    for (const scheme of ['dark', 'light']) {
      const ctx = await browser.newContext({ viewport: VIEWPORT, colorScheme: scheme });
      check.cases[scheme] = {};
      for (const p of PAGES) {
        const page = await ctx.newPage();
        attachListeners(page, `check4:${scheme}:${p}`);
        // Empty localStorage + default preference 'system'.
        await page.addInitScript(() => {
          try {
            localStorage.removeItem('themePreference');
          } catch (e) {}
        });
        await gotoAndSettle(page, base + p);
        const theme = await readTheme(page);
        check.cases[scheme][p] = {
          dataTheme: theme.attr,
          preference: theme.pref,
          pass: theme.attr === scheme,
        };
        await page.close();
      }
      await ctx.close();
    }
    check.pass = Object.values(check.cases).every((pg) =>
      Object.values(pg).every((e) => e.pass)
    );
    report.checks.systemPreference = check;
  }

  // ======================================================= checks 5,6,7,8: dark-mode audit + shots
  {
    const ctx = await browser.newContext({ viewport: VIEWPORT, colorScheme: 'dark' });
    const check5 = { name: '5. dark-mode contrast audit', pages: {} };
    const check6 = { name: '6. no near-white painted surfaces in dark', pages: {} };
    const check7 = { name: '7. header / category-nav not translucent white', pages: {} };
    const check8 = { name: '8. color-scheme: dark', pages: {} };
    const check9 = { name: '9. screenshots (3 pages x 2 themes)', files: [] };
    const check10 = { name: '10. light-mode not regressed', pages: {}, defectSites: {} };
    const check11 = { name: '11. theme icon (sun in light, moon in dark)', icons: {} };

    for (const p of PAGES) {
      const page = await ctx.newPage();
      attachListeners(page, `dark:${p}`);
      // Dark preference + auth token seeded before scripts run.
      await page.addInitScript(() => {
        try {
          localStorage.setItem('themePreference', 'dark');
          localStorage.setItem('arxiv_auth_token', 'DISABLED_NO_PASSWORD_SET_IN_SECRETS');
          localStorage.setItem('arxiv_auth_expire', String(Date.now() + 7 * 24 * 3600 * 1000));
        } catch (e) {}
      });
      await gotoAndSettle(page, base + p);

      const theme = await readTheme(page);
      if (theme.attr !== 'dark') {
        report.notes.push(
          `dark-mode audit on ${p}: data-theme was "${theme.attr}" (expected dark) - results below are not a dark-theme measurement`
        );
      }

      // --- synthetic fixture so CSS is exercised even with no data/CDN
      let syntheticCards = 0;
      try {
        syntheticCards = await page.evaluate(INJECT_FIXTURE_FN);
      } catch (e) {
        report.notes.push(`fixture injection failed on ${p}: ${e.message}`);
      }
      await sleep(300);

      const collected = await page.evaluate(COLLECT_FN, {
        nearWhite: Array.from(NEAR_WHITE),
        minArea: WHITE_AREA_THRESHOLD,
      });

      check5.pages[p] = {
        dataTheme: collected.dataTheme,
        inspected: collected.inspected,
        syntheticCards,
        offenders: collected.textIssues,
        count: collected.textIssues.length,
        invisibleBothSameSide: (collected.invisibleIssues || []).filter(
          (i) => i.bothDark || i.bothLight
        ),
        invisibleCount: (collected.invisibleIssues || []).length,
        pass: collected.textIssues.length === 0 && collected.dataTheme === 'dark',
      };
      report.contrastOffenders.push(
        ...collected.textIssues.map((o) => ({ page: p, theme: 'dark', ...o }))
      );
      report.invisibleTextOffenders.push(
        ...(collected.invisibleIssues || []).map((o) => ({ page: p, theme: 'dark', ...o }))
      );

      check6.pages[p] = {
        offenders: collected.whiteIssues,
        count: collected.whiteIssues.length,
        pass: collected.whiteIssues.length === 0,
      };
      report.whiteSurfaceOffenders.push(
        ...collected.whiteIssues.map((o) => ({ page: p, theme: 'dark', ...o }))
      );

      // --- check 7: sticky bars composited over the page background
      const bars = await page.evaluate(() => {
        function parse(str) {
          const m = String(str || '').match(/rgba?\(([^)]+)\)/);
          if (!m) return null;
          const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
          return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
        }
        function rel(c) {
          const f = (v) => {
            const s = v / 255;
            return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
          };
          return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
        }
        const out = [];
        // Same trap as the contrast walk: <html> is transparent, so pick the
        // first root candidate that actually paints before compositing.
        const htmlBg = parse(getComputedStyle(document.documentElement).backgroundColor);
        const bodyBg = parse(getComputedStyle(document.body).backgroundColor);
        const rootBg =
          htmlBg && htmlBg.a > 0.02 ? htmlBg : bodyBg && bodyBg.a > 0.02 ? bodyBg : { r: 255, g: 255, b: 255, a: 1 };
        for (const sel of ['header', '.category-nav', '.header-content']) {
          const el = document.querySelector(sel);
          if (!el) continue;
          const cs = getComputedStyle(el);
          const bg = parse(cs.backgroundColor);
          let comp = bg;
          if (bg && bg.a < 1) {
            comp = {
              r: bg.r * bg.a + rootBg.r * (1 - bg.a),
              g: bg.g * bg.a + rootBg.g * (1 - bg.a),
              b: bg.b * bg.a + rootBg.b * (1 - bg.a),
            };
          }
          out.push({
            selector: sel,
            rawBackground: cs.backgroundColor,
            canvasBodyBg: getComputedStyle(document.body).backgroundColor,
            canvasRootBg: `rgb(${Math.round(rootBg.r)}, ${Math.round(rootBg.g)}, ${Math.round(rootBg.b)})`,
            compositedOverBody: comp
              ? `rgb(${Math.round(comp.r)}, ${Math.round(comp.g)}, ${Math.round(comp.b)})`
              : null,
            compositedIsDark: comp ? rel(comp) < 0.25 : null,
            backdropFilter: cs.backdropFilter || cs.webkitBackdropFilter || 'none',
          });
        }
        return out;
      });
      check7.pages[p] = {
        bars,
        pass: bars.length > 0 && bars.every((b) => b.compositedIsDark === true),
      };

      check8.pages[p] = {
        colorScheme: collected.colorScheme,
        pass: /dark/.test(collected.colorScheme || ''),
      };

      // --- screenshots (dark)
      const fullPage = p === 'index.html';
      const darkFile = await shoot(page, `${p.replace('.html', '')}-dark`, fullPage);
      check9.files.push(darkFile);
      await page.close();
    }

    // --- light screenshots + check 10 (same browser session, separate context)
    const ctxLight = await browser.newContext({ viewport: VIEWPORT, colorScheme: 'light' });
    for (const p of PAGES) {
      const page = await ctxLight.newPage();
      attachListeners(page, `light:${p}`);
      await gotoAndSettle(page, base + p);
      const probe = await page.evaluate(() => {
        function parse(str) {
          const m = String(str || '').match(/rgba?\(([^)]+)\)/);
          if (!m) return null;
          const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
          return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
        }
        const bodyBg = parse(getComputedStyle(document.body).backgroundColor) || {
          r: 255, g: 255, b: 255, a: 1,
        };
        const header = document.querySelector('header');
        const hBg = header ? parse(getComputedStyle(header).backgroundColor) : null;
        const lum = (c) => (c.r + c.g + c.b) / 3;
        return {
          theme: document.documentElement.getAttribute('data-theme'),
          bodyBgRaw: getComputedStyle(document.body).backgroundColor,
          bodyBgIsLight: lum(bodyBg) > 180,
          headerBgRaw: hBg ? getComputedStyle(header).backgroundColor : null,
          headerIsTranslucentWhite: !!(
            hBg && hBg.a < 0.999 && hBg.r > 240 && hBg.g > 240 && hBg.b > 240
          ),
          colorScheme: getComputedStyle(document.documentElement).colorScheme,
        };
      });
      const file = await shoot(page, `${p.replace('.html', '')}-light`, p === 'index.html');
      check9.files.push(file);
      check10.pages[p] = { ...probe, pass: probe.bodyBgIsLight };

      // --- check 10b: LIGHT-mode regression on the exact task-4 defect sites.
      // task-4 claims its edits are light-neutral; prove it instead of assuming.
      const lightSites = await page.evaluate(DEFECT_SITE_FN, {
        sites: DEFECT_SITES,
        lightMode: true,
      });
      check10.defectSites = check10.defectSites || {};
      check10.defectSites[p] = lightSites;

      // --- check 11: theme icon visibility (sun in light, moon in dark)
      const lightIcon = await page.evaluate(THEME_ICON_FN);
      check11.icons = check11.icons || {};
      check11.icons[p] = { light: { ...lightIcon, expected: 'sun', pass: lightIcon.pass }, dark: null };

      await page.close();
    }
    await ctxLight.close();

    // --- dark-side defect-site + theme-icon probe (real DOM, no fixture)
    {
      const ctxDark2 = await browser.newContext({ viewport: VIEWPORT, colorScheme: 'dark' });
      for (const p of PAGES) {
        const page = await ctxDark2.newPage();
        attachListeners(page, `sites-dark:${p}`);
        await page.addInitScript(() => {
          try {
            localStorage.setItem('themePreference', 'dark');
          } catch (e) {}
        });
        await gotoAndSettle(page, base + p);
        const sites = await page.evaluate(DEFECT_SITE_FN, {
          sites: DEFECT_SITES,
          lightMode: false,
        });
        check5.defectSites = check5.defectSites || {};
        check5.defectSites[p] = sites;
        const icons = await page.evaluate(THEME_ICON_FN);
        check11.icons = check11.icons || {};
        check11.icons[p] = {
          light: (check11.icons[p] && check11.icons[p].light) || null,
          dark: { ...icons, expected: 'moon', pass: icons.pass },
        };
        await page.close();
      }
      await ctxDark2.close();
    }

    // screenshot dimensions
    check9.note = 'index.html captured fullPage; other pages viewport-only';

    // Aggregate the per-page verdicts into a top-level .pass for every check;
    // a missing .pass silently reads as FAIL in the summary table.
    check5.pass = PAGES.every((p) => check5.pages[p] && check5.pages[p].pass);
    check6.pass = PAGES.every((p) => check6.pages[p] && check6.pages[p].pass);
    check7.pass = PAGES.every((p) => check7.pages[p] && check7.pages[p].pass);
    check8.pass = PAGES.every((p) => check8.pages[p] && check8.pages[p].pass);
    check9.pass = check9.files.length === 6;
    check10.pass =
      Object.values(check10.pages).every((e) => e.pass) &&
      Object.values(check10.pages).every((e) => e.headerIsTranslucentWhite) &&
      // task-4 claims light-neutrality: prove the four defect sites did not
      // regress the light theme.
      Object.values(check10.defectSites || {}).every((s) =>
        (s.sites || []).every((x) => x.pass)
      );
    check11.pass = Object.values(check11.icons).every(
      (i) => i.light && i.light.pass && i.dark && i.dark.pass
    );
    report.checks.contrastDark = check5;
    report.checks.whiteSurfaces = check6;
    report.checks.stickyBars = check7;
    report.checks.colorScheme = check8;
    report.checks.screenshots = check9;
    report.checks.lightRegression = check10;
    report.checks.themeIcon = check11;
    report.screenshots = check9.files;

    await ctx.close();
  }

  } catch (e) {
    report.fatal = `runner threw: ${e && e.stack ? e.stack : e}`;
  } finally {
    await browser.close().catch(() => {});
    await new Promise((r) => server.close(r));
  }

  report.environment.consoleErrors = consoleErrors;
  report.environment.pageErrors = pageErrors;

  // ---- environment limitations -------------------------------------------
  // Classify network noise so no environment failure is mistaken for a theme
  // defect. api.github.com (star/fork badges in the header) returns 403 from
  // this sandbox: an unauthenticated, rate-limited API call, not a bug.
  const ENV_HOSTS = [
    { pattern: /api\.github\.com/, why: 'GitHub API star/fork badges are unauthenticated and rate-limited here (403); header badge numbers stay "-"' },
    { pattern: /raw\.githubusercontent\.com|githubusercontent/, why: 'data branch (paper JSONL) may be unreachable' },
    { pattern: /d3js\.org|cdn\.jsdelivr\.net|unpkg\.com|fonts\.googleapis\.com|cdnjs/, why: 'third-party CDN asset' },
  ];
  const allConsole = Object.entries(consoleErrors).flatMap(([where, list]) =>
    list.map((msg) => ({ where, msg }))
  );
  // Network-layer failures carry the URL, which is what lets us classify them.
  report.environment.networkFailures = dedupeBy(networkFailures, (x) => `${x.status}|${x.url}`);
  report.environment.environmentLimitations = [];
  for (const host of ENV_HOSTS) {
    const hits = report.environment.networkFailures.filter((x) => host.pattern.test(x.url));
    if (hits.length) {
      report.environment.environmentLimitations.push({
        host: String(host.pattern).replace(/\\|\.\*|\//g, ''),
        why: host.why,
        occurrences: hits.length,
        sampleUrls: Array.from(new Set(hits.map((h) => h.url))).slice(0, 3),
        statuses: Array.from(new Set(hits.map((h) => String(h.status)))),
      });
    }
  }
  report.environment.unexplainedNetworkErrors = report.environment.networkFailures.filter(
    (x) => !ENV_HOSTS.some((h) => h.pattern.test(x.url))
  );
  if (report.environment.unexplainedNetworkErrors.length) {
    report.notes.push(
      `${report.environment.unexplainedNetworkErrors.length} unexplained network error(s) - inspect environment.unexplainedNetworkErrors; NOT counted as theme defects.`
    );
  }
  report.notes.push(
    'Network/CDN/API failures above are environment limitations, not theme defects.'
  );
  report.environment.consoleErrorMessagesOnly = allConsole.length;
  report.environment.cdnHostsObserved = [
    'd3js.org',
    'cdn.jsdelivr.net',
    'unpkg.com',
    'fonts.googleapis.com',
    'api.github.com',
  ];

  // ---- derive per-check pass/fail + defects ------------------------------
  buildDefects(report);

  finishAndWrite(report);
  printSummary(report);
  return report.summary.allPass ? 0 : 1;
}

function buildDefects(report) {
  const c = report.checks;
  const defects = [];

  const push = (d) => defects.push(d);
  if (c.contrastDark) {
    for (const o of c.contrastDark.pages
      ? Object.entries(c.contrastDark.pages).flatMap(([pg, v]) =>
          (v.offenders || []).map((x) => ({ page: pg, ...x }))
        )
      : []) {
      push({
        severity: o.sameSideInvisible ? 'P0' : o.ratio < 2 ? 'P1' : 'P2',
        kind: 'contrast',
        page: o.page,
        theme: 'dark',
        selector: o.selector,
        expected: `contrast ratio >= ${o.threshold} (${o.isLarge ? 'large' : 'normal'} text)`,
        actual: `ratio ${o.ratio} (color ${o.color} on ${o.background}, bg from ${o.bgFrom})`,
        repro: `dark mode -> ${o.page} -> inspect ${o.selector} (text "${o.text}")`,
      });
    }
    for (const o of report.invisibleTextOffenders || []) {
      push({
        severity: 'P0',
        kind: o.bothDark ? 'invisible-text-both-dark' : 'invisible-text-both-light',
        page: o.page,
        theme: 'dark',
        selector: o.selector,
        expected: 'text colour must differ from its effective background',
        actual: `ratio ${o.ratio}: foreground ${o.color} and background ${o.background} are BOTH ${o.bothDark ? 'dark' : 'light'} (bg from ${o.bgFrom})`,
        repro: `dark mode -> ${o.page} -> inspect ${o.selector} (text "${o.text}")`,
      });
    }
  }
  if (c.whiteSurfaces) {
    for (const [pg, v] of Object.entries(c.whiteSurfaces.pages || {})) {
      for (const o of v.offenders || []) {
        push({
          severity: o.area > 20000 ? 'P1' : 'P2',
          kind: 'white-surface',
          page: pg,
          theme: 'dark',
          selector: o.selector,
          expected: 'no near-white surface in dark mode',
          actual: `${o.backgroundColor} painted over ${o.area}px^2`,
          repro: `dark mode -> ${pg} -> inspect ${o.selector}`,
        });
      }
    }
  }
  if (c.stickyBars) {
    for (const [pg, v] of Object.entries(c.stickyBars.pages || {})) {
      for (const b of v.bars || []) {
        if (b.compositedIsDark === false) {
          push({
            severity: 'P1',
            kind: 'translucent-light-bar',
            page: pg,
            theme: 'dark',
            selector: b.selector,
            expected: 'sticky bar composites to a dark colour over the dark page',
            actual: `${b.rawBackground} over body ${b.canvasBodyBg} = ${b.compositedOverBody}`,
            repro: `dark mode -> ${pg} -> inspect computed background of ${b.selector}`,
          });
        }
      }
    }
  }
  if (c.bootLight && !c.bootLight.pass) {
    push({
      severity: 'P1',
      kind: 'boot',
      page: PAGES.join(', '),
      theme: 'n/a',
      selector: 'html[data-theme]',
      expected: 'light by default with an empty localStorage',
      actual: JSON.stringify(
        Object.fromEntries(Object.entries(c.bootLight.pages).map(([k, v]) => [k, v.dataTheme]))
      ),
      repro: 'node tools/qa/theme-qa.js',
    });
  }
  if (c.toggle && !c.toggle.pass) {
    push({
      severity: 'P1',
      kind: 'toggle',
      page: PAGES.join(', '),
      theme: 'n/a',
      selector: '[data-theme-toggle]',
      expected: 'click flips data-theme + localStorage.themePreference, aria-pressed follows',
      actual: JSON.stringify(
        Object.fromEntries(
          Object.entries(c.toggle.pages).map(([k, v]) => [
            k,
            { pass: v.pass, reason: v.reason, afterFirstClick: v.afterFirstClick },
          ])
        )
      ).slice(0, 1200),
      repro: 'node tools/qa/theme-qa.js',
    });
  }
  if (c.persistenceNoFouc && !c.persistenceNoFouc.pass) {
    for (const [pg, v] of Object.entries(c.persistenceNoFouc.pages)) {
      if (v.pass) continue;
      push({
        severity: 'P1',
        kind: 'fouc',
        page: pg,
        theme: 'dark',
        selector: 'html[data-theme]',
        expected: 'dark at first paint, no light flash',
        actual: (v.failReasons || []).join('; ') || JSON.stringify(v).slice(0, 300),
        repro: `set localStorage.themePreference=dark, reload ${pg}, watch the first frames`,
      });
    }
  }
  if (c.systemPreference && !c.systemPreference.pass) {
    push({
      severity: 'P1',
      kind: 'system-preference',
      page: PAGES.join(', '),
      theme: 'n/a',
      selector: 'html[data-theme]',
      expected: 'prefers-color-scheme:dark -> dark, prefers-color-scheme:light -> light',
      actual: JSON.stringify(c.systemPreference.cases).slice(0, 800),
      repro: 'launch a context with colorScheme dark/light and an empty localStorage',
    });
  }
  if (c.colorScheme && !c.colorScheme.pass) {
    push({
      severity: 'P2',
      kind: 'color-scheme',
      page: PAGES.join(', '),
      theme: 'dark',
      selector: 'html',
      expected: "getComputedStyle(document.documentElement).colorScheme includes 'dark'",
      actual: JSON.stringify(
        Object.fromEntries(Object.entries(c.colorScheme.pages).map(([k, v]) => [k, v.colorScheme]))
      ),
      repro: 'dark mode -> evaluate getComputedStyle(document.documentElement).colorScheme',
    });
  }
  if (c.lightRegression && !c.lightRegression.pass) {
    push({
      severity: 'P1',
      kind: 'light-regression',
      page: PAGES.join(', '),
      theme: 'light',
      selector: 'body / header',
      expected: 'light body background and translucent white header',
      actual: JSON.stringify(c.lightRegression.pages).slice(0, 800),
      repro: 'light mode -> inspect body background and header background',
    });
  }

  report.defects = defects;

  // ---- defect-site before/after table ------------------------------------
  // "before" numbers were measured on the pre-task-4 revision and are kept in
  // tools/qa/defect-baseline.json; if that file is absent we still emit the
  // "after" column so the table is never misleading.
  const baselinePath = path.join(OUT_DIR, 'defect-baseline.json');
  let baseline = null;
  try {
    if (fs.existsSync(baselinePath)) baseline = JSON.parse(fs.readFileSync(baselinePath, 'utf8'));
  } catch (e) {
    baseline = null;
  }
  const siteRows = [];
  const darkPages = (report.checks.contrastDark && report.checks.contrastDark.defectSites) || {};
  const lightPages = (report.checks.lightRegression && report.checks.lightRegression.defectSites) || {};
  for (const [pg, res] of Object.entries(darkPages)) {
    for (const s of res.sites) {
      const key = `${s.id}|${s.selector}`;
      const lightRes = ((lightPages[pg] || {}).sites || []).find((x) => x.id === s.id);
      siteRows.push({
        id: s.id,
        page: s.page,
        selector: s.selector,
        what: s.what,
        cssFile: s.cssFile,
        dark: {
          ratio: s.ratio,
          color: s.color,
          background: s.effectiveBackground,
          ownPaintedBackground: s.ownPaintedBackground,
          present: s.present,
          pass: s.pass,
          verdicts: s.verdicts,
          reason: s.reason,
        },
        light: lightRes
          ? {
              ratio: lightRes.ratio,
              color: lightRes.color,
              background: lightRes.effectiveBackground,
              ownPaintedBackground: lightRes.ownPaintedBackground,
              present: lightRes.present,
              pass: lightRes.pass,
              verdicts: lightRes.verdicts,
              reason: lightRes.reason,
            }
          : null,
        before: baseline && baseline.sites ? baseline.sites[key] || null : null,
      });
    }
  }
  report.defectSiteTable = {
    baselineFile: fs.existsSync(baselinePath) ? path.relative(REPO_ROOT, baselinePath) : null,
    baselinePresent: !!baseline,
    rows: siteRows,
  };

  // ---- summary table ------------------------------------------------------
  const rows = [
    ['1. boot light + no JS errors', report.checks.bootLight],
    ['2. toggle flips theme', report.checks.toggle],
    ['3. persistence + no FOUC', report.checks.persistenceNoFouc],
    ['4. system preference', report.checks.systemPreference],
    ['5. dark contrast', report.checks.contrastDark],
    ['6. no near-white surfaces (dark)', report.checks.whiteSurfaces],
    ['7. sticky bars dark (dark)', report.checks.stickyBars],
    ['8. color-scheme dark', report.checks.colorScheme],
    ['9. screenshots 3x2', report.checks.screenshots],
    ['10. light mode not regressed', report.checks.lightRegression],
    ['11. theme icon (sun/moon)', report.checks.themeIcon],
  ];
  report.summary = {
    rows: rows.map(([name, v]) => ({
      check: name,
      status: v ? (v.pass ? 'PASS' : 'FAIL') : 'SKIPPED',
      detail: summarizeCheck(v),
    })),
    allPass: rows.every(([, v]) => v && v.pass),
    preconditionFailures: report.notes.filter((n) => n.startsWith('PRECONDITION MISSING')),
    defectCount: defects.length,
    p0: defects.filter((d) => d.severity === 'P0').length,
    p1: defects.filter((d) => d.severity === 'P1').length,
    p2: defects.filter((d) => d.severity === 'P2').length,
  };
  report.finishedAt = nowIso();
}

function summarizeCheck(v) {
  if (!v) return 'not run';
  if (v.icons) {
    return Object.entries(v.icons)
      .map(([k, e]) => `${k}:${e.light ? e.light.expected + '/ok' : 'light?n/a'}+${e.dark ? e.dark.expected + '/ok' : 'dark?n/a'}`)
      .join(' ');
  }
  if (v.pages) {
    return Object.entries(v.pages)
      .map(([k, e]) => `${k}:${e.pass ? 'ok' : 'bad'}`)
      .join(' ');
  }
  if (v.cases) return JSON.stringify(v.cases).slice(0, 120);
  if (v.files) return `${v.files.length} png`;
  return '';
}

function finishAndWrite(report) {
  if (!report.finishedAt) report.finishedAt = nowIso();
  try {
    fs.writeFileSync(REPORT_JSON, JSON.stringify(report, null, 2));
  } catch (e) {
    console.error('failed to write report:', e.message);
  }
}

function printSummary(report) {
  console.log('\n================ THEME QA SUMMARY ================');
  console.log('revision :', report.revision);
  console.log('url      :', report.url);
  console.log('finished :', report.finishedAt);
  console.log('');
  const rows = report.summary ? report.summary.rows : [];
  for (const r of rows) {
    console.log(`${r.status.padEnd(8)} ${r.check}${r.detail ? '   [' + r.detail + ']' : ''}`);
  }
  console.log('');
  if (report.summary) {
    console.log(
      `defects: ${report.summary.defectCount} (P0 ${report.summary.p0}, P1 ${report.summary.p1}, P2 ${report.summary.p2})`
    );
  }
  const off = report.contrastOffenders || [];
  console.log(`\ncontrast offenders: ${off.length}`);
  for (const o of off.slice(0, 40)) {
    console.log(
      `  [${o.page}] ${o.selector} :: ratio ${o.ratio} < ${o.threshold} :: ${o.color} on ${o.background} (bg from ${o.bgFrom}) :: "${o.text}"`
    );
  }
  const white = report.whiteSurfaceOffenders || [];
  console.log(`\nnear-white surfaces (dark): ${white.length}`);
  for (const o of white.slice(0, 20)) {
    console.log(`  [${o.page}] ${o.selector} :: ${o.backgroundColor} :: ${o.area}px^2`);
  }
  const invis = report.invisibleTextOffenders || [];
  console.log(`\ninvisible text (color/background within 1.3:1): ${invis.length}`);
  for (const o of invis.slice(0, 20)) {
    console.log(
      `  [${o.page}] ${o.selector} :: ratio ${o.ratio} :: BOTH ${o.bothDark ? 'DARK' : 'LIGHT'} :: ${o.color} on ${o.background} :: "${o.text}"`
    );
  }

  const dst = report.defectSiteTable;
  if (dst && dst.rows.length) {
    console.log('\n---- defect-site before/after (real DOM) ----');
    for (const r of dst.rows) {
      const before = r.before ? `${r.before.ratio}:1` : 'n/a';
      const light = r.light ? `${r.light.ratio}:1 ${r.light.pass ? 'PASS' : 'FAIL'}` : 'n/a';
      console.log(
        `  ${r.id.padEnd(4)} ${r.page.padEnd(15)} ${r.selector.slice(0, 34).padEnd(34)}` +
          ` before=${before.padEnd(9)} dark=${String(r.dark.ratio + ':1').padEnd(9)}${r.dark.pass ? 'PASS' : 'FAIL'}` +
          ` light=${light.padEnd(14)} bg=${r.dark.ownPaintedBackground || r.dark.background}`
      );
    }
    if (!dst.baselinePresent) {
      console.log('  (note: no baseline file, "before" column unavailable for this run)');
    }
  }

  const icons = (report.checks.themeIcon && report.checks.themeIcon.icons) || {};
  if (Object.keys(icons).length) {
    console.log('\n---- theme icon visibility (exactly one glyph per theme) ----');
    for (const [pg, e] of Object.entries(icons)) {
      const f = (x) =>
        x
          ? `${x.expected} visible, other hidden (sun ${x.sunVisible}/${x.sunCount}, moon ${x.moonVisible}/${x.moonCount})`
          : 'not measured';
      console.log(`  ${pg.padEnd(15)} light: ${f(e.light)}`);
      console.log(`  ${''.padEnd(15)} dark : ${f(e.dark)}`);
    }
  }
  if (report.fatal) console.log('\nFATAL:', report.fatal);
  if (report.notes && report.notes.length) {
    console.log('\nnotes:');
    for (const n of report.notes) console.log('  -', n);
  }
  console.log(`\nreport: ${path.relative(process.cwd(), REPORT_JSON)}`);
  console.log('=================================================\n');
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    console.error('unhandled:', e);
    process.exit(1);
  });
