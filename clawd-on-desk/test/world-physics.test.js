const { test } = require("node:test");
const assert = require("node:assert/strict");
const createWorldPhysics = require("../src/world-physics");

const DEFAULTS = createWorldPhysics({ getBounds: () => null, setPosition: () => {}, getFloorY: () => 1000 })._defaults;

function mk(overrides = {}) {
  const applied = [];
  const state = { bounds: { x: 100, y: 800, width: 100, height: 100 }, floor: 1029 };
  const landed = [];
  const phys = createWorldPhysics(Object.assign({
    getBounds: () => state.bounds,
    setPosition: (x, y) => { applied.push({ x, y }); state.bounds = { ...state.bounds, x, y }; },
    getFloorY: () => state.floor,
    onLanded: (vy) => landed.push(vy),
  }, overrides));
  return { phys, state, applied, landed };
}

test("grounded & content already on floor → holding steady, no writes", () => {
  const { phys, state } = mk();
  state.bounds.y = 929; // bottom=1029=floor
  const r = phys._stepPosition({ grounded: true }, state.bounds, 1029, 1000);
  assert.equal(r.holding, true);
  assert.equal(r.grounded, true);
});

test("floor rises (taskbar pops) → rigid push-up to new floor", () => {
  const { phys, state } = mk();
  state.bounds.y = 929;
  const r = phys._stepPosition({ grounded: true }, state.bounds, 981, 1000);
  assert.equal(r.grounded, true);
  assert.equal(r.winY, 881); // 981-100
});

test("regression: fall anchor (t0) must persist across ticks — the y=const freeze bug", () => {
  const { phys, state } = mk();
  state.bounds.y = 881; // 48px above floor 1029
  const start = phys._stepPosition({ grounded: true }, state.bounds, 1029, 1000);
  assert.equal(start.grounded, false);
  // tick N: create fall anchor
  const t1 = phys._stepPosition({ grounded: false, fall: start.fall }, state.bounds, 1029, 1050);
  assert.ok(t1.fall, "fall state must be returned so it persists");
  assert.equal(t1.fall.t0, 1000);
  // tick N+1 @ +120ms: winY = 881 + ½·3200·0.12²  (≈ 23px down)
  const t2 = phys._stepPosition({ grounded: false, fall: t1.fall }, state.bounds, 1029, 1120);
  assert.ok(!t2.grounded);
  const expected = 881 + 0.5 * DEFAULTS.gravity * 0.12 * 0.12;
  assert.ok(Math.abs(t2.winY - expected) < 1e-6, `winY follows analytic gravity, got ${t2.winY} want ${expected}`);
});

test("floor drops → fall with parametric gravity; position is a function of REAL elapsed time", () => {
  const { phys, state } = mk();
  state.bounds.y = 881; // 48px above floor 1029
  const start = phys._stepPosition({ grounded: true }, state.bounds, 1029, 1000);
  assert.equal(start.grounded, false);
  assert.deepEqual(start.fall, { t0: 1000, winY0: 881, vy0: 0 });
  // 50ms later (one throttled Electron tick): analytic y = y0 + ½·g·t²
  const mid = phys._stepPosition({ grounded: false, fall: start.fall }, state.bounds, 1029, 1050);
  assert.ok(!mid.grounded);
  assert.ok(Math.abs(mid.winY - (881 + 0.5 * DEFAULTS.gravity * 0.05 * 0.05)) < 1e-6);
  // The same physical time regardless of tick cadence: 400ms in one tick vs four.
  const late = phys._stepPosition({ grounded: false, fall: { t0: 1000, winY0: 881, vy0: 0 } }, state.bounds, 1029, 1400);
  assert.equal(late.grounded, true);
  assert.equal(late.winY, 929);
  assert.ok(late.landingVy > 0);
});

test("content-bottom anchor: floor snapping targets the visual content edge, not the window edge", () => {
  const { phys, state } = mk({
    getContentBottom: (b) => b.y + 160, // window is 200 tall; content ends 40px above window bottom
  });
  state.bounds = { x: 100, y: 500, width: 200, height: 200 };
  // content bottom = 660; floor pops UP to 640 (e.g. taskbar) → content must be
  // lifted so contentBottom == 640 → winY = 500 + (640 - 660) = 480
  const r = phys._stepPosition({ grounded: true }, state.bounds, 640, 1000);
  assert.equal(r.winY, 480);
  assert.equal(r.grounded, true);
});

test("external move (user drag) → 500ms hold, physics yields", async () => {
  const { state, applied, phys } = mk();
  state.bounds.y = 929;
  phys.start();
  await new Promise((r) => setTimeout(r, 80));
  applied.length = 0;
  state.bounds = { x: 300, y: 400, width: 100, height: 100 }; // external drag
  await new Promise((r) => setTimeout(r, 150));
  phys.stop();
  assert.equal(applied.filter((a) => a.x === 300 && a.y !== 400).length, 0);
});

test("guard suspension → physics never touches the window", async () => {
  const applied = [];
  const state = { bounds: { x: 0, y: 0, width: 100, height: 100 } };
  const phys = createWorldPhysics({
    getBounds: () => state.bounds,
    setPosition: (x, y) => applied.push({ x, y }),
    getFloorY: () => 1029,
    guards: [() => true],
  });
  phys.start();
  await new Promise((r) => setTimeout(r, 80));
  phys.stop();
  assert.equal(applied.length, 0);
});

test("window height animating → physics never pushes pet off-screen, converges after settle", async () => {
  const applied = [];
  let h = 200;
  const state = { bounds: { x: 64, y: 500, width: 240, height: h } };
  const phys = createWorldPhysics({
    getBounds: () => state.bounds,
    setPosition: (x, y) => { applied.push({ x, y }); state.bounds = { ...state.bounds, x, y }; },
    getFloorY: () => 1029,
  });
  phys.start();
  await new Promise((r) => setTimeout(r, 60));
  applied.length = 0;
  const grower = setInterval(() => { h += 2; state.bounds = { ...state.bounds, height: h, y: 1029 - h }; }, 20);
  await new Promise((r) => setTimeout(r, 200));
  clearInterval(grower);
  for (const a of applied) {
    assert.ok(a.y >= 0 && a.y + h <= 1029 + 80, `write ${JSON.stringify(a)} keeps pet near floor (h=${h})`);
  }
  await new Promise((r) => setTimeout(r, 500));
  phys.stop();
  assert.equal(state.bounds.y, 1029 - h);
});
