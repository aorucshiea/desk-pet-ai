"use strict";
// Settings sidebar icons — inline SVG strings keyed by tab id.
//
// All icons use a 24x24 viewBox, `stroke="currentColor"`, fill="none",
// stroke-width 1.5, so they inherit the sidebar text color (works in
// both light and dark mode) and visually match each other.
//
// Why inline SVG and not <img src="...">?
//  - The settings renderer escapes / innerHTMLs the icon string in
//    place; inline SVG side-steps any path resolution (asar / file://)
//    that bites packaged builds.
//  - Each icon is small (~200-500 bytes); the whole file weighs <5 KB.

const ICONS = {
  // ⚙ — gear
  general:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/>' +
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>' +
    '</svg>',

  // Models tab — generic wireframe cube (the previous icon was the
  // MiniCPM vendor brand mark, removed along with the vendor branding).
  // 24x24, stroke-only, same weight as the rest of the set.
  minicpm:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M12 2.9 3.4 7.4v9.2L12 21.1l8.6-4.5V7.4L12 2.9Z"/>' +
    '<path d="M12 12.1 3.4 7.4M12 12.1l8.6-4.7M12 12.1v9"/>' +
    '</svg>',

  // 📖 — book (skills)
  skills:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>' +
    '<path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>' +
    '<path d="M12 6v7M9 9h6"/>' +
    '</svg>',

  // ⚡ — bolt
  agents:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M13 2 3 14h7l-1 8 10-12h-7l1-8Z"/>' +
    '</svg>',

  // 🎨 — palette
  theme:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M12 22a10 10 0 1 1 10-10c0 2.5-2 4-4 4h-2a2 2 0 0 0-1 3.7A2 2 0 0 1 12 22Z"/>' +
    '<circle cx="7.5" cy="10.5" r="1" fill="currentColor"/>' +
    '<circle cx="12" cy="7.5" r="1" fill="currentColor"/>' +
    '<circle cx="16.5" cy="10.5" r="1" fill="currentColor"/>' +
    '</svg>',

  // 🎬 — clapperboard
  animMap:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<rect x="2" y="7" width="20" height="13" rx="2"/>' +
    '<path d="m4 7 3-4M10 7l3-4M16 7l3-4"/>' +
    '</svg>',

  // 🎞 — film strip
  animOverrides:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<rect x="3" y="4" width="18" height="16" rx="2"/>' +
    '<path d="M7 4v16M17 4v16"/>' +
    '<path d="M3 9h4M17 9h4M3 15h4M17 15h4"/>' +
    '</svg>',

  // ⌨ — keyboard
  shortcuts:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<rect x="2" y="6" width="20" height="12" rx="2"/>' +
    '<path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h12"/>' +
    '</svg>',

  "telegram-approval":
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M21 4 3.8 11.2c-.8.3-.8 1.5.1 1.8l4.4 1.5 1.8 5c.3.8 1.4.9 1.8.2L21 4Z"/>' +
    '<path d="m8.3 14.5 5.2-4.2"/>' +
    '</svg>',

  // 🔌 — plug
  "remote-ssh":
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M9 2v6M15 2v6"/>' +
    '<path d="M7 8h10v3a5 5 0 0 1-5 5 5 5 0 0 1-5-5V8Z"/>' +
    '<path d="M12 16v6"/>' +
    '</svg>',

  mobile:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<rect x="7" y="2.5" width="10" height="19" rx="2"/>' +
    '<path d="M10.5 18.5h3"/>' +
    '</svg>',

  // ℹ — info circle
  about:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<circle cx="12" cy="12" r="9"/>' +
    '<path d="M12 11v6M12 7.5v.01"/>' +
    '</svg>',

  // 🖱 — mouse pointer (Screen Click)
  screenclick:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M4 3 5 21 10 16 14 21 16 19 12 14 20 12 4 3Z"/>' +
    '</svg>',

  // 🛠 — wrench-and-screwdriver (placeholder when a tab is missing one)
  placeholder:
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="m14.5 5.5 4 4-9 9-4 .5.5-4 8.5-9.5Z"/>' +
    '<path d="m18.5 5.5-3 3"/>' +
    '</svg>',
};

function getIcon(id) {
  return ICONS[id] || ICONS.placeholder;
}

globalThis.ClawdSettingsIcons = { getIcon, ICONS };
