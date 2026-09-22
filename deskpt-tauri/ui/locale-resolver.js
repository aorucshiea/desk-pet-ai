"use strict";

// ── Locale resolution ──
//
// `prefs.lang` stores the user's *choice* — including the special value
// `"system"` which means "follow the OS locale on every launch". The runtime,
// however, always renders in one of the five concrete UI languages
// (en / zh / zh-TW / ko / ja). This module bridges the two.
//
// `mapOsLocale(raw)`         — normalize an OS locale string ("zh_TW.UTF-8",
//                              "ja-JP", "en-US") to one of the supported langs.
// `resolveEffectiveLang(stored, getOsLocale?)`
//                            — turn a stored value into the effective render
//                              language. `"system"` resolves via getOsLocale.
//                              Unknown stored values fall back to "en".
//
// Pure module: never imports Electron so it works in unit tests, the main
// process, and the renderer (loaded via <script> in settings.html for the
// language picker). Exposes itself on `globalThis.ClawdLocaleResolver` for
// renderer use and as CommonJS exports for Node use.

(function initLocaleResolver(root) {
  const SUPPORTED_LANGS = Object.freeze(["en", "zh", "zh-TW", "ko", "ja"]);
  const STORED_LANGS = Object.freeze(["system", ...SUPPORTED_LANGS]);

  function mapOsLocale(raw) {
    if (typeof raw !== "string" || !raw) return "en";
    const l = raw.toLowerCase().replace(/_/g, "-");
    if (l.startsWith("zh-tw") || l.startsWith("zh-hk") || l.startsWith("zh-mo") || l.startsWith("zh-hant")) {
      return "zh-TW";
    }
    if (l.startsWith("zh")) return "zh";
    if (l.startsWith("ko")) return "ko";
    if (l.startsWith("ja")) return "ja";
    return "en";
  }

  function resolveEffectiveLang(stored, getOsLocale) {
    if (stored === "system") {
      let raw = "en";
      try {
        if (typeof getOsLocale === "function") {
          const v = getOsLocale();
          if (typeof v === "string" && v) raw = v;
        }
      } catch {
        // fall through to "en"
      }
      return mapOsLocale(raw);
    }
    if (SUPPORTED_LANGS.includes(stored)) return stored;
    return "en";
  }

  const api = {
    SUPPORTED_LANGS,
    STORED_LANGS,
    mapOsLocale,
    resolveEffectiveLang,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.ClawdLocaleResolver = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
