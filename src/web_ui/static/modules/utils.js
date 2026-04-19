/**
 * utils.js — Shared Utilities
 *
 * Pure display helpers with no business logic:
 *  • SvgIcon web component
 *  • ANSI → HTML conversion for backend log output
 *  • Log terminal management (append, auto-scroll, debug toggle)
 *  • Number formatting (decimal, power)
 *  • DOM diffing helpers (upsert / cleanup with animations)
 *  • Micro-components reused across cards.js & miner-modal.js
 *  • Toast notifications
 *  • Clipboard
 *
 * Depends on: state.js (state, logTerminal, toastContainer)
 */

/* ── Web Component: SVG Icon ─────────────────────────────────── */
class SvgIcon extends HTMLElement {
  connectedCallback() {
    this.render();
  }
  static get observedAttributes() { return ['name']; }
  attributeChangedCallback() { this.render(); }
  render() {
    const name = this.getAttribute('name');
    if (!name) return;
    this.innerHTML = `<svg viewBox="0 0 24 24" preserveAspectRatio="xMidYMid meet"><use href="/static/icons/sprite.svg#${name}"></use></svg>`;
  }
}
customElements.define('svg-icon', SvgIcon);

/* ── ANSI → HTML ─────────────────────────────────────────────── */
const ANSI_MAP = {
  '30': 'ansi-white', '31': 'ansi-red', '32': 'ansi-green', '33': 'ansi-yellow',
  '34': 'ansi-blue', '35': 'ansi-magenta', '36': 'ansi-cyan', '37': 'ansi-white',
  '90': 'ansi-white', '91': 'ansi-red', '92': 'ansi-green', '93': 'ansi-yellow',
  '94': 'ansi-blue', '95': 'ansi-magenta', '96': 'ansi-cyan', '97': 'ansi-white',
  '1': 'ansi-bold',
};

function ansiToHtml(text) {
  text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  let result = '', classes = [];
  for (const part of text.split(/(\x1b\[[0-9;]*m)/)) {
    const m = part.match(/^\x1b\[([0-9;]*)m$/);
    if (m) {
      const codes = m[1].split(';');
      if (!codes[0] || codes[0] === '0') classes = [];
      else for (const c of codes) { const cls = ANSI_MAP[c]; if (cls && !classes.includes(cls)) classes.push(cls); }
    } else if (part) {
      result += classes.length ? `<span class="${classes.join(' ')}">${part}</span>` : part;
    }
  }
  return result.replace(/(https?:\/\/[^\s<>"]+)/g, url => `<a href="${url}" target="_blank" rel="noopener">${url}</a>`);
}

/* ── Log Terminal ────────────────────────────────────────────── */
let autoScroll = true;

if (logTerminal) {
  logTerminal.classList.add('hide-debug');
  logTerminal.addEventListener('scroll', () => {
    const { scrollTop, scrollHeight, clientHeight } = logTerminal;
    autoScroll = scrollHeight - scrollTop - clientHeight < 40;
  });
}

function stripAnsi(text) {
  return text.replace(/\x1b\[[0-9;]*m/g, '');
}

function appendLog(time, message, level = 'INFO') {
  const entry = document.createElement('div');
  entry.className = `log-entry ${level}`;
  entry.innerHTML = `<span class="log-time">${time || ''}</span><span class="log-text">${ansiToHtml(message)}</span>`;
  logTerminal.appendChild(entry);
  if (logTerminal.children.length > 2000) logTerminal.removeChild(logTerminal.firstChild);
  if (autoScroll) logTerminal.scrollTop = logTerminal.scrollHeight;
}

function toggleDebugLogs() {
  state.showDebug = !state.showDebug;
  const btn = document.getElementById('btn-toggle-debug');
  if (btn) btn.innerText = state.showDebug ? 'Hide DEBUG' : 'Show DEBUG';
  logTerminal.classList.toggle('hide-debug', !state.showDebug);
}

/* ── Number Formatting ───────────────────────────────────────── */
function formatDecimal(value, precision = 4) {
  if (value === null || value === undefined) return "0," + "0".repeat(precision);
  return parseFloat(value).toFixed(precision).replace(".", ",");
}

function displayPower(raw) {
  const w = raw * POWER_UNIT;
  return w >= 1000 ? `${formatDecimal(w / 1000, 1)} kW` : `${w} W`;
}

/* ── DOM Diffing Helpers ─────────────────────────────────────── */

/**
 * Create-or-update an element inside `parent`.
 * Preserves existing DOM nodes where possible to avoid flicker, and adds an `.is-new` class for CSS entry animations.
 */
function upsertElement(parent, id, className, html, tagName = 'div', staggerIndex = 0) {
  let el = document.getElementById(id);
  if (el && !el.classList.contains('removing') && el.parentNode === parent) {
    // Preserve 'is-new' so animations aren't interrupted by state updates
    const finalClass = el.classList.contains('is-new') ? className + ' is-new' : className;
    if (el.className !== finalClass) el.className = finalClass;
    
    const currentStagger = el.style.getPropertyValue('--stagger');
    if (currentStagger !== String(staggerIndex)) {
      el.style.setProperty('--stagger', staggerIndex);
    }

    if (el.innerHTML !== html) {
      const oldHeight = el.offsetHeight;
      const temp = document.createElement(el.tagName || tagName);
      temp.id = id;
      temp.className = finalClass;
      temp.innerHTML = html;
      morphDOM(el, temp);
      
      const newHeight = el.offsetHeight;
      if (oldHeight > 0 && oldHeight !== newHeight && typeof el.animate === 'function') {
        const oldOverflow = el.style.overflow;
        el.style.overflow = 'hidden';
        const anim = el.animate([
          { height: `${oldHeight}px` },
          { height: `${newHeight}px` }
        ], {
          duration: 350,
          easing: 'cubic-bezier(0.4, 0, 0.2, 1)'
        });
        anim.onfinish = () => { el.style.overflow = oldOverflow; };
      }
    }
    
    if (el.parentNode !== parent) {
      parent.appendChild(el);
    }
    return el;
  }
  
  const newEl = document.createElement(tagName);
  newEl.id = id;
  newEl.className = className + ' is-new';
  newEl.style.setProperty('--stagger', staggerIndex);
  newEl.innerHTML = html;
  parent.appendChild(newEl);
  
  // Remove 'is-new' only after the staggered animation has fully completed
  const totalAnimTime = 600 + (staggerIndex * 80);
  setTimeout(() => { if (newEl) newEl.classList.remove('is-new'); }, Math.max(1500, totalAnimTime));
  return newEl;
}

/**
 * Simple DOM diffing to update elements without destroying them. 
 * Allows CSS transitions to play and prevents entrance animations from re-triggering.
 */
function morphDOM(oldNode, newNode) {
  if (!oldNode || !newNode) return;
  if (oldNode.nodeType === Node.TEXT_NODE && newNode.nodeType === Node.TEXT_NODE) {
    if (oldNode.nodeValue !== newNode.nodeValue) oldNode.nodeValue = newNode.nodeValue;
    return;
  }
  if (oldNode.nodeName !== newNode.nodeName || oldNode.nodeType !== newNode.nodeType) {
    if (oldNode.parentNode) oldNode.parentNode.replaceChild(newNode.cloneNode(true), oldNode);
    return;
  }
  if (oldNode.attributes && newNode.attributes) {
    const newAttrNames = new Set();
    for (const attr of newNode.attributes) {
      newAttrNames.add(attr.name);
      let newValue = attr.value;
      
      // Native preservation of tracking classes during deep DOM diffing
      if (attr.name === 'class' && oldNode.classList) {
        const keeps = ['is-new', 'removing'].filter(c => oldNode.classList.contains(c));
        if (keeps.length > 0) {
          const newClasses = (newValue || '').split(' ').filter(Boolean);
          keeps.forEach(c => {
            if (!newClasses.includes(c)) newClasses.push(c);
          });
          newValue = newClasses.join(' ');
        }
      }

      if (oldNode.getAttribute(attr.name) !== newValue) {
        oldNode.setAttribute(attr.name, newValue);
      }
    }
    for (const attr of Array.from(oldNode.attributes)) {
      if (!newAttrNames.has(attr.name)) {
        oldNode.removeAttribute(attr.name);
      }
    }
  }
  const getSignificantChildren = (node) => Array.from(node.childNodes).filter(n => 
    n.nodeType === Node.ELEMENT_NODE || (n.nodeType === Node.TEXT_NODE && n.nodeValue.trim() !== '')
  );

  const oldChildren = getSignificantChildren(oldNode);
  const newChildren = getSignificantChildren(newNode);
  
  // 1. Remove old children that are no longer in new children (by index)
  // Actually, standard positional diffing:
  const max = Math.max(oldChildren.length, newChildren.length);
  for (let i = 0; i < max; i++) {
    const oldChild = oldChildren[i];
    const newChild = newChildren[i];

    if (!oldChild && newChild) {
      const clone = newChild.cloneNode(true);
      if (clone.nodeType === Node.ELEMENT_NODE) {
        clone.classList.add('is-new');
        setTimeout(() => { if (clone) clone.classList.remove('is-new'); }, 1500);
      }
      oldNode.appendChild(clone);
    } else if (oldChild && !newChild) {
      oldNode.removeChild(oldChild);
    } else if (oldChild && newChild) {
      morphDOM(oldChild, newChild);
    }
  }

  // 2. Cleanup ANY remaining nodes that might be insignificant text nodes we didn't filter
  // or that were left behind. This ensures the DOM is clean.
  Array.from(oldNode.childNodes).forEach(n => {
    if (n.nodeType === Node.TEXT_NODE && n.nodeValue.trim() === '') n.remove();
  });
}

/**
 * Remove children of `parent` whose IDs are not in `activeIds`.
 * Applies a `.removing` class first for exit animations.
 */
function cleanupElements(parent, activeIds, delay = 500) {
  const children = Array.from(parent.children).filter(c => c.id);
  children.forEach(child => {
    if (!activeIds.includes(child.id) && !child.classList.contains('removing')) {
      child.classList.add('removing');
      setTimeout(() => { 
        if (child.parentNode === parent && child.classList.contains('removing')) {
          child.remove(); 
        }
      }, delay);
    }
  });
}

/* ── UI Micro-Components ─────────────────────────────────────── */

/** Status pill used inside wallet detail cards. */
function pill(label, value, cls) {
  return `<div class="wdc-amount-pill ${cls}"><span class="pill-label">${label}</span><span class="pill-value">${value}</span></div>`;
}

/** Single transaction row with status icon and explorer link. */
function txRow(label, url, status) {
  const icons = { pending: '<svg-icon name="spin" class="spin-icon-svg svg-size-sm"></svg-icon>', success: '✓', error: '✗' };
  const clsMap = { pending: 'status-running', success: 'status-success', error: 'status-error' };
  const baseUrl = state.config.explorer_url || 'https://snowtrace.io';
  const shortUrl = url ? url.replace(baseUrl + '/tx/', '').slice(0, 10) + '...' + url.slice(-6) : '';
  return `<div class="wdc-tx"><span class="wdc-tx-label">${label}</span><span class="wdc-tx-status ${clsMap[status] || ''}">${icons[status] || '·'}</span>${url ? `<a href="${url}" target="_blank" class="wdc-tx-link"><span class="privacy-data">${shortUrl}</span></a>` : ''}</div>`;
}

/** Human-readable status badge content. */
function statusLabel(s) {
  return {
    idle: '—',
    pending: '<svg-icon name="spin" class="spin-icon-svg svg-size-sm"></svg-icon> Waiting',
    running: '<svg-icon name="spin" class="spin-icon-svg svg-size-sm"></svg-icon> Processing',
    success: '✓ Done',
    'success-cyan': '✓ Received',
    skipped: '⊘ Skipped',
    partial: '⚠ Partial',
    error: '✗ Error'
  }[s] || s || '—';
}

/* ── Toast Notifications ─────────────────────────────────────── */
function showToast(msg, type = 'success') {
  const duration = 10000;
  const t = document.createElement('div');
  t.className = `toast ${type}`;

  const content = document.createElement('div');
  content.className = 'toast-content';
  
  const icons = {
    success: '✓',
    error: '✗',
    warning: '⚠',
    info: 'ℹ'
  };
  const icon = document.createElement('span');
  icon.className = 'toast-icon';
  icon.textContent = icons[type] || icons.info;
  
  const text = document.createElement('span');
  text.className = 'toast-msg';
  text.textContent = msg;

  content.appendChild(icon);
  content.appendChild(text);
  t.appendChild(content);

  const progress = document.createElement('div');
  progress.className = 'toast-progress';
  progress.style.animationDuration = `${duration}ms`;
  t.appendChild(progress);

  toastContainer.appendChild(t);

  let isClosing = false;
  const close = () => {
    if (isClosing) return;
    isClosing = true;
    t.classList.add('closing');
    t.addEventListener('animationend', (e) => {
      if (e.animationName === 'toastSlideOut') {
        t.remove();
      }
    });
    // Fallback in case animation event is missed
    setTimeout(() => { if (t.parentNode) t.remove(); }, 500);
  };

  const timer = setTimeout(close, duration);

  t.addEventListener('click', () => {
    clearTimeout(timer);
    close();
  });
}

/* ── System Alerts (Banners) ────────────────────────────────── */

/** 
 * Registers or updates a system-wide alert banner.
 * @param {string} id Unique identifier for the alert
 * @param {object} opts { type: 'warning'|'error'|'info'|'success', title, message, section: 'inventory'|'global', persistent: bool }
 */
function registerAlert(id, opts) {
  state.alerts[id] = { ...opts, id };
  const section = opts.section || 'global';
  const trayId = section === 'global' ? 'global-alerts-tray' : `alerts-tray-${section}`;
  const tray = document.getElementById(trayId);
  if (tray) {
    tray.classList.remove('closing', 'hidden');
    renderAlertTrays(tray, section);
  }
}

function removeAlert(id) {
  const alert = state.alerts[id];
  if (!alert) return;
  const section = alert.section || 'global';
  const trayId = section === 'global' ? 'global-alerts-tray' : `alerts-tray-${section}`;
  delete state.alerts[id];

  const tray = document.getElementById(trayId);
  if (tray) {
    const remainingInSection = Object.values(state.alerts).filter(a => (a.section || 'global') === section).length;
    if (remainingInSection === 0) {
      tray.classList.add('closing');
      setTimeout(() => {
        // Double check no new alert arrived during animation
        const currentCount = Object.values(state.alerts).filter(a => (a.section || 'global') === section).length;
        if (currentCount === 0) {
          tray.classList.add('hidden');
        }
      }, 400);
    }
    renderAlertTrays(tray, section);
  }
}

/** Renders all alerts belonging to a specific section into a container. */
function renderAlertTrays(container, section = 'global') {
  if (!container) return;
  const activeIds = [];
  const icons = { success: '✓', error: '✗', warning: '⚠', info: 'ℹ' };

  for (const [id, alert] of Object.entries(state.alerts)) {
    if (alert.section !== section) continue;
    activeIds.push(id);
    
    const bannerHtml = `
      <span class="warning-icon">${icons[alert.type] || icons.info}</span>
      <div class="warning-content">
        <div class="warning-text">${alert.title}</div>
        <div class="warning-subtext">${alert.message}</div>
      </div>
    `;
    upsertElement(container, id, `system-alert alert-${alert.type}`, bannerHtml);
  }
  cleanupElements(container, activeIds);
}

/* ── Clipboard ───────────────────────────────────────────────── */
function copyToClipboard(text) {
  if (!text) return;
  let val = text.toString().replace(',', '.');
  if (!isNaN(parseFloat(val)) && isFinite(val)) {
    text = formatDecimal(val, 4);
  }
  navigator.clipboard.writeText(text).then(() => {
    showToast(`Amount copied: ${text}`, 'success');
  }).catch((err) => {
    appendLog('', `\u001b[31m[UI] Clipboard copy failed: ${err.message || err}\u001b[0m`, 'ERROR');
    showToast('Error while copying', 'error');
  });
}

/* ── Window Exports (for HTML onclick handlers) ──────────────── */
window.copyToClipboard = copyToClipboard;
window.toggleDebugLogs = toggleDebugLogs;
