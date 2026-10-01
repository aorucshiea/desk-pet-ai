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

test("grounded: pet resting on floor stays put", () => {
  const { phys, state, applied } = mk();
  // petBottom = 800+100 = 900, floor = 1029 → falls. Put it on the floor first:
  state.bounds.y = 929; // bottom = 1029 = floor
  const r = phys._stepState({ vy: 0, grounded: true }, state.bounds, 1029, 0.016);
  assert.equal(r.grounded, true);
  assert.equal(r.y, 929);
});

test("floor rises → pet is pushed up, stays grounded", () => {
  const { phys, state } = mk();
  state.bounds.y = 929;
  const r = phys._stepState({ vy: 0, grounded: true }, state.bounds, 981, 0.016); // taskbar popped
  assert.equal(r.grounded, true);
  assert.equal(r.y, 881); // 981 - 100
});

test("floor drops → pet falls with gravity, then lands with impact speed", () => {
  const { phys, state } = mk();
  state.bounds.y = 881;
  let r = phys._stepState({ vy: 0, grounded: true }, state.bounds, 1029, 0.016);
  assert.equal(r.grounded, false);
  assert.equal(r.y, 881); // first falling frame does not move (vy starts 0)
  // integrate until landing
  let s = { vy: 0, grounded: false };
  let y = 881;
  let t = 0;
  while (!r.grounded && t < 5) {
    r = phys._stepState(s, { ...state.bounds, y }, 1029, 0.016);
    s = { vy: r.vy, grounded: r.grounded };
    y = r.y;
    t += 0.016;
  }
  assert.equal(y, 929);
  // fall of 48px under g=3200 → t = sqrt(2·48/3200) ≈ 0.173s
  assert.ok(t > 0.15 && t < 0.25, `fall time matches g=3200 for 48px, got ${t}s`);
});

test("external move (user drag) → 500ms hold, physics yields", async () => {
  const { phys, state, applied } = mk();
  state.bounds.y = 929; // grounded on floor 1029
  phys.start();
  await new Promise((r) => setTimeout(r, 50)); // let physics run & self-mark lastApplied
  state.bounds = { x: 300, y: 400, width: 100, height: 100 }; // external drag
  await new Promise((r) => setTimeout(r, 120));
  phys.stop();
  // during hold, physics must not re-assert any position at the dragged x
  const writesAfterDrag = applied.filter((a) => a.x === 300 && a.y !== 400);
  assert.equal(writesAfterDrag.length, 0);
});

test("guard suspension → physics never touches the window", () => {
  const applied = [];
  const state = { bounds: { x: 0, y: 0, width: 100, height: 100 } };
  const phys = createWorldPhysics({
    getBounds: () => state.bounds,
    setPosition: (x, y) => applied.push({ x, y }),
    getFloorY: () => 1029,
    guards: [() => true],
  });
  phys.start();
  return new Promise((resolve) => setTimeout(resolve, 60)).then(() => {
    phys.stop();
    assert.equal(applied.length, 0);
  });
});

test("non-grounded mid-air keeps accelerating (vy grows)", () => {
  const { phys, state } = mk();
  let s = { vy: 0, grounded: false };
  const v0 = phys._stepState(s, state.bounds, 2000, 0.016).vy;
  const v1 = phys._stepState({ vy: v0, grounded: false }, state.bounds, 2000, 0.016).vy;
  assert.ok(Math.abs(v1 - v0 - DEFAULTS.gravity * 0.016) < 1e-6);
});

test("window height animating (clawd resizing) → physics never pushes pet off-screen, converges after settle", async () => {
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
  // During height animation every write must keep the pet bottom at/near the floor
  // (never the runaway drift y→negative that motivated the height-yield guard).
  for (const a of applied) {
    assert.ok(a.y >= 0 && a.y + h <= 1029 + 80, `write ${JSON.stringify(a)} keeps pet near floor (h=${h})`);
  }
  await new Promise((r) => setTimeout(r, 500));
  phys.stop();
  assert.equal(state.bounds.y, 1029 - h); // settled onto floor
});
