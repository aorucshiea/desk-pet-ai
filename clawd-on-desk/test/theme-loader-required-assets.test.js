"use strict";

// Regression test: the doctor's theme check crashed with
// TypeError ERR_INVALID_ARG_TYPE ("path" argument must be of type string,
// received an instance of Object) whenever the active theme carried
// emotion-tagged variant entries ({file, emotion}) — collectRequiredAssetFiles
// intentionally yields those objects, but the consumer passed them straight
// into path.basename.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");

const themeLoader = require("../src/theme-loader");
themeLoader.init(path.join(__dirname, "..", "src"));
const themeSchema = require("../src/theme-schema");

test("collectRequiredAssetFiles baselines variant objects to {file, emotion}", () => {
  const theme = {
    states: {
      attention: [
        "attention.svg",
        { file: "icons/nested/attention-happy.svg", emotion: "happy" },
      ],
    },
  };
  const files = themeSchema.collectRequiredAssetFiles(theme);
  const arr = [...files];
  assert.ok(arr.includes("attention.svg"));
  const variant = arr.find((f) => typeof f === "object");
  assert.ok(variant, "variant entry must pass through as an object");
  assert.equal(variant.file, "attention-happy.svg");
  assert.equal(variant.emotion, "happy");
});

test("_validateRequiredAssets accepts variant objects without throwing", () => {
  const theme = themeLoader.loadTheme("cloudling");
  assert.ok(theme, "cloudling theme must load");
  const errors = themeLoader._validateRequiredAssets(theme);
  assert.ok(Array.isArray(errors));
  // Real theme assets exist — no errors expected, and crucially no throw.
  assert.deepEqual(errors, []);
});
