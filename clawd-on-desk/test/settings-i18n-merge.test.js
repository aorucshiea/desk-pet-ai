"use strict";

// settings-i18n.js merges a per-product override table on top of the main
// language tables. The old merge assigned the ENGLISH product table into
// every language first, so any key it repeated rendered in English even
// where a real translation existed (the providers hero and the evolve
// space map shipped half-English in a zh UI). The English table must only
// fill gaps; a language's own value always wins.

const { describe, it } = require("node:test");
const assert = require("node:assert");

require("../src/settings-i18n");

const STRINGS = globalThis.ClawdSettingsI18n.STRINGS;

describe("settings i18n product-table merge", () => {
  it("keeps each language's own translation over the English product table", () => {
    const sentinels = {
      zh: {
        provNowUsing: "现在用它说话",
        provKindBuiltin: "桌宠内置引擎 · 本机运行，可原地更新",
        provNoLocalApi: "还没有——在下面添加 LM Studio 或 Ollama。",
        evolveMapTitle: "插件空间图",
        petEngineStopNow: "停止引擎",
      },
    };
    for (const [lang, expect] of Object.entries(sentinels)) {
      for (const [key, value] of Object.entries(expect)) {
        assert.strictEqual(STRINGS[lang][key], value, `${lang}.${key} was clobbered`);
      }
    }
  });

  it("still gap-fills product keys a language does not define at all", () => {
    // Whatever the English product table introduces must be reachable in
    // every language, even if only as the English fallback.
    for (const key of Object.keys(STRINGS.en)) {
      for (const lang of ["zh", "ko", "ja"]) {
        assert.ok(key in STRINGS[lang], `${lang} is missing ${key}`);
      }
    }
  });

  it("does not leave a translated language showing the English sentence", () => {
    // A translation that survived the merge must differ from English.
    for (const key of ["provNowUsing", "provKindBuiltin", "evolveMapTitle", "petEngineStopNow"]) {
      assert.notStrictEqual(STRINGS.zh[key], STRINGS.en[key], `zh.${key} equals the English value`);
    }
  });

  it("no user-facing string advertises the deleted Dashboard window", () => {
    // The Dashboard was removed end to end, but 通用 → 文字大小 kept saying
    // it scaled "气泡、会话 HUD、Dashboard 和设置页" in all five languages —
    // copy that points at a page which no longer exists is a bug report
    // waiting to happen, so the whole table is policed, not just that row.
    const offenders = [];
    for (const [lang, block] of Object.entries(STRINGS)) {
      for (const [key, value] of Object.entries(block)) {
        if (typeof value !== "string") continue;
        if (/dashboard|ダッシュボード|대시보드/i.test(value)) {
          offenders.push(`${lang}.${key}: ${value.slice(0, 70)}`);
        }
      }
    }
    assert.deepEqual(offenders, [], "stale Dashboard copy:\n  " + offenders.join("\n  "));
  });
});
