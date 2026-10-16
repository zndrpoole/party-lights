/* Yard view.
 *
 * A 3D model of 2255 S Longmore St (lot, house, trees, walls, neighbours),
 * built from public parcel, OpenStreetMap and USGS LiDAR data, with every
 * fixture in the patch placed in it as a spotlight.
 *
 * In "Live rig" mode each spotlight takes its colour and level from /api/wire,
 * the frame last handed to the DMX driver, decoded per fixture -- the same
 * source as /viz. So what you see lit here is what the receivers were sent.
 * "Preview" mode swaps in local colours for trying looks without touching the
 * rig; nothing in this page ever writes DMX.
 *
 * Placement (mount point, aim, beam, throw) is saved to the server through
 * /api/yard, so the laptop and the phone share one arrangement. "Use for
 * spatial looks" writes a top-down projection of it to /api/layout, which is
 * what the engine's spatial looks follow.
 *
 * Coordinates are metres: x east, y north, h up, origin at the address point.
 * Three.js is y-up, so the model's north maps to -z.
 */

const $ = (id) => document.getElementById(id);

/* -- the site, from verified public data ------------------------------------ */

const D = {
  parcel: [[15.42,-9.36],[-18.53,-9.62],[-18.29,-8.74],[-18.17,-7.85],[-18.01,-6.9],[-17.98,-5.98],[-17.95,-5.04],[-18.04,-4.12],[-18.1,-3.17],[-18.29,-2.22],[-18.47,-1.3],[-18.74,-0.38],[-19.02,0.5],[-19.41,1.36],[-19.78,2.22],[-20.23,3.04],[-20.69,3.81],[-21.24,4.54],[15.17,30.93]],
  house: [[-12.19,-8.02],[6.61,-0.11],[0.93,13.25],[-10.42,8.46],[-7.54,1.67],[-14.98,-1.45]],
  nb: [
    { n: "2275", h: 5, p: [[3.6,-38.1],[3.7,-30.3],[-8.1,-30.1],[-8.2,-38.3],[-15.8,-38.1],[-15.9,-44.8],[7.3,-45],[7.5,-38.2]] },
    { n: "2265", h: 5.7, p: [[9.8,-11.8],[-15.2,-11.8],[-15.2,-19.8],[-8,-19.8],[-8,-26.5],[9.8,-26.5]] },
    { n: "1600", h: 4.8, p: [[-21.1,16.4],[-23.5,13],[-18,8.9],[-6,25.2],[-10.8,28.7],[-8.8,31.3],[-15.9,36.5],[-27.4,20.9]] },
    { n: "1610", h: 5, p: [[-49.9,15.9],[-43.9,15.9],[-43.9,22.2],[-35.9,22.2],[-35.9,35.6],[-49.9,35.6]] },
    { n: "1620", h: 5, p: [[-52.4,40.1],[-66.9,40.1],[-66.9,14.7],[-58.9,14.7],[-58.9,21.2],[-52.4,21.2]] },
    { n: "1611", h: 5, p: [[-53.4,-10.6],[-53.4,-30.3],[-44.7,-30.3],[-44.7,-27.1],[-38.6,-27.1],[-38.6,-10.6]] },
    { n: "1557", h: 5, p: [[-17.4,78],[-17.4,56.3],[-3,56.3],[-3,78]] },
    { n: "1547", h: 5, p: [[14.1,55.5],[14.1,79.6],[-0.7,79.6],[-0.7,55.5]] },
  ],
  // [east, north, crown radius, height]
  trees: [[-16.9,-11.1,4,8.4],[9.5,-10.9,3.9,12.4],[7.7,-4.6,5.5,11.9],[16.6,-3.8,5.1,12.6],[5.8,6.5,3.7,6.5],[16.4,7.7,4.8,9.3],[-22.1,10.8,3.2,4.8],[-1.9,14.5,3.7,5.7],[3.2,19.7,5.5,11],[13.2,22.6,6.4,12.1],[16.7,31.5,4.4,8.7]],
  walls: [{ a: [15.42,-9.36], b: [-18.53,-9.62], h: 1.95 }, { a: [-21.24,4.54], b: [15.17,30.93], h: 1.63 }, { a: [15.17,30.93], b: [15.42,-9.36], h: 1.69 }],
  two: { eave: 6.6, ridge: 8.1, cutX: -6.8 },
  one: { eave: 3.0, ridge: 3.8 },
};

/* Default placements, in patch order: tree uplights, facade washes, eave
   downlights, wall-top washes. Anything past these gets a patio uplight. */
const DEFAULTS = [
  { x: 4.5, y: -5.5, h: .25, ax: 7.7, ay: -4.6, ah: 6, beam: 35, throw: 20 },
  { x: 8.8, y: 4.5, h: .25, ax: 5.8, ay: 6.5, ah: 3.5, beam: 35, throw: 16 },
  { x: 6.5, y: 17, h: .25, ax: 3.2, ay: 19.7, ah: 6, beam: 35, throw: 20 },
  { x: 10.5, y: 20, h: .25, ax: 13.2, ay: 22.6, ah: 6, beam: 35, throw: 20 },
  { x: 4, y: 11, h: .25, ax: -1.9, ay: 14.5, ah: 4, beam: 35, throw: 16 },
  { x: 9.5, y: -2.5, h: .25, ax: 6, ay: .5, ah: 4.5, beam: 45, throw: 16 },
  { x: 9.2, y: 3.5, h: .25, ax: 5.2, ay: 3.5, ah: 4.5, beam: 45, throw: 16 },
  { x: 7.5, y: 9, h: .25, ax: 2.6, ay: 9, ah: 4.5, beam: 45, throw: 16 },
  { x: 5.7, y: 3, h: 6.6, ax: 9, ay: 3, ah: 0, beam: 55, throw: 16 },
  { x: 3.2, y: 9, h: 6.6, ax: 7, ay: 9, ah: 0, beam: 55, throw: 16 },
  { x: 14.9, y: 14, h: 1.9, ax: 6, ay: 14, ah: .3, beam: 60, throw: 20 },
  { x: 14.4, y: 26, h: 1.9, ax: 8, ay: 20, ah: .3, beam: 60, throw: 20 },
];
const EXTRA = { x: 10, y: 12, h: .25, ax: 4, ay: 10, ah: 4, beam: 40, throw: 18 };
const GEOM = ["x", "y", "h", "ax", "ay", "ah", "beam", "throw"];
const LEN = ["x", "y", "h", "ax", "ay", "ah", "throw"];

const SWATCH_PRESETS = [
  ["Warm", () => fx.forEach((f) => Object.assign(f.pv, { on: true, color: "#ffb060", dim: 80 }))],
  ["Moonlight", () => fx.forEach((f) => Object.assign(f.pv, { on: true, color: "#9ec1ff", dim: 38 }))],
  ["Party", () => {
    const c = ["#ff2a2a", "#ff8a2a", "#ffe22a", "#2aff6a", "#2affe0", "#2a6bff", "#a02aff", "#ff2ad4"];
    fx.forEach((f, i) => Object.assign(f.pv, { on: true, color: c[i % c.length], dim: 85 }));
  }],
  ["Halloween", () => {
    const c = ["#ff6a00", "#8a2aff", "#2aff4a"];
    fx.forEach((f, i) => Object.assign(f.pv, { on: true, color: c[i % c.length], dim: 85 }));
  }],
  ["All off", () => fx.forEach((f) => { f.pv.on = false; })],
];

let rig = null;
let fx = [];            // [{id, label, rig, g: geometry, pv: preview, light, body, cone, tag}]
let sel = 0;
let U = 1;              // display unit multiplier: 1 = metres, 3.28084 = feet
let pickMode = null;
let mode = "live";      // "live" | "preview"
let showBeams = true;
let latest = {};        // fid -> decoded fixture from the last wire frame
let showLabels = true;

/* -- geometry helpers ------------------------------------------------------- */

function area(p) {
  let s = 0;
  for (let i = 0; i < p.length; i++) {
    const a = p[i], b = p[(i + 1) % p.length];
    s += a[0] * b[1] - b[0] * a[1];
  }
  return Math.abs(s) / 2;
}

function clipX(poly, c, keepGreater) {
  const out = [];
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i], b = poly[(i + 1) % poly.length];
    const ia = keepGreater ? a[0] >= c : a[0] <= c, ib = keepGreater ? b[0] >= c : b[0] <= c;
    if (ia) out.push(a);
    if (ia !== ib) { const t = (c - a[0]) / (b[0] - a[0]); out.push([c, a[1] + t * (b[1] - a[1])]); }
  }
  return out;
}

$("lotinfo").textContent = `${area(D.parcel).toFixed(0)} m²`;

/* -- scene ------------------------------------------------------------------ */

const cv = $("cv");
const renderer = new THREE.WebGLRenderer({ canvas: cv, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputEncoding = THREE.sRGBEncoding;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(48, 1, .3, 600);
const controls = new THREE.OrbitControls(camera, cv);
controls.enableDamping = !(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
controls.maxPolarAngle = Math.PI / 2 - .02;
controls.minDistance = 4;
controls.maxDistance = 160;

const VIEWS = {
  home: [[-30, 22, 16], [-1, 1.5, -8]],
  top: [[0, 78, -9.4], [0, 0, -10]],
  street: [[-48, 4.5, -3], [0, 2.5, -9]],
  yard: [[5, 3.2, 2.2], [10, 2, -20]],
};
function setView(k) {
  const [p, t] = VIEWS[k];
  camera.position.set(...p);
  controls.target.set(...t);
  controls.update();
}
setView("home");

const pick = [];
const mat = (c, r = .9) => new THREE.MeshStandardMaterial({ color: c, roughness: r, metalness: 0 });
const shapeOf = (p) => new THREE.Shape(p.map((q) => new THREE.Vector2(q[0], q[1])));

function extrude(p, h, m) {
  const g = new THREE.ExtrudeGeometry(shapeOf(p), { depth: h, bevelEnabled: false });
  g.rotateX(-Math.PI / 2);
  const o = new THREE.Mesh(g, m);
  o.castShadow = o.receiveShadow = true;
  return o;
}

// A roof surface over a footprint: triangulate, subdivide long edges so the
// height function can bend the surface, then lift each vertex.
function roof(poly, hfn, maxEdge, m) {
  const faces = THREE.ShapeUtils.triangulateShape(poly.map((q) => new THREE.Vector2(q[0], q[1])), []);
  const pos = [];
  const emit = (a, b, c) => {
    if ((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]) < 0) [b, c] = [c, b];
    [a, b, c].forEach((p) => pos.push(p[0], hfn(p[0], p[1]), -p[1]));
  };
  const rec = (a, b, c, d) => {
    const e = [[a, b], [b, c], [c, a]].map(([s, t]) => Math.hypot(s[0] - t[0], s[1] - t[1]));
    const mx = Math.max(...e);
    if (mx <= maxEdge || d > 12) { emit(a, b, c); return; }
    const i = e.indexOf(mx);
    const [p, q, r] = i === 0 ? [a, b, c] : i === 1 ? [b, c, a] : [c, a, b];
    const mid = [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2];
    rec(p, mid, r, d + 1);
    rec(mid, q, r, d + 1);
  };
  faces.forEach((f) => rec(poly[f[0]].slice(), poly[f[1]].slice(), poly[f[2]].slice(), 0));
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.computeVertexNormals();
  const o = new THREE.Mesh(g, m);
  o.castShadow = o.receiveShadow = true;
  return o;
}

const ground = new THREE.Mesh(new THREE.PlaneGeometry(260, 260).rotateX(-Math.PI / 2), mat(0x58595a, 1));
ground.receiveShadow = true;
scene.add(ground); pick.push(ground);
const lot = new THREE.Mesh(new THREE.ShapeGeometry(shapeOf(D.parcel)).rotateX(-Math.PI / 2), mat(0x7d8a66, 1));
lot.position.y = .02; lot.receiveShadow = true;
scene.add(lot); pick.push(lot);
scene.add(new THREE.LineLoop(
  new THREE.BufferGeometry().setFromPoints(D.parcel.map((p) => new THREE.Vector3(p[0], .06, -p[1]))),
  new THREE.LineBasicMaterial({ color: 0xffc247 })));

// House: a two-storey block and a one-storey wing, each with a gable whose
// ridge runs along the house's long axis.
const wallM = mat(0xd9ccb3, .95), roofM = mat(0x8a6a58, .9);
roofM.side = THREE.DoubleSide;
function gable(poly, eave, ridge) {
  const cr = [0.920, 0.391];
  const vs = poly.map((p) => p[0] * cr[0] + p[1] * cr[1]);
  const vmin = Math.min(...vs), vmax = Math.max(...vs), v0 = (vmin + vmax) / 2, hw = (vmax - vmin) / 2;
  const w = extrude(poly, eave, wallM);
  scene.add(w); pick.push(w);
  const r = roof(poly, (x, y) => eave + (ridge - eave) * Math.max(0, 1 - Math.abs(x * cr[0] + y * cr[1] - v0) / hw), .6, roofM);
  scene.add(r); pick.push(r);
}
gable(clipX(D.house, D.two.cutX, true), D.two.eave, D.two.ridge);
gable(clipX(D.house, D.two.cutX, false), D.one.eave, D.one.ridge);

const bwallM = mat(0xb9b2a6, .95);
D.walls.forEach((w) => {
  const dx = w.b[0] - w.a[0], dy = w.b[1] - w.a[1];
  const m = new THREE.Mesh(new THREE.BoxGeometry(Math.hypot(dx, dy), w.h, .2), bwallM);
  m.position.set((w.a[0] + w.b[0]) / 2, w.h / 2, -(w.a[1] + w.b[1]) / 2);
  m.rotation.y = Math.atan2(dy, dx);
  m.castShadow = m.receiveShadow = true;
  scene.add(m); pick.push(m);
});

const nbG = new THREE.Group();
scene.add(nbG);
const nbM = mat(0x7f8388, .95);
D.nb.forEach((n) => { const m = extrude(n.p, n.h, nbM); nbG.add(m); pick.push(m); });

// Trees can be hidden, resized and made see-through, since the LiDAR is from
// 2020-21 and a crown can hide the very light being placed under it.
const treeG = new THREE.Group();
scene.add(treeG);
const leaf = mat(0x2f5d34, .95), bark = mat(0x4b3a2c, 1);
const TREE_MIN = 0.2, TREE_MAX = 1.5, OPACITY_MIN = 0.1;
let treeOpacity = 1;
const trees = D.trees.map(([x, y, rad, h], i) => {
  const trunk = new THREE.Mesh(new THREE.CylinderGeometry(.22, .32, 2.6, 8), bark);
  trunk.castShadow = true;
  const crown = new THREE.Mesh(new THREE.SphereGeometry(1, 22, 16), leaf);
  crown.receiveShadow = true;
  const tag = makeTag(`T${i + 1}`, "#9fd7a8");
  treeG.add(trunk, crown, tag);
  pick.push(crown);
  return { x, y, r: Math.min(rad, 6.5), h, trunk, crown, tag, hidden: false, scale: 1 };
});

function shapeTree(t) {
  const s = t.scale, h = t.h * s;
  t.trunk.scale.set(1, s, 1);
  t.trunk.position.set(t.x, 1.3 * s, -t.y);
  t.crown.scale.set(t.r * s, (h - 2 * s) / 2, t.r * s);
  t.crown.position.set(t.x, (h + 2 * s) / 2, -t.y);
  t.tag.position.set(t.x, h + .6, -t.y);
  [t.trunk, t.crown, t.tag].forEach((o) => { o.visible = !t.hidden; });
  if (!showLabels) t.tag.visible = false;
}

function setTreeOpacity(v) {
  treeOpacity = Math.min(Math.max(v, OPACITY_MIN), 1);
  const solid = treeOpacity >= 0.99;
  if (leaf.transparent === solid) leaf.needsUpdate = true;
  leaf.transparent = !solid;
  leaf.opacity = treeOpacity;
  leaf.depthWrite = solid;
  // A see-through crown should not block light either.
  trees.forEach((t) => { t.crown.castShadow = solid; });
}

const amb = new THREE.AmbientLight(0x2a3b57, .35);
const hemi = new THREE.HemisphereLight(0x3a4a6a, 0x0a0a0a, .2);
const sun = new THREE.DirectionalLight(0xfff0d8, 1.3);
sun.position.set(40, 60, 25);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
Object.assign(sun.shadow.camera, { left: -50, right: 50, top: 50, bottom: -50, far: 200 });
scene.add(amb, hemi, sun);

function setNight(n) {
  scene.background = new THREE.Color(n ? 0x070b12 : 0x9fc3e6);
  amb.intensity = n ? .35 : .75;
  amb.color.set(n ? 0x2a3b57 : 0xffffff);
  hemi.intensity = n ? .2 : .5;
  sun.visible = !n;
  renderer.toneMappingExposure = n ? 1.25 : 1;
  $("t-night").classList.toggle("on", n);
  $("t-day").classList.toggle("on", !n);
}

/* -- fixtures --------------------------------------------------------------- */

const coneG = new THREE.ConeGeometry(1, 1, 28, 1, true);
coneG.translate(0, -.5, 0);
const DOWN = new THREE.Vector3(0, -1, 0), UP = new THREE.Vector3(0, 1, 0);
const aimDot = new THREE.Mesh(new THREE.SphereGeometry(.22, 12, 10), new THREE.MeshBasicMaterial({ color: 0x6ad7ff }));
const aimLine = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
  new THREE.LineBasicMaterial({ color: 0x6ad7ff }));
scene.add(aimDot, aimLine);

// A short label floating above each fixture, so the patch order can be read
// off the model (chases run in that order).
function makeTag(text, color = "#e8eaf0") {
  const font = "600 30px ui-monospace, Menlo, monospace";
  const c = document.createElement("canvas");
  let g = c.getContext("2d");
  g.font = font;
  // Short tags keep the original 128px canvas; longer names widen it.
  c.width = Math.max(128, Math.ceil(g.measureText(text).width) + 48);
  c.height = 64;
  g = c.getContext("2d");
  g.fillStyle = "rgba(8,13,19,.8)";
  g.fillRect(14, 10, c.width - 28, 44);
  g.fillStyle = color;
  g.font = font;
  g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText(text, c.width / 2, 33);
  const tex = new THREE.CanvasTexture(c);
  tex.encoding = THREE.sRGBEncoding;
  // Fixed size on screen, so the numbers stay readable from any distance.
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, sizeAttenuation: false }));
  s.scale.set(.05 * c.width / 128, .025, 1);
  s.renderOrder = 3;
  return s;
}

function shortName(f, i) {
  const m = f.id.match(/(\d+)$/);
  return m ? m[1] : f.id.slice(0, 2).toUpperCase() || String(i + 1);
}

function buildFixtures(saved) {
  let extra = 0;
  fx = rig.fixtures.map((r, i) => {
    const fallback = DEFAULTS[i] || {
      ...EXTRA, x: EXTRA.x + 1.5 * extra, ax: EXTRA.ax + 1.5 * (extra++),
    };
    const f = {
      id: r.id, label: r.label, rig: r, short: shortName(r, i),
      g: { ...fallback, ...(saved[r.id] || {}) },
      pv: { on: true, color: "#ffb060", dim: 80 },
      shadow: false,
    };
    f.light = new THREE.SpotLight(0xffffff, 0, 20, .3, .35, 2);
    f.light.shadow.mapSize.set(1024, 1024);
    f.light.shadow.bias = -.0004;
    scene.add(f.light, f.light.target);
    f.body = new THREE.Mesh(new THREE.CylinderGeometry(.13, .16, .34, 14),
      new THREE.MeshStandardMaterial({ color: 0x222a31, emissive: 0x000000, roughness: .6 }));
    scene.add(f.body);
    f.cone = new THREE.Mesh(coneG, new THREE.MeshBasicMaterial({
      color: 0xffffff, transparent: true, opacity: .07, depthWrite: false, side: THREE.DoubleSide }));
    f.cone.renderOrder = 2;
    scene.add(f.cone);
    f.tag = makeTag(f.short);
    scene.add(f.tag);
    pick.push(f.body);
    return f;
  });
}

// Where the fixture sits and points. Called on placement changes only.
function placeFixture(f) {
  const g = f.g;
  const pos = new THREE.Vector3(g.x, g.h, -g.y), aim = new THREE.Vector3(g.ax, g.ah, -g.ay);
  const dir = aim.clone().sub(pos);
  const len = Math.max(dir.length(), .1);
  dir.normalize();
  f.light.position.copy(pos);
  f.light.target.position.copy(aim);
  f.light.distance = g.throw;
  f.light.angle = THREE.MathUtils.degToRad(g.beam / 2);
  f.light.shadow.camera.far = g.throw;
  f.body.position.copy(pos);
  f.body.quaternion.setFromUnitVectors(UP, dir);
  f.cone.position.copy(pos);
  f.cone.quaternion.setFromUnitVectors(DOWN, dir);
  const rad = Math.tan(THREE.MathUtils.degToRad(g.beam / 2)) * len;
  f.cone.scale.set(rad, len, rad);
  f.tag.position.set(g.x, g.h + .75, -g.y);
}

const tmpColor = new THREE.Color();

// What the fixture is emitting: from the wire in live mode, from the local
// preview otherwise. `level` is linear light output 0..1.
function emission(f) {
  if (mode === "preview") {
    const on = f.pv.on && f.pv.dim > 0;
    return { hex: f.pv.color, chip: f.pv.color, level: on ? f.pv.dim / 100 : 0 };
  }
  const d = latest[f.id];
  if (!d || !(d.output > 0)) return { hex: null, level: 0 };
  // `display` is gamma-encoded and already scaled by level; undo the scale so
  // the spotlight gets the hue and `output` alone sets its brightness.
  const [r, g, b] = d.display;
  const m = Math.max(r, g, b, 1);
  return { rgb: [r / m, g / m, b / m], chip: `rgb(${r},${g},${b})`, level: d.output };
}

// Colour and level. Cheap, so it runs on every wire frame.
function lightFixture(f) {
  const e = emission(f);
  const on = e.level > 0.002;
  if (e.rgb) tmpColor.setRGB(e.rgb[0], e.rgb[1], e.rgb[2]).convertSRGBToLinear();
  else if (e.hex) tmpColor.set(e.hex).convertSRGBToLinear();
  else tmpColor.setRGB(0, 0, 0);
  f.light.visible = on;
  f.light.color.copy(tmpColor);
  f.light.intensity = 7 * e.level;
  f.light.castShadow = f.shadow && on;
  f.body.material.emissive.copy(tmpColor);
  f.body.material.emissiveIntensity = on ? .1 + .9 * e.level : 0;
  f.cone.visible = showBeams && on;
  f.cone.material.color.copy(tmpColor);
  f.cone.material.opacity = .03 + .07 * e.level;
  const chip = chipEls[fx.indexOf(f)];
  if (chip) chip.style.background = !on ? "#000" : e.chip;
}

function lightAll() { fx.forEach(lightFixture); }

function markSelected() {
  const f = fx[sel];
  if (!f) return;
  const a = new THREE.Vector3(f.g.x, f.g.h, -f.g.y), b = new THREE.Vector3(f.g.ax, f.g.ah, -f.g.ay);
  aimDot.position.copy(b);
  aimLine.geometry.setFromPoints([a, b]);
  const n = fx.filter((x) => x.shadow).length;
  $("shadow-note").textContent = n > 3
    ? `${n} fixtures cast shadows. Expect a slow frame rate.`
    : "Shadows are costly: keep three or fewer on.";
  $("shadow-note").style.color = n > 3 ? "var(--bad)" : "";
}

/* -- console ---------------------------------------------------------------- */

const SLIDERS = { beam: "s-beam", throw: "s-throw", x: "s-x", y: "s-y", h: "s-h", ax: "s-ax", ay: "s-ay", ah: "s-ah" };
const chipEls = [];

function fmt(v, k) {
  if (LEN.includes(k)) return `${(v * U).toFixed(1)} ${U === 1 ? "m" : "ft"}`;
  return k === "beam" ? `${v.toFixed(0)}°` : `${v.toFixed(0)}%`;
}

Object.entries(SLIDERS).forEach(([k, id]) => {
  const el = $(id);
  Object.assign(el.dataset, { min: el.min, max: el.max, step: el.step });
  el.addEventListener("input", () => {
    fx[sel].g[k] = parseFloat(el.value) / (LEN.includes(k) ? U : 1);
    placeFixture(fx[sel]);
    $(`o-${k}`).textContent = fmt(fx[sel].g[k], k);
    markSelected();
    save();
  });
});
$("s-dim").addEventListener("input", () => {
  fx[sel].pv.dim = parseFloat($("s-dim").value);
  $("o-dim").textContent = fmt(fx[sel].pv.dim, "dim");
  lightFixture(fx[sel]);
});
$("f-on").onchange = (e) => { fx[sel].pv.on = e.target.checked; lightFixture(fx[sel]); };
$("f-color").oninput = (e) => { fx[sel].pv.color = e.target.value; lightFixture(fx[sel]); };
$("f-shadow").onchange = (e) => { fx[sel].shadow = e.target.checked; lightFixture(fx[sel]); markSelected(); };

function sync() {
  const f = fx[sel];
  Object.entries(SLIDERS).forEach(([k, id]) => {
    const el = $(id), m = LEN.includes(k) ? U : 1;
    el.min = (el.dataset.min * m).toFixed(1);
    el.max = (el.dataset.max * m).toFixed(1);
    el.step = LEN.includes(k) ? (el.dataset.step * m).toFixed(2) : el.dataset.step;
    el.value = f.g[k] * m;
    $(`o-${k}`).textContent = fmt(f.g[k], k);
  });
  $("s-dim").value = f.pv.dim;
  $("o-dim").textContent = fmt(f.pv.dim, "dim");
  $("f-on").checked = f.pv.on;
  $("f-color").value = f.pv.color;
  $("f-shadow").checked = f.shadow;
  $("fname").textContent = f.label;
  $("fsub").textContent = `${f.id} · ${f.rig.model} · ch ${f.rig.address}–${f.rig.address + f.rig.channels - 1}`;
  chipEls.forEach((b, i) => b.parentElement.classList.toggle("sel", i === sel));
  markSelected();
}

function buildChips() {
  const box = $("chips");
  box.innerHTML = "";
  chipEls.length = 0;
  fx.forEach((f, i) => {
    const b = document.createElement("button");
    b.innerHTML = `<i></i>${f.short}`;
    b.title = `${f.label} (${f.id})`;
    b.setAttribute("aria-label", `Fixture ${f.id}`);
    b.onclick = () => { sel = i; sync(); };
    box.appendChild(b);
    chipEls.push(b.firstChild);
  });
}

function setMode(m) {
  mode = m;
  $("m-live").classList.toggle("on", m === "live");
  $("m-preview").classList.toggle("on", m === "preview");
  document.querySelectorAll(".preview-only").forEach((el) => { el.hidden = m !== "preview"; });
  $("m-note").textContent = m === "live"
    ? "Colours come from the frame sent to the transmitter, as in Stage view."
    : "Local colours for trying ideas. The rig is not touched.";
  try { localStorage.setItem("yard.mode", m); } catch {}
  lightAll();
}
$("m-live").onclick = () => setMode("live");
$("m-preview").onclick = () => setMode("preview");

SWATCH_PRESETS.forEach(([name, fn]) => {
  const b = document.createElement("button");
  b.textContent = name;
  b.onclick = () => { fn(); sync(); lightAll(); };
  $("presets").appendChild(b);
});

function setPick(m) {
  pickMode = pickMode === m ? null : m;
  $("pick-pos").classList.toggle("on", pickMode === "pos");
  $("pick-aim").classList.toggle("on", pickMode === "aim");
  cv.style.cursor = pickMode ? "crosshair" : "";
}
$("pick-pos").onclick = () => { cancelDraft(); setPick("pos"); };
$("pick-aim").onclick = () => { cancelDraft(); setPick("aim"); };

/* -- saving ----------------------------------------------------------------- */

const post = (url, body) => fetch(url, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function placement() {
  const out = {};
  fx.forEach((f) => { out[f.id] = Object.fromEntries(GEOM.map((k) => [k, f.g[k]])); });
  return out;
}

let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const r = await post("/api/yard", { ...placement(), trees: treeSettings(), ...areaSettings() });
      $("s-saved").textContent = r.ok ? "Placement saved." : "Save failed.";
    } catch {
      $("s-saved").textContent = "Save failed: is the app running?";
    }
  }, 300);
}

$("b-reset").onclick = () => {
  let extra = 0;
  fx.forEach((f, i) => {
    f.g = { ...(DEFAULTS[i] || { ...EXTRA, x: EXTRA.x + 1.5 * extra, ax: EXTRA.ax + 1.5 * (extra++) }) };
    placeFixture(f);
  });
  sync();
  save();
};

$("b-copy").onclick = () => {
  const j = JSON.stringify(placement(), null, 1), ta = $("json");
  ta.hidden = false;
  ta.value = j;
  ta.select();
  (navigator.clipboard?.writeText ? navigator.clipboard.writeText(j) : Promise.reject())
    .then(() => { $("s-saved").textContent = "Copied."; },
          () => { $("s-saved").textContent = "Select the text below and copy it."; });
};

// Project the yard from above (north up) onto the 0..1 stage plan that
// spatial looks read, scaled evenly so distances keep their proportions.
$("b-stage").onclick = async () => {
  const xs = fx.map((f) => f.g.x), ys = fx.map((f) => f.g.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const W = .86, H = .66, X0 = .07, Y0 = .12;
  const s = Math.min(W / Math.max(maxX - minX, 1e-6), H / Math.max(maxY - minY, 1e-6));
  const ox = X0 + (W - s * (maxX - minX)) / 2, oy = Y0 + (H - s * (maxY - minY)) / 2;
  const layout = {};
  fx.forEach((f) => {
    layout[f.id] = { x: +(ox + s * (f.g.x - minX)).toFixed(4), y: +(oy + s * (maxY - f.g.y)).toFixed(4) };
  });
  try {
    const r = await post("/api/layout", layout);
    $("s-saved").textContent = r.ok
      ? "Stage view layout replaced with these positions; spatial looks now follow the yard."
      : "Could not update the stage layout.";
  } catch {
    $("s-saved").textContent = "Could not update the stage layout: is the app running?";
  }
};

/* -- trees ------------------------------------------------------------------ */

function treeSettings() {
  return { opacity: +treeOpacity.toFixed(2),
           items: trees.map((t) => ({ hidden: t.hidden, scale: +t.scale.toFixed(2) })) };
}

function applyTreeSettings(saved) {
  const items = (saved && Array.isArray(saved.items)) ? saved.items : [];
  trees.forEach((t, i) => {
    const it = items[i] || {};
    t.hidden = !!it.hidden;
    t.scale = Math.min(Math.max(+it.scale || 1, TREE_MIN), TREE_MAX);
    shapeTree(t);
  });
  setTreeOpacity(saved && saved.opacity != null ? +saved.opacity : 1);
}

function buildTreeRows() {
  const box = $("tree-rows");
  box.innerHTML = "";
  trees.forEach((t, i) => {
    const row = document.createElement("div");
    row.className = "trow";
    row.innerHTML = `
      <label class="chk"><input type="checkbox"> T${i + 1}</label>
      <input type="range" min="${TREE_MIN * 100}" max="${TREE_MAX * 100}" step="5" aria-label="Tree T${i + 1} size">
      <output class="mono"></output>`;
    const [chk, size, out] = [row.querySelector("input[type=checkbox]"), row.querySelector("input[type=range]"), row.querySelector("output")];
    t.row = { row, chk, size, out };
    chk.onchange = () => { t.hidden = !chk.checked; shapeTree(t); syncTreeRow(t); save(); };
    size.oninput = () => { t.scale = size.value / 100; shapeTree(t); syncTreeRow(t); save(); };
    box.appendChild(row);
  });
  $("s-topac").oninput = (e) => { setTreeOpacity(e.target.value / 100); syncTrees(); save(); };
  $("b-trees-show").onclick = () => { trees.forEach((t) => { t.hidden = false; shapeTree(t); }); syncTrees(); save(); };
  $("b-trees-hide").onclick = () => { trees.forEach((t) => { t.hidden = true; shapeTree(t); }); syncTrees(); save(); };
  $("b-trees-reset").onclick = () => { applyTreeSettings(null); syncTrees(); save(); };
}

function syncTreeRow(t) {
  t.row.chk.checked = !t.hidden;
  t.row.size.value = Math.round(t.scale * 100);
  t.row.size.disabled = t.hidden;
  t.row.out.textContent = t.hidden ? "off" : `${(t.h * t.scale * U).toFixed(1)} ${U === 1 ? "m" : "ft"}`;
}

function syncTrees() {
  trees.forEach(syncTreeRow);
  $("s-topac").value = Math.round(treeOpacity * 100);
  $("o-topac").textContent = `${Math.round(treeOpacity * 100)}%`;
}

function flashTreeRow(i) {
  const row = trees[i].row.row;
  row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  row.classList.remove("flash");
  void row.offsetWidth;
  row.classList.add("flash");
}

/* -- areas and markers ------------------------------------------------------ */

// Shapes drawn on the ground to say where the party is and where light should
// (and should not) land, plus named points. They save with the placement, so
// /api/yard holds them as exact coordinates in the model's metres.
const AREA_KINDS = {
  boundary: { label: "Backyard boundary", color: 0x6ad7ff, fill: 0 },
  lit: { label: "Light this", color: 0xffa733, fill: .28 },
  dark: { label: "Keep dark", color: 0xff4d5e, fill: .22 },
};
const MARKER_COLOR = 0xe8eaf0;
let areas = [];          // [{name, kind, points: [[x, y], ...], obj}]
let markers = [];        // [{name, x, y, obj}]
let draft = null;        // the area being drawn: {name, kind, points, obj}
let placingMarker = false;
const areaG = new THREE.Group();
scene.add(areaG);
const dotGeom = new THREE.SphereGeometry(.15, 8, 6);
const hex = (c) => "#" + c.toString(16).padStart(6, "0");

function centroid(points) {
  const n = points.length;
  return [points.reduce((a, p) => a + p[0], 0) / n, points.reduce((a, p) => a + p[1], 0) / n];
}

function areaObject(a, open) {
  const k = AREA_KINDS[a.kind], grp = new THREE.Group();
  const pts = a.points.map(([x, y]) => new THREE.Vector3(x, .1, -y));
  if (pts.length >= 2) {
    const Line = open || pts.length < 3 ? THREE.Line : THREE.LineLoop;
    grp.add(new Line(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: k.color, depthTest: false, toneMapped: false })));
  }
  if (k.fill && !open && a.points.length >= 3) {
    const m = new THREE.Mesh(new THREE.ShapeGeometry(shapeOf(a.points)).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: k.color, transparent: true, opacity: k.fill, toneMapped: false,
                                    depthWrite: false, side: THREE.DoubleSide }));
    m.position.y = .08;
    grp.add(m);
  }
  const dotM = new THREE.MeshBasicMaterial({ color: k.color, toneMapped: false });
  a.points.forEach(([x, y]) => {
    const d = new THREE.Mesh(dotGeom, dotM);
    d.position.set(x, .12, -y);
    grp.add(d);
  });
  if (!open && a.points.length) {
    const [cx, cy] = centroid(a.points);
    const t = makeTag(a.name, hex(k.color));
    t.position.set(cx, .8, -cy);
    grp.add(t);
  }
  grp.renderOrder = 2;
  return grp;
}

function markerObject(m) {
  const grp = new THREE.Group();
  const pin = new THREE.Mesh(new THREE.ConeGeometry(.25, .9, 12), new THREE.MeshBasicMaterial({ color: MARKER_COLOR }));
  pin.rotation.x = Math.PI;
  pin.position.set(m.x, .45, -m.y);
  const t = makeTag(m.name);
  t.position.set(m.x, 1.5, -m.y);
  grp.add(pin, t);
  return grp;
}

function redrawAreas() {
  areaG.clear();
  areas.forEach((a) => { a.obj = areaObject(a, false); areaG.add(a.obj); });
  markers.forEach((m) => { m.obj = markerObject(m); areaG.add(m.obj); });
  if (draft) { draft.obj = areaObject(draft, true); areaG.add(draft.obj); }
  buildAreaList();
}

function areaSettings() {
  return {
    areas: areas.map(({ name, kind, points }) => ({ name, kind, points })),
    markers: markers.map(({ name, x, y }) => ({ name, x, y })),
  };
}

function applyAreaSettings(saved) {
  areas = (saved.areas || []).filter((a) => AREA_KINDS[a.kind] && Array.isArray(a.points) && a.points.length >= 3)
    .map((a) => ({ name: String(a.name || AREA_KINDS[a.kind].label), kind: a.kind, points: a.points }));
  markers = (saved.markers || []).map((m) => ({ name: String(m.name || "Marker"), x: +m.x, y: +m.y }));
  redrawAreas();
}

function defaultName(kind) {
  const n = areas.filter((a) => a.kind === kind).length + 1;
  return kind === "boundary" && n === 1 ? "Backyard" : `${AREA_KINDS[kind].label} ${n}`;
}

function areaMode() {
  const busy = !!draft || placingMarker;
  $("a-drawing").hidden = !draft;
  $("a-draw").classList.toggle("on", !!draft);
  $("a-marker").classList.toggle("on", placingMarker);
  $("a-finish").disabled = !draft || draft.points.length < 3;
  $("a-undo").disabled = !draft || !draft.points.length;
  cv.style.cursor = busy || pickMode ? "crosshair" : "";
  $("a-note").textContent = draft
    ? `Drawing "${draft.name}": ${draft.points.length} point${draft.points.length === 1 ? "" : "s"}. Click the ground to add corners. Finish or Enter closes it, Esc cancels.`
    : placingMarker ? "Click the ground to drop the marker. Esc cancels."
    : "Top view works best. Pick a kind, name it, then click Draw area.";
}

function stopPicking() {
  pickMode = null;
  $("pick-pos").classList.remove("on");
  $("pick-aim").classList.remove("on");
}

$("a-draw").onclick = () => {
  if (draft) { finishDraft(); return; }
  stopPicking();
  placingMarker = false;
  const kind = $("a-kind").value;
  draft = { kind, name: $("a-name").value.trim() || defaultName(kind), points: [] };
  $("a-name").value = "";
  setView("top");
  redrawAreas();
  areaMode();
};
$("a-marker").onclick = () => {
  stopPicking();
  if (draft) cancelDraft();
  placingMarker = !placingMarker;
  if (placingMarker) setView("top");
  areaMode();
};
$("a-finish").onclick = () => finishDraft();
$("a-undo").onclick = () => { if (draft) { draft.points.pop(); redrawAreas(); areaMode(); } };
$("a-cancel").onclick = () => cancelDraft();

function finishDraft() {
  if (!draft || draft.points.length < 3) return;
  areas.push({ name: draft.name, kind: draft.kind, points: draft.points });
  draft = null;
  redrawAreas();
  areaMode();
  save();
}

function cancelDraft() {
  draft = null;
  placingMarker = false;
  redrawAreas();
  areaMode();
}

function groundClick(p) {
  const pt = [+p.x.toFixed(2), +(-p.z).toFixed(2)];
  if (placingMarker) {
    const name = $("a-name").value.trim() || `Marker ${markers.length + 1}`;
    $("a-name").value = "";
    markers.push({ name, x: pt[0], y: pt[1] });
    placingMarker = false;
    redrawAreas();
    areaMode();
    save();
    return;
  }
  draft.points.push(pt);
  redrawAreas();
  areaMode();
}

document.addEventListener("keydown", (e) => {
  if (!draft && !placingMarker) return;
  if (e.key === "Escape") { cancelDraft(); e.preventDefault(); }
  else if (e.key === "Enter" && draft) { finishDraft(); e.preventDefault(); }
});

function buildAreaList() {
  const box = $("a-list");
  box.innerHTML = "";
  const row = (color, name, detail, onRename, onDelete) => {
    const r = document.createElement("div");
    r.className = "arow";
    r.innerHTML = `<i style="background:${color}"></i><input maxlength="40" aria-label="Name">
      <span class="mono"></span><button aria-label="Delete">✕</button>`;
    const [input, span, del] = [r.querySelector("input"), r.querySelector("span"), r.querySelector("button")];
    input.value = name;
    span.textContent = detail;
    input.onchange = () => { onRename(input.value.trim() || name); redrawAreas(); save(); };
    del.onclick = () => { onDelete(); redrawAreas(); save(); };
    box.appendChild(r);
  };
  areas.forEach((a, i) => {
    const m2 = area(a.points);
    const size = U === 1 ? `${m2.toFixed(0)} m²` : `${(m2 * 10.7639).toFixed(0)} ft²`;
    row(hex(AREA_KINDS[a.kind].color), a.name, `${AREA_KINDS[a.kind].label.split(" ")[0]} · ${size}`,
        (n) => { a.name = n; }, () => areas.splice(i, 1));
  });
  markers.forEach((m, i) => {
    row(hex(MARKER_COLOR), m.name, `pin · E${m.x.toFixed(0)} N${m.y.toFixed(0)}`,
        (n) => { m.name = n; }, () => markers.splice(i, 1));
  });
}

/* -- scene toggles ---------------------------------------------------------- */

$("t-night").onclick = () => setNight(true);
$("t-day").onclick = () => setNight(false);
function toggle(id, fn) {
  const b = $(id);
  b.onclick = () => { const on = !b.classList.contains("on"); b.classList.toggle("on", on); fn(on); };
}
toggle("t-beams", (on) => { showBeams = on; lightAll(); });
toggle("t-labels", (on) => {
  showLabels = on;
  fx.forEach((f) => { f.tag.visible = on; });
  trees.forEach(shapeTree);
});
toggle("t-trees", (on) => { treeG.visible = on; });
toggle("t-nb", (on) => { nbG.visible = on; });
$("t-units").onclick = () => {
  U = U === 1 ? 3.28084 : 1;
  $("t-units").textContent = U === 1 ? "Feet" : "Metres";
  sync();
  syncTrees();
  buildAreaList();
};
document.querySelectorAll("[data-view]").forEach((b) => { b.onclick = () => setView(b.dataset.view); });

/* -- picking ---------------------------------------------------------------- */

const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
// The raycaster ignores visibility, so hidden trees would still catch clicks.
function shown(o) {
  for (; o; o = o.parent) if (!o.visible) return false;
  return true;
}
let down = null, hoverQueued = false;

function hit(ev) {
  const r = cv.getBoundingClientRect();
  mouse.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(mouse, camera);
  return ray.intersectObjects(pick.filter(shown), false)[0] || null;
}

cv.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
cv.addEventListener("pointerup", (e) => {
  if (!down) return;
  const moved = Math.hypot(e.clientX - down[0], e.clientY - down[1]);
  down = null;
  if (moved > 5) return;
  const h = hit(e);
  if (!h) return;
  if (draft || placingMarker) { groundClick(h.point); return; }
  if (!pickMode) {
    // A plain click on a fixture selects it.
    const i = fx.findIndex((f) => f.body === h.object);
    if (i >= 0) { sel = i; sync(); }
    const ti = trees.findIndex((t) => t.crown === h.object);
    if (ti >= 0) flashTreeRow(ti);
    return;
  }
  const f = fx[sel], p = h.point;
  if (pickMode === "pos") { f.g.x = +p.x.toFixed(2); f.g.y = +(-p.z).toFixed(2); }
  else { f.g.ax = +p.x.toFixed(2); f.g.ay = +(-p.z).toFixed(2); f.g.ah = +Math.max(0, p.y).toFixed(2); }
  placeFixture(f);
  sync();
  save();
});
cv.addEventListener("pointermove", (e) => {
  if (hoverQueued) return;
  hoverQueued = true;
  const ev = { clientX: e.clientX, clientY: e.clientY };
  requestAnimationFrame(() => {
    hoverQueued = false;
    const h = hit(ev);
    if (!h) { $("readout").textContent = "Hover the ground for coordinates"; return; }
    const p = h.point, up = Math.max(0, p.y), ft = 3.28084;
    $("readout").textContent = `E ${p.x.toFixed(1)} m · N ${(-p.z).toFixed(1)} m · up ${up.toFixed(1)} m  `
      + `(${(p.x * ft).toFixed(0)}, ${(-p.z * ft).toFixed(0)}, ${(up * ft).toFixed(0)} ft)`;
  });
});

/* -- wire stream and engine readout ----------------------------------------- */

let frames = 0, lastFrameAt = 0;

function connect() {
  const es = new EventSource("/api/wire");
  es.onmessage = (e) => {
    latest = JSON.parse(e.data).fixtures;
    if (mode === "live") lightAll();
    frames += 1;
    lastFrameAt = performance.now();
  };
  // EventSource reconnects on its own; the status dot shows the gap.
}

setInterval(() => {
  const live = performance.now() - lastFrameAt < 1000;
  $("d-wire").className = "dot " + (live ? (frames >= 30 ? "ok" : "warn") : "bad");
  $("s-fps").textContent = live ? `${frames} fps` : "no frames";
  frames = 0;
}, 1000);

async function pollState() {
  try {
    const s = await (await fetch("/api/state")).json();
    $("s-look").textContent = (s.engine && s.engine.look) || "—";
  } catch {}
}

/* -- start ------------------------------------------------------------------ */

function resize() {
  const s = cv.parentElement, w = s.clientWidth, h = s.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

(async () => {
  const [rigRes, yardRes] = await Promise.all([fetch("/api/rig"), fetch("/api/yard")]);
  rig = await rigRes.json();
  const savedYard = await yardRes.json();
  buildFixtures(savedYard);
  buildTreeRows();
  applyTreeSettings(savedYard.trees);
  syncTrees();
  applyAreaSettings(savedYard);
  buildChips();
  fx.forEach(placeFixture);
  setNight(true);
  let saved = "live";
  try { saved = localStorage.getItem("yard.mode") || "live"; } catch {}
  setMode(saved === "preview" ? "preview" : "live");
  sync();
  try { new ResizeObserver(resize).observe(cv.parentElement); } catch { window.addEventListener("resize", resize); }
  resize();
  (function loop() {
    requestAnimationFrame(loop);
    controls.update();
    $("compass").style.transform = `rotate(${controls.getAzimuthalAngle()}rad)`;
    renderer.render(scene, camera);
  })();
  pollState();
  setInterval(pollState, 1000);
  connect();
})();
