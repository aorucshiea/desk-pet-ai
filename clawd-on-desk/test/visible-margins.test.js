const { describe, it } = require("node:test");
const assert = require("node:assert");
const path = require("path");

const themeLoader = require("../src/theme-loader");
const hitGeometry = require("../src/hit-geometry");
const {
  getThemeMarginBox,
  computeThemeAnchorRect,
  collectThemeEnvelopeFiles,
  computeStableVisibleContentMargins,
  getLooseDragMargins,
  getRestClampMargins,
} = require("../src/visible-margins");

themeLoader.init(path.join(__dirname, "..", "src"));

describe("visible margin envelopes", () => {
  const bounds = { x: 0, y: 0, width: 280, height: 280 };

  it("prefers layout.marginBox over contentBox when present", () => {
    const cloudling = themeLoader.loadTheme("mochi");  // 夹具已换 mochi
    assert.deepStrictEqual(getThemeMarginBox(cloudling), cloudling.layout.marginBox);

    const idleFile = cloudling.states.idle[0];
    const contentRect = hitGeometry.getContentRectScreen(cloudling, bounds, "idle", idleFile, {
      box: cloudling.layout.contentBox,
    });
    const marginRect = hitGeometry.getContentRectScreen(cloudling, bounds, "idle", idleFile, {
      box: cloudling.layout.marginBox,
    });

    assert.ok(marginRect.top < contentRect.top);
    assert.ok(marginRect.bottom >= contentRect.bottom);
  });

  it("collects a non-mini envelope file set", () => {
    const cloudling = themeLoader.loadTheme("mochi");  // 夹具已换 mochi
    const files = collectThemeEnvelopeFiles(cloudling);

    assert.ok(files.includes("mochi-working.gif"));
    assert.ok(files.includes("mochi-sweeping.gif"));
    assert.ok(!files.includes("mochi-mini-idle.gif"));
    assert.ok(!files.some((file) => file.startsWith("mini-")));
  });

  it("uses the minimum top and bottom margins across a theme envelope", () => {
    const cloudling = themeLoader.loadTheme("mochi");
    const files = collectThemeEnvelopeFiles(cloudling);
    const expected = files.reduce((acc, file) => {
      const rect = hitGeometry.getContentRectScreen(cloudling, bounds, null, file, {
        box: cloudling.layout.contentBox,
      });
      if (!rect) return acc;
      return {
        top: Math.min(acc.top, Math.max(0, Math.round(rect.top - bounds.y))),
        bottom: Math.min(acc.bottom, Math.max(0, Math.round(bounds.y + bounds.height - rect.bottom))),
      };
    }, { top: Infinity, bottom: Infinity });

    const stable = computeStableVisibleContentMargins(cloudling, bounds, { box: cloudling.layout.contentBox });
    assert.deepStrictEqual(stable, expected);

    // mochi 的几何是均匀的（同一套 contentBox）；用合成变体注入每文件差异，
    // 验证 min-envelope 确实取的是所有文件的最小边距（算法意图不变）。
    const variant = structuredClone(cloudling);
    variant.objectScale = { ...variant.objectScale, fileOffsets: { "mochi-juggling.gif": { x: 0, y: 18 } } };
    const variantFiles = collectThemeEnvelopeFiles(variant);
    const shifted = variantFiles.reduce((acc, file) => {
      const rect = hitGeometry.getContentRectScreen(variant, bounds, null, file, {
        box: variant.layout.contentBox,
      });
      if (!rect) return acc;
      return {
        top: Math.min(acc.top, Math.max(0, Math.round(rect.top - bounds.y))),
        bottom: Math.min(acc.bottom, Math.max(0, Math.round(bounds.y + bounds.height - rect.bottom))),
      };
    }, { top: Infinity, bottom: Infinity });
    const variantStable = computeStableVisibleContentMargins(variant, bounds, { box: variant.layout.contentBox });
    assert.deepStrictEqual(variantStable, shifted);
    const idleRect = hitGeometry.getContentRectScreen(cloudling, bounds, "idle", cloudling.states.idle[0], {
      box: cloudling.layout.contentBox,
    });
    assert.ok(variantStable.top < Math.round(idleRect.top - bounds.y));
  });

  it("builds the theme anchor from marginBox and the idle file", () => {
    const cloudling = themeLoader.loadTheme("mochi");  // 夹具已换 mochi
    const expected = hitGeometry.getContentRectScreen(cloudling, bounds, "idle", cloudling.states.idle[0], {
      box: cloudling.layout.marginBox,
    });

    assert.deepStrictEqual(computeThemeAnchorRect(cloudling, bounds), expected);
  });


  it("keeps a stable theme anchor even though per-state hit bottoms differ", () => {
    const mochi = themeLoader.loadTheme("mochi");
    const anchor = computeThemeAnchorRect(mochi, bounds);
    const thinkingHit = hitGeometry.getHitRectScreen(
      mochi,
      bounds,
      "thinking",
      "mochi-thinking.gif",
      mochi.hitBoxes.default
    );
    const notificationHit = hitGeometry.getHitRectScreen(
      mochi,
      bounds,
      "notification",
      "mochi-notification.gif",
      mochi.hitBoxes.wide
    );

    assert.notStrictEqual(Math.round(thinkingHit.bottom), Math.round(notificationHit.bottom));
    assert.deepStrictEqual(
      anchor,
      hitGeometry.getContentRectScreen(mochi, bounds, "idle", mochi.states.idle[0], {
        box: mochi.layout.marginBox,
      })
    );
  });

  it("returns null for the theme anchor when the theme has no layout", () => {
    const theme = structuredClone(themeLoader.loadTheme("mochi"));
    delete theme.layout;
    assert.strictEqual(computeThemeAnchorRect(theme, bounds), null);
  });

});

describe("edge pinning margin policy", () => {
  it("keeps OFF drag bottom rubber band but caps total top overflow at half the window", () => {
    const margins = getLooseDragMargins({
      width: 200,
      height: 280,
      visibleMargins: { top: 100, bottom: 50 },
      allowEdgePinning: false,
    });

    assert.deepStrictEqual(margins, {
      marginX: 50,
      marginTop: 140, // capped to round(280 * 0.5)
      marginBottom: 70, // round(280 * 0.25), bottom drag OFF ignores visibleMargins.bottom
    });
  });

  it("OFF drag keeps the full 0.25h top overshoot when headroom is modest", () => {
    const margins = getLooseDragMargins({
      width: 200,
      height: 280,
      visibleMargins: { top: 40, bottom: 50 },
      allowEdgePinning: false,
    });

    assert.deepStrictEqual(margins, {
      marginX: 50,
      marginTop: 110, // 40 + round(280 * 0.25)
      marginBottom: 70,
    });
  });

  it("OFF drag stops adding extra top overshoot once rest headroom already exceeds half the window", () => {
    const margins = getLooseDragMargins({
      width: 200,
      height: 280,
      visibleMargins: { top: 170, bottom: 50 },
      allowEdgePinning: false,
    });

    assert.deepStrictEqual(margins, {
      marginX: 50,
      marginTop: 170,
      marginBottom: 70,
    });
  });

  it("ON drag uses height ratios 0.6/0.25 (Peter hitRect parity) regardless of visibleMargins", () => {
    const margins = getLooseDragMargins({
      width: 200,
      height: 280,
      visibleMargins: { top: 100, bottom: 50 }, // should be ignored when ON
      allowEdgePinning: true,
    });

    assert.deepStrictEqual(margins, {
      marginX: 50,
      marginTop: 168, // round(280 * 0.6)
      marginBottom: 70, // round(280 * 0.25)
    });
  });

  it("ON drag caps bottom slack by display inset", () => {
    const margins = getLooseDragMargins({
      width: 200,
      height: 280,
      visibleMargins: { top: 100, bottom: 50 },
      allowEdgePinning: true,
      bottomInset: 48,
    });

    assert.deepStrictEqual(margins, {
      marginX: 50,
      marginTop: 168,
      marginBottom: 48,
    });
  });

  it("OFF rest clamp keeps the visibleMargins verbatim", () => {
    assert.deepStrictEqual(
      getRestClampMargins({
        height: 280,
        visibleMargins: { top: 22, bottom: 14 },
        allowEdgePinning: false,
      }),
      { top: 22, bottom: 14 }
    );
  });

  it("ON rest clamp matches ON drag (no rubber-band bounce-back)", () => {
    const height = 280;
    const drag = getLooseDragMargins({
      width: 200,
      height,
      visibleMargins: { top: 22, bottom: 14 },
      allowEdgePinning: true,
    });
    const rest = getRestClampMargins({
      height,
      visibleMargins: { top: 22, bottom: 14 },
      allowEdgePinning: true,
    });

    assert.strictEqual(rest.top, drag.marginTop);
    assert.strictEqual(rest.bottom, drag.marginBottom);
    assert.deepStrictEqual(rest, { top: 168, bottom: 70 });
  });

  it("ON rest clamp caps bottom slack by display inset", () => {
    assert.deepStrictEqual(
      getRestClampMargins({
        height: 280,
        visibleMargins: { top: 500, bottom: 500 },
        allowEdgePinning: true,
        bottomInset: 48,
      }),
      { top: 168, bottom: 48 }
    );
  });

  it("ON cap uses the smaller of ratio and inset", () => {
    assert.deepStrictEqual(
      getRestClampMargins({
        height: 280,
        visibleMargins: { top: 500, bottom: 500 },
        allowEdgePinning: true,
        bottomInset: 120,
      }),
      { top: 168, bottom: 70 }
    );
  });

  it("ON bottom can clamp fully to zero when no physical inset is available", () => {
    const drag = getLooseDragMargins({
      width: 200,
      height: 280,
      visibleMargins: { top: 22, bottom: 14 },
      allowEdgePinning: true,
      bottomInset: 0,
    });
    const rest = getRestClampMargins({
      height: 280,
      visibleMargins: { top: 22, bottom: 14 },
      allowEdgePinning: true,
      bottomInset: 0,
    });

    assert.strictEqual(drag.marginBottom, 0);
    assert.strictEqual(rest.bottom, 0);
  });

  it("ON rest clamp ignores visibleMargins and uses height ratios", () => {
    assert.deepStrictEqual(
      getRestClampMargins({
        height: 280,
        visibleMargins: { top: 500, bottom: 500 }, // should be ignored
        allowEdgePinning: true,
      }),
      { top: 168, bottom: 70 }
    );
  });
});
