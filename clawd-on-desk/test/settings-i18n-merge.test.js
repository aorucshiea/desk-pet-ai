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
});
