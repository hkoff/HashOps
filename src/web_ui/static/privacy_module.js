// src/web_ui/static/privacy_module.js

(function() {
  const originalDataKey = 'data-orig-text';
  window.randomPrivacyEnabled = localStorage.getItem('random_privacy_mode') === 'true';

  window.toggleRandomPrivacy = function() {
    window.randomPrivacyEnabled = !window.randomPrivacyEnabled;
    localStorage.setItem('random_privacy_mode', window.randomPrivacyEnabled);
    applyRandomPrivacyUI();
    updatePrivacyNodes();
  };

  function maskAddress(text) {
    return text.replace(/0x[a-fA-F0-9]{3,}\.\.\.[a-fA-F0-9]{3,}/g, '0x...');
  }

  function applyRandomPrivacyUI() {
    const btn = document.getElementById('btn-toggle-random');
    if (btn) {
       if (window.randomPrivacyEnabled) btn.classList.add('active');
       else btn.classList.remove('active');
    }
  }

  // Utils
  function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
  }

  function getRandomFloat(min, max, decimals = 4) {
    const val = (Math.random() * (max - min)) + min;
    return val.toFixed(decimals).replace('.', ',');
  }

  function generateRandomHex(length) {
    let result = '';
    const chars = '0123456789abcdef';
    for (let i = 0; i < length; i++) {
      result += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return result;
  }

  function isMainWalletContext(element) {
    const appState = typeof state !== 'undefined' ? state : (window.state || null);
    if (!appState || !appState.wallets) return false;
    
    let wName = null;
    let wAddr = null;

    let curr = element;
    while (curr && curr !== document.body) {
      if (curr.classList && curr.classList.contains('wallet-card')) {
        wName = curr.dataset.name;
        break;
      }
      if (curr.id && curr.id.startsWith('mov-body-')) {
        wAddr = curr.id.replace('mov-body-', '');
        break;
      }
      if (curr.id && curr.id.startsWith('mov-header-')) {
        wAddr = curr.id.replace('mov-header-', '');
        break;
      }
      if (curr.classList && curr.classList.contains('miners-ov-wallet')) {
        if (curr.id && curr.id.startsWith('mov-')) {
          wAddr = curr.id.replace('mov-', '');
        }
        break;
      }
      if (curr.classList && curr.classList.contains('wdc-card')) {
        if (curr.id && curr.id.startsWith('card-')) {
          wName = curr.id.replace('card-', '');
        }
        break;
      }
      curr = curr.parentElement;
    }
    
    if (wName) {
      const w = appState.wallets.find(x => x.name === wName);
      if (w) return w.is_main;
    }
    if (wAddr) {
      const w = appState.wallets.find(x => x.address.toLowerCase() === wAddr.toLowerCase());
      if (w) return w.is_main;
    }
    
    return false;
  }

  function getTokenType(element) {
    const textAround = (element.parentElement ? element.parentElement.textContent : '');
    if (textAround.includes('AVAX')) return 'AVAX';
    if (textAround.includes('hCASH') || textAround.includes('💎')) return 'hCASH';
    
    // Also check previous/next siblings explicitly if needed
    if (element.nextSibling && element.nextSibling.textContent.includes('AVAX')) return 'AVAX';
    if (element.nextSibling && element.nextSibling.textContent.includes('hCASH')) return 'hCASH';

    return 'UNKNOWN';
  }

  function scrambleText(element, originalText) {
    let text = originalText;

    // 1. Addresses and Hashes (shortened) - Global pass
    text = text.replace(/0x[a-fA-F0-9]{3,}\.\.\.[a-fA-F0-9]{3,}/g, (match) => {
      const parts = match.split('...');
      return '0x' + generateRandomHex(parts[0].length - 2) + '...' + generateRandomHex(parts[1].length);
    });

    // 2. Full Address / Hash - Global pass
    text = text.replace(/0x[a-fA-F0-9]{30,66}/g, (match) => {
      return '0x' + generateRandomHex(match.length - 2);
    });

    // 3. Power values (with W or kW) - Global pass
    text = text.replace(/([0-9,.]+)\s*(k?W)/g, (match, val, unit) => {
       const strNum = val.replace(',', '.');
       if (strNum && parseFloat(strNum) > 0) {
          const newVal = getRandomFloat(10, 5000, 1).replace(',0', '');
          return newVal + ' ' + unit;
       }
       return match;
    });

    // 4. IDs or strings containing #1234, potentially with nested tags - Global pass
    text = text.replace(/#((?:\s*<[^>]+>\s*)*)(\d+)/g, (match, tags, id) => {
       return '#' + (tags || '') + getRandomInt(1000, 99999);
    });

    // 5. Pure Numbers (Ints and Floats) - For elements that ONLY contain a number
    const trimmed = text.trim();
    if (/^[0-9]+(,[0-9]+)?$/.test(trimmed)) {
      const val = parseFloat(trimmed.replace(',', '.'));
      const isFloat = trimmed.includes(',');
      const prevText = element.previousSibling ? element.previousSibling.textContent : '';
      const textAround = element.parentElement ? element.parentElement.textContent : '';
      
      if (prevText.endsWith('#') || (element.parentElement && element.parentElement.className && element.parentElement.className.includes('miner-chip'))) {
         return getRandomInt(1000, 99999).toString();
      }
      if (textAround.includes('MH/s')) {
         if (val === 0) return text;
         return isFloat ? getRandomFloat(10, 1000, 2) : getRandomInt(10, 1000).toString();
      }
      const isMain = isMainWalletContext(element);
      const tt = getTokenType(element);
      if (!isMain && val === 0) return text; 
      if (tt === 'AVAX') {
         return isMain ? getRandomFloat(10, 100, 4) : text;
      } else {
         return isMain ? getRandomFloat(1000, 15000, 4) : getRandomFloat(0.1, 999.9, 4);
      }
    }

    return text;
  }

  function updatePrivacyNodes() {
    const modeOn = window.randomPrivacyEnabled;
    const blurOn = (typeof state !== 'undefined' && state.privacyEnabled) || (window.state && window.state.privacyEnabled);
    
    document.querySelectorAll('.privacy-data, .privacy-random').forEach(el => {
      // Avoid modifying nodes that are inside script or style tags
      if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.tagName === 'SELECT') return;

      // Optimization: Skip if an ancestor is already .privacy-data. 
      // The parent's scrambling logic (using innerHTML) will handle this node's content.
      // EXCEPTION: Always process children of SELECT because we don't scramble SELECT itself.
      if (el.parentElement) {
        const closestPrivacy = el.parentElement.closest('.privacy-data');
        if (closestPrivacy && closestPrivacy.tagName !== 'SELECT') return;
      }

      if (!el.hasAttribute(originalDataKey)) {
        el.setAttribute(originalDataKey, el.innerHTML); // Save HTML!
      }
      
      const orig = el.getAttribute(originalDataKey);
      
      // EXCEPTION: Don't randomize data inside mm-sim-row (Facility Simulation)
      const skipRandom = el.closest('.mm-sim-row');
      
      const isRandomOnly = el.classList.contains('privacy-random');
      
      if (modeOn && !skipRandom) {
        if (!el.hasAttribute('data-scrambled')) {
          const scrambled = scrambleText(el, orig);
          if (scrambled !== orig) {
             el.innerHTML = scrambled;
          }
          el.setAttribute('data-scrambled', 'true');
        } else {
          // It's checked as scrambled, but what if app.js mutated HTML directly?
          if (el.innerHTML === orig) {
             const scrambled = scrambleText(el, orig);
             if (scrambled !== orig) {
                el.innerHTML = scrambled;
             }
          }
        }
      } else if (blurOn && isRandomOnly && !skipRandom) {
          // Apply "0x..." masking instead of blur for .privacy-random
          const masked = maskAddress(orig);
          if (el.innerHTML !== masked) {
             el.innerHTML = masked;
          }
          el.setAttribute('data-scrambled', 'true');
      } else {
        if (el.hasAttribute('data-scrambled')) {
          if (el.innerHTML !== orig) {
             el.innerHTML = orig;
          }
          el.removeAttribute('data-scrambled');
        }
      }
    });
  }

  window.updatePrivacyNodes = updatePrivacyNodes;

  const observer = new MutationObserver((mutations) => {
    let contentChanged = false;

    for (const mut of mutations) {
      if (mut.type === 'childList' || mut.type === 'characterData') {
         contentChanged = true;
         break;
      }
    }

    const blurOn = (typeof state !== 'undefined' && state.privacyEnabled) || (window.state && window.state.privacyEnabled);
    if ((window.randomPrivacyEnabled || blurOn) && contentChanged) {
       observer.disconnect();
       updatePrivacyNodes();
       observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    }
  });

  window.addEventListener('DOMContentLoaded', () => {
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    applyRandomPrivacyUI();
    updatePrivacyNodes();
  });

})();
