/* Stage view.
 *
 * Draws each fixture from /api/wire, which is the frame the DMX writer last
 * handed to the driver, decoded through the fixture's own profile. Nothing here
 * reads engine state, so if this page and the room disagree, the fault is past
 * the USB port: cable, receiver, address or fixture menu.
 *
 * Positions are fractions of the stage (0..1) and are saved to the server, so
 * one arrangement serves the laptop and the phone alike.
 */

const $ = (id) => document.getElementById(id);
const stage = $("stage");

let rig = null;
let layout = {};
const els = {};          // fid -> {root, lens, badges, addr}
let latest = {};         // fid -> decoded fixture from the last frame
let selected = null;
let locked = false;
let showAddr = false;

/* The flash-rate range drawn for a strobe channel's slow..fast span. */
const STROBE_HZ_MIN = 1, STROBE_HZ_MAX = 20;

/* -- default arrangement ---------------------------------------------------- */

function defaultLayout(fixtures) {
  // PARs on a shallow arc across the front, in patch order (which is the
  // physical order chases rely on); anything else centred upstage.
  const out = {};
  const pars = fixtures.filter((f) => f.groups.includes("pars"));
  const rest = fixtures.filter((f) => !f.groups.includes("pars"));
  pars.forEach((f, i) => {
    const t = pars.length > 1 ? i / (pars.length - 1) : 0.5;
    out[f.id] = { x: 0.07 + 0.86 * t, y: 0.78 - 0.32 * Math.sin(Math.PI * t) };
  });
  rest.forEach((f, i) => {
    out[f.id] = { x: (i + 1) / (rest.length + 1), y: 0.2 };
  });
  return out;
}

/* -- building --------------------------------------------------------------- */

function build() {
  stage.innerHTML = "";
  const defaults = defaultLayout(rig.fixtures);
  rig.fixtures.forEach((f) => {
    const pos = layout[f.id] || defaults[f.id];
    layout[f.id] = pos;
    const root = document.createElement("div");
    root.className = "fx" + (f.emitters.length > 3 ? " big" : "");
    root.innerHTML = `
      <div class="lens"><div class="badges"></div></div>
      <div class="tag">${f.label}<small>${f.id} · ch ${f.address}–${f.address + f.channels - 1}</small></div>`;
    stage.appendChild(root);
    els[f.id] = {
      root,
      lens: root.querySelector(".lens"),
      badges: root.querySelector(".badges"),
      addr: root.querySelector("small"),
      strobeHz: 0,
    };
    place(f.id);
    enableDrag(f.id, root);
  });
  applyAddrVisibility();
}

function place(fid) {
  const { x, y } = layout[fid];
  els[fid].root.style.left = `${x * 100}%`;
  els[fid].root.style.top = `${y * 100}%`;
}

/* -- dragging --------------------------------------------------------------- */

function enableDrag(fid, root) {
  let start = null;
  root.addEventListener("pointerdown", (e) => {
    start = { x: e.clientX, y: e.clientY, moved: false };
    root.setPointerCapture(e.pointerId);
  });
  root.addEventListener("pointermove", (e) => {
    if (!start || locked) return;
    if (!start.moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 4) return;
    start.moved = true;
    root.classList.add("dragging");
    const r = stage.getBoundingClientRect();
    // Keep the lens and its label inside the stage rather than clipped.
    const mx = 40 / r.width, mTop = 40 / r.height, mBottom = 64 / r.height;
    layout[fid] = {
      x: Math.min(1 - mx, Math.max(mx, (e.clientX - r.left) / r.width)),
      y: Math.min(1 - mBottom, Math.max(mTop, (e.clientY - r.top) / r.height)),
    };
    place(fid);
  });
  const end = () => {
    if (!start) return;
    root.classList.remove("dragging");
    if (start.moved) save();
    else select(fid);
    start = null;
  };
  root.addEventListener("pointerup", end);
  root.addEventListener("pointercancel", end);
}

let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const r = await fetch("/api/layout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(layout),
      });
      $("s-saved").textContent = r.ok ? "Layout saved" : "Save failed";
    } catch {
      $("s-saved").textContent = "Save failed: is the app running?";
    }
  }, 250);
}

/* -- drawing a frame -------------------------------------------------------- */

function draw(fid, d) {
  const el = els[fid];
  if (!el) return;
  const [r, g, b] = d.display;
  const out = d.output;
  el.lens.style.background = out > 0 ? `rgb(${r},${g},${b})` : "#000";
  el.lens.style.boxShadow = out > 0.003
    ? `0 0 ${8 + 46 * out}px ${2 + 16 * out}px rgba(${r},${g},${b},${0.25 + 0.6 * out})`
    : "none";

  // The fixture runs the strobe itself, so only the rate is on the wire; the
  // flash speed here is an estimate across the profile's slow..fast range.
  const strobing = d.strobe !== null && d.strobe > 0 && out > 0;
  const hz = strobing ? STROBE_HZ_MIN + (STROBE_HZ_MAX - STROBE_HZ_MIN) * d.strobe : 0;
  // Only restart the animation on a real rate change, not one-step wobble.
  if (Math.abs(hz - el.strobeHz) > 0.5 || (hz === 0) !== (el.strobeHz === 0)) {
    el.strobeHz = hz;
    el.lens.classList.toggle("strobing", strobing);
    el.lens.style.setProperty("--period", `${Math.round(1000 / Math.max(hz, 1))}ms`);
  }

  const badges = [];
  if (strobing) badges.push(`<span class="badge">STROBE</span>`);
  if ((d.emitters.uv || 0) * d.dimmer > 0.02) badges.push(`<span class="badge uv">UV</span>`);
  const html = badges.join("");
  if (el.badges.innerHTML !== html) el.badges.innerHTML = html;
}

function drawDetail() {
  const box = $("detail");
  const f = rig && rig.fixtures.find((x) => x.id === selected);
  const d = latest[selected];
  if (!f || !d) return;
  const [r, g, b] = d.display;
  const rows = f.channel_map.map((c, i) => {
    const v = d.raw[i];
    return `<tr><td class="num">${c.channel}</td><td>${c.role}${c.note ? ` <span style="color:var(--dim)">· ${c.note.split(" - ")[0]}</span>` : ""}</td>
      <td class="num">${v}</td><td class="barcell"><i style="width:${(v / 255) * 100}%"></i></td></tr>`;
  }).join("");
  box.innerHTML = `
    <h2><span class="swatch" style="background:rgb(${r},${g},${b})"></span>${f.label} · ${f.model}</h2>
    <table><tr><th>DMX ch</th><th>Role</th><th class="num">Value</th><th></th></tr>${rows}</table>`;
}

function select(fid) {
  if (selected && els[selected]) els[selected].root.classList.remove("sel");
  selected = selected === fid ? null : fid;
  if (selected) els[selected].root.classList.add("sel");
  else $("detail").innerHTML = `<h2>Fixture</h2><div class="note" style="margin:0">Tap a fixture to see its channels.</div>`;
  drawDetail();
}

/* -- toolbar ---------------------------------------------------------------- */

function applyAddrVisibility() {
  Object.values(els).forEach((el) => { el.addr.style.display = showAddr ? "" : "none"; });
  $("b-names").classList.toggle("on", showAddr);
}

$("b-lock").onclick = () => {
  locked = !locked;
  stage.classList.toggle("locked", locked);
  $("b-lock").classList.toggle("on", locked);
  $("b-lock").textContent = locked ? "Layout locked" : "Lock layout";
  try { localStorage.setItem("viz.locked", locked ? "1" : ""); } catch {}
};
$("b-names").onclick = () => {
  showAddr = !showAddr;
  applyAddrVisibility();
  try { localStorage.setItem("viz.addr", showAddr ? "1" : ""); } catch {}
};
$("b-reset").onclick = () => {
  layout = defaultLayout(rig.fixtures);
  Object.keys(els).forEach(place);
  save();
};

/* -- wire stream ------------------------------------------------------------ */

let frames = 0, lastFrameAt = 0;

function connect() {
  const es = new EventSource("/api/wire");
  es.onmessage = (e) => {
    const p = JSON.parse(e.data);
    latest = p.fixtures;
    for (const fid in p.fixtures) draw(fid, p.fixtures[fid]);
    if (selected) drawDetail();
    $("s-frame").textContent = p.frame;
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

/* -- start ------------------------------------------------------------------ */

(async () => {
  try {
    locked = !!localStorage.getItem("viz.locked");
    showAddr = !!localStorage.getItem("viz.addr");
  } catch {}
  const [rigRes, layoutRes] = await Promise.all([fetch("/api/rig"), fetch("/api/layout")]);
  rig = await rigRes.json();
  layout = await layoutRes.json();
  build();
  if (locked) { locked = false; $("b-lock").click(); }
  connect();
})();
