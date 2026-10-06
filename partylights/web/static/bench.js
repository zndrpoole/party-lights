/* Test bench.
 *
 * Holds the selected fixtures at raw channel values via /api/bench. The engine
 * writes those bytes after everything else and without the profile's
 * conversions, so what the sliders say is what goes on the wire -- which the
 * readback table confirms from /api/wire.
 */

const $ = (id) => document.getElementById(id);
const post = (url, body) => fetch(url, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
}).then((r) => r.json());

const ROLES = ["dimmer", "red", "green", "blue", "white", "amber", "uv"];
const DIMMER_PRESETS = [0, 4, 8, 14, 15, 30, 60, 255];
const COLOUR_PRESETS = [
  ["Orange", { red: 255, green: 51, blue: 0 }],     // Halloween palette orange
  ["Purple", { red: 115, green: 0, blue: 255 }],    // Halloween palette purple
  ["Red", { red: 255, green: 0, blue: 0 }],
  ["Green", { red: 0, green: 255, blue: 0 }],
  ["Blue", { red: 0, green: 0, blue: 255 }],
  ["White", { red: 255, green: 255, blue: 255 }],
];

let rig = null;
let selected = new Set();
let held = {};            // fid -> {role: byte}, from /api/state
let floor = 0;            // the live dimmer floor, from /api/state
let wire = {};            // fid -> decoded fixture, from /api/wire
const values = { dimmer: 255, red: 255, green: 51, blue: 0, white: 0, amber: 0, uv: 0 };
const rows = {};          // role -> {row, range, num}

const fixture = (fid) => rig.fixtures.find((f) => f.id === fid);
const rolesOf = (f) => f.channel_map.map((c) => c.role);

/* -- fixtures --------------------------------------------------------------- */

function buildChips() {
  const box = $("chips");
  rig.fixtures.forEach((f) => {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = f.label;
    b.title = `${f.id} · ch ${f.address}`;
    b.dataset.fid = f.id;
    b.onclick = () => {
      selected.has(f.id) ? selected.delete(f.id) : selected.add(f.id);
      onSelection();
    };
    box.appendChild(b);
  });
  document.querySelectorAll("[data-sel]").forEach((b) => {
    b.onclick = () => {
      const k = b.dataset.sel;
      selected = new Set(
        k === "none" ? [] :
        k === "all" ? rig.fixtures.map((f) => f.id) :
        rig.fixtures.filter((f) => f.groups.includes(k)).map((f) => f.id));
      onSelection();
    };
  });
}

function onSelection() {
  document.querySelectorAll(".chip").forEach((c) =>
    c.classList.toggle("sel", selected.has(c.dataset.fid)));
  // Start the sliders from what the first selected fixture is doing now, so
  // taking a fixture does not make it jump.
  const first = [...selected][0];
  if (first) {
    const f = fixture(first);
    const src = held[first] || {};
    const raw = wire[first] ? wire[first].raw : null;
    rolesOf(f).forEach((role, i) => {
      if (!ROLES.includes(role)) return;
      if (role in src) values[role] = src[role];
      else if (raw) values[role] = raw[i];
    });
  }
  const n = selected.size;
  $("s-target").textContent = n ? `· ${n} fixture${n > 1 ? "s" : ""}` : "· none selected";
  buildChannels();
  drawWire();
}

/* -- channels --------------------------------------------------------------- */

function availableRoles() {
  const have = new Set();
  selected.forEach((fid) => rolesOf(fixture(fid)).forEach((r) => have.add(r)));
  return ROLES.filter((r) => have.has(r));
}

function cutoff() {
  // Where the show goes black: the live dimmer floor, or a profile's own
  // min_dimmer if one in the selection is higher.
  let c = floor;
  selected.forEach((fid) => { c = Math.max(c, fixture(fid).min_dimmer || 0); });
  return c;
}

function buildChannels() {
  const box = $("channels");
  box.innerHTML = "";
  const roles = availableRoles();
  if (!roles.length) {
    box.innerHTML = `<div class="note" style="margin:0">Select a fixture to control it.</div>`;
    $("q-dimmer").style.display = $("q-colour").style.display = "none";
    updateWarn();
    return;
  }
  $("q-dimmer").style.display = $("q-colour").style.display = "";
  const c = cutoff();
  roles.forEach((role) => {
    const row = document.createElement("div");
    row.className = "ch";
    row.innerHTML = `
      <span class="name">${role}</span>
      <button data-d="-1">&minus;</button>
      <span class="track"><input type="range" min="0" max="255" step="1">
        ${role === "dimmer" && c ? `<i class="cutoff" style="left:calc(${(c / 255) * 100}% + ${8 - (c / 255) * 16}px)" title="show cutoff ${c}"></i>` : ""}
      </span>
      <button data-d="1">+</button>
      <input type="number" min="0" max="255" step="1">`;
    const range = row.querySelector("input[type=range]");
    const num = row.querySelector("input[type=number]");
    range.value = num.value = values[role];
    range.oninput = () => setValue(role, +range.value);
    num.onchange = () => setValue(role, +num.value);
    row.querySelectorAll("[data-d]").forEach((b) => {
      b.onclick = () => setValue(role, values[role] + +b.dataset.d);
    });
    box.appendChild(row);
    rows[role] = { range, num };
  });
  updateWarn();
}

function setValue(role, v, { send = true } = {}) {
  v = Math.max(0, Math.min(255, Math.round(v || 0)));
  values[role] = v;
  if (rows[role] && document.body.contains(rows[role].range)) {
    rows[role].range.value = v;
    rows[role].num.value = v;
  }
  updateWarn();
  if (send) queueSend();
}

function updateWarn() {
  const c = cutoff();
  const d = values.dimmer;
  $("warn").textContent = selected.size && c && d > 0 && d < c
    ? `Dimmer ${d} is below the show's cutoff of ${c}: during the show this fixture would be sent black.`
    : "";
}

/* Send the whole slider set, so a fixture taken mid-show gets a complete,
   predictable state rather than one channel on top of a dark frame. Coalesced
   to one request in flight, so dragging a slider cannot queue a backlog. */
let pending = false, inFlight = false;
function queueSend() {
  pending = true;
  if (!inFlight) flush();
}
async function flush() {
  if (!pending || !selected.size) { pending = false; return; }
  pending = false;
  inFlight = true;
  const roles = availableRoles();
  const body = { ids: [...selected], values: Object.fromEntries(roles.map((r) => [r, values[r]])) };
  try { await post("/api/bench", body); } catch {}
  inFlight = false;
  if (pending) flush();
}

function buildPresets() {
  DIMMER_PRESETS.forEach((v) => {
    const b = document.createElement("button");
    b.textContent = v;
    b.onclick = () => setValue("dimmer", v);
    $("q-dimmer").appendChild(b);
  });
  COLOUR_PRESETS.forEach(([name, rgb]) => {
    const b = document.createElement("button");
    b.innerHTML = `<span class="swatch" style="background:rgb(${rgb.red},${rgb.green},${rgb.blue})"></span>${name}`;
    b.onclick = () => {
      Object.entries(rgb).forEach(([r, v]) => setValue(r, v, { send: false }));
      ["white", "amber", "uv"].forEach((r) => setValue(r, 0, { send: false }));
      queueSend();
    };
    $("q-colour").appendChild(b);
  });
}

$("b-release").onclick = () => post("/api/bench/release", { ids: [...selected] });
$("b-release-all").onclick = () => post("/api/bench/release", {});

/* -- readback --------------------------------------------------------------- */

function drawWire() {
  const ids = [...selected];
  if (!ids.length) {
    $("wire").innerHTML = `<div class="note" style="margin:0">Select a fixture.</div>`;
    return;
  }
  const roles = availableRoles();
  const head = roles.map((r) => `<th>${r}</th>`).join("");
  const body = ids.map((fid) => {
    const f = fixture(fid), d = wire[fid];
    if (!d) return "";
    const map = rolesOf(f);
    const [r, g, b] = d.display;
    const cells = roles.map((role) => {
      const i = map.indexOf(role);
      return `<td>${i < 0 ? "" : d.raw[i]}</td>`;
    }).join("");
    return `<tr><td><span class="swatch" style="background:rgb(${r},${g},${b})"></span>${f.label}${held[fid] ? " ·&nbsp;held" : ""}</td>${cells}</tr>`;
  }).join("");
  $("wire").innerHTML = `<table><tr><th>Fixture</th>${head}</tr>${body}</table>`;
}

function connect() {
  const es = new EventSource("/api/wire");
  let last = 0;
  es.onmessage = (e) => {
    wire = JSON.parse(e.data).fixtures;
    const now = performance.now();
    if (now - last > 100) { last = now; drawWire(); }   // 10 Hz is plenty to read
  };
}

async function pollHeld() {
  try {
    const s = await fetch("/api/state").then((r) => r.json());
    held = s.state.raw || {};
    if (s.state.dimmer_floor !== floor) {
      floor = s.state.dimmer_floor || 0;
      buildChannels();
    }
  } catch { return; }
  const n = Object.keys(held).length;
  $("s-held").textContent = n;
  $("d-held").className = "dot " + (n ? "warn" : "ok");
  document.querySelectorAll(".chip").forEach((c) =>
    c.classList.toggle("held", !!held[c.dataset.fid]));
}

/* -- start ------------------------------------------------------------------ */

(async () => {
  rig = await fetch("/api/rig").then((r) => r.json());
  buildChips();
  buildPresets();
  buildChannels();
  connect();
  pollHeld();
  setInterval(pollHeld, 1000);
})();
