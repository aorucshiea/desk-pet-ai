"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert");

const {
  SUPPORTED_LANGS,
  STORED_LANGS,
  mapOsLocale,
  resolveEffectiveLang,
} = require("../src/locale-resolver");

describe("locale-resolver", () => {
  it("exposes supported and stored language lists", () => {
    assert.deepStrictEqual(SUPPORTED_LANGS, ["en", "zh", "zh-TW", "ko", "ja"]);
    assert.deepStrictEqual(STORED_LANGS, ["system", "en", "zh", "zh-TW", "ko", "ja"]);
  });

  describe("mapOsLocale", () => {
    const cases = [
      ["en-US", "en"],
      ["en", "en"],
      ["fr-FR", "en"],
      ["zh-CN", "zh"],
      ["zh_CN.UTF-8", "zh"],
      ["zh-Hans", "zh"],
      ["zh-Hans-CN", "zh"],
      ["zh-TW", "zh-TW"],
      ["zh_TW", "zh-TW"],
      ["zh-HK", "zh-TW"],
      ["zh-Hant", "zh-TW"],
      ["zh-Hant-TW", "zh-TW"],
      ["ko", "ko"],
      ["ko-KR", "ko"],
      ["ja", "ja"],
      ["ja_JP.UTF-8", "ja"],
      ["", "en"],
      [null, "en"],
      [undefined, "en"],
    ];
    for (const [input, expected] of cases) {
      it(`maps ${JSON.stringify(input)} → ${expected}`, () => {
        assert.strictEqual(mapOsLocale(input), expected);
      });
    }
  });

  describe("resolveEffectiveLang", () => {
    it("returns the stored lang when it's a concrete UI language", () => {
      assert.strictEqual(resolveEffectiveLang("en"), "en");
      assert.strictEqual(resolveEffectiveLang("zh"), "zh");
      assert.strictEqual(resolveEffectiveLang("zh-TW"), "zh-TW");
      assert.strictEqual(resolveEffectiveLang("ko"), "ko");
      assert.strictEqual(resolveEffectiveLang("ja"), "ja");
    });

    it("falls back to en for unknown stored values", () => {
      assert.strictEqual(resolveEffectiveLang(null), "en");
      assert.strictEqual(resolveEffectiveLang(""), "en");
      assert.strictEqual(resolveEffectiveLang("fr"), "en");
      assert.strictEqual(resolveEffectiveLang("zh-Hans"), "en");
    });

    it("resolves system via getOsLocale", () => {
      assert.strictEqual(resolveEffectiveLang("system", () => "zh-CN"), "zh");
      assert.strictEqual(resolveEffectiveLang("system", () => "zh-TW"), "zh-TW");
      assert.strictEqual(resolveEffectiveLang("system", () => "ja-JP"), "ja");
      assert.strictEqual(resolveEffectiveLang("system", () => "ko"), "ko");
      assert.strictEqual(resolveEffectiveLang("system", () => "en-US"), "en");
      assert.strictEqual(resolveEffectiveLang("system", () => "fr-FR"), "en");
    });

    it("falls back gracefully when getOsLocale throws or is missing", () => {
      assert.strictEqual(resolveEffectiveLang("system"), "en");
      assert.strictEqual(resolveEffectiveLang("system", () => { throw new Error("nope"); }), "en");
      assert.strictEqual(resolveEffectiveLang("system", () => null), "en");
    });
  });
});
