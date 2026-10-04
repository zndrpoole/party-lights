/* Control page.
 *
 * Reads state from an SSE stream and writes through small POSTs. Local widget
 * state is only adopted from the server when the user is not currently dragging
 * that widget -- otherwise a 20 Hz stream fights your finger on a fader, which
 * feels broken even though both sides are "correct".
 */

const $ = (id) => document.getElementById(id);
const post = (url, body) =>
  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json());

let rig = null;
let dragging = null;     // id of the widget being dragged right now
const fixtureEls = {};

/* -- helpers --------------------------------------------------------- */

const pct = (x) => Math.round(Math.max(0, Math.min(1, x)) * 100);

function rgbToHex([r, g, b]) {
  const h = (v) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, "0");
  return `#${h(r)}${h(g)}${h(b)}`;
}
function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}
function setDot(el, ok, warn) {
  el.className = "dot" + (ok ? " ok" : warn ? " warn" : " bad");
}

/* -- build static UI from the rig ------------------------------------ */

async function loadRig() {
  rig = await fetch("/api/rig").then((r) => r.json());

  const looks = $("looks");
  looks.innerHTML = "";
  rig.looks.forEach((l) => {
    const b = document.createElement("button");
    b.textContent = l.name;
    b.dataset.look = l.name;
    b.title = l.description;
    if (l.manual_only) b.classList.add("danger");
    b.onclick = () => post("/api/look", { look: l.name });
    b.onmouseenter = () => ($("look-desc").textContent = l.description);
    looks.appendChild(b);
  });

  const pals = $("palettes");
  pals.innerHTML = "";
  rig.palettes.forEach((p) => {
    const b = document.createElement("button");
    b.dataset.palette = p.name;
    b.title = p.description;
    b.innerHTML =
      `<span style="display:inline-flex;gap:3px;vertical-align:-2px;margin-right:7px">` +
      p.colors.map((c) =>
        `<i style="width:11px;height:11px;border-radius:50%;display:inline-block;background:${rgbToHex(c)}"></i>`
      ).join("") + `</span>${p.name}`;
    b.onclick = () => post("/api/palette", { palette: p.name });
    pals.appendChild(b);
  });

  const bands = $("bands");
  bands.innerHTML = "";
  ["sub", "bass", "lowmid", "mid", "highmid", "high", "air"].forEach((name) => {
    const d = document.createElement("div");
    d.className = "meter";
    d.innerHTML = `<div class="lbl"><span>${name}</span><span id="v-${name}">0.00</span></div>
                   <div class="bar"><i id="m-${name}"></i></div>`;
    bands.appendChild(d);
  });

  const groups = $("group-buttons");
  groups.innerHTML = "";
  rig.groups.forEach((g) => {
    const b = document.createElement("button");
    b.textContent = g;
    b.onclick = () => post("/api/fixtures", { group: g, active: true });
    groups.appendChild(b);
  });

  const grid = $("fixtures");
  grid.innerHTML = "";
  rig.fixtures.forEach((f) => {
    const el = document.createElement("div");
    el.className = "fix";
    el.innerHTML = `
      <div class="top">
        <span class="name">${f.id}</span>
        <span class="addr">@${f.address}&ndash;${f.address + f.channels - 1}</span>
      </div>
      <div class="mini">${f.model}${f.has_uv ? ' <span class="tag">uv</span>' : ""}</div>
      <div class="swatch" id="sw-${f.id}"></div>
      <label class="field">Intensity <span id="iv-${f.id}" class="mono">—</span></label>
      <input type="range" id="ii-${f.id}" min="0" max="100" value="100">
      <input type="color" id="ic-${f.id}" value="#ffffff">
      ${f.has_uv ? `<label class="field" style="margin-top:7px">UV <span id="uv-${f.id}" class="mono">0%</span></label>
                    <input type="range" id="ui-${f.id}" min="0" max="100" value="0">` : ""}
      <button id="ib-${f.id}" style="width:100%;margin-top:9px">Take</button>
    `;
    grid.appendChild(el);
    fixtureEls[f.id] = el;

    const send = (patch) => post(`/api/fixture/${f.id}`, patch);
    const ii = $(`ii-${f.id}`), ic = $(`ic-${f.id}`), ib = $(`ib-${f.id}`);

    ii.oninput = () => {
      dragging = `ii-${f.id}`;
      $(`iv-${f.id}`).textContent = ii.value + "%";
      send({ intensity: ii.value / 100, active: true });
    };
    ii.onchange = () => (dragging = null);
    ic.oninput = () => send({ rgb: hexToRgb(ic.value), active: true });
    ib.onclick = () => {
      const taken = el.classList.contains("taken");
      send({ active: !taken });
    };
    if (f.has_uv) {
      const ui = $(`ui-${f.id}`);
      ui.oninput = () => {
        dragging = `ui-${f.id}`;
        $(`uv-${f.id}`).textContent = ui.value + "%";
        send({ uv: ui.value / 100, active: true });
      };
      ui.onchange = () => (dragging = null);
    }
  });
}

/* -- apply live state ------------------------------------------------ */

let lastBeat = -1;

function apply(s) {
  const dmx = s.dmx, audio = s.audio, juke = s.jukebox, st = s.state, m = s.music;

  $("s-dmx").textContent = `${dmx.fps} fps`;
  setDot($("d-dmx"), dmx.fps > dmx.target_fps * 0.9, dmx.fps > 1);
  $("s-audio").textContent = audio.device
    ? `${audio.device.slice(0, 18)}${audio.dropouts ? ` (${audio.dropouts} drops)` : ""}`
    : "none";
  setDot($("d-audio"), !!audio.device && !m.silent, !!audio.device);
  $("s-juke").textContent = juke.connected ? "ok" : "offline";
  setDot($("d-juke"), juke.connected, false);

  $("s-mode").textContent = st.mode;
  $("b-mode").classList.toggle("on", st.mode === "manual");
  $("b-blackout").classList.toggle("on", st.blackout);
  $("b-freeze").classList.toggle("on", st.freeze);

  if (dragging !== "r-master") {
    $("r-master").value = pct(st.master);
  }
  $("s-master").textContent = pct(st.master) + "%";

  document.querySelectorAll("[data-look]").forEach((b) =>
    b.classList.toggle("on", b.dataset.look === s.engine.look));
  document.querySelectorAll("[data-palette]").forEach((b) =>
    b.classList.toggle("on", b.dataset.palette === st.palette));

  const trackBits = [];
  if (juke.track && juke.track.title) {
    trackBits.push(`${juke.track.artist} — ${juke.track.title}`);
    if (juke.track.requested_by) trackBits.push(`requested by ${juke.track.requested_by}`);
  }
  if (s.engine.fading_from) trackBits.push(`fading from ${s.engine.fading_from}`);
  if (s.engine.errors) trackBits.push(`${s.engine.errors} engine errors (${s.engine.last_error})`);
  $("s-track").textContent = trackBits.join("  ·  ");

  if (m.available) {
    $("s-bpm").textContent = m.bpm > 0 ? `${m.bpm.toFixed(1)} BPM` : "— BPM";
    $("s-lock").textContent = m.tempo_locked
      ? `locked, confidence ${m.tempo_confidence}`
      : "no tempo lock";
    if (m.beat_index !== lastBeat) {
      lastBeat = m.beat_index;
      const b = $("beat");
      b.classList.add("hit");
      setTimeout(() => b.classList.remove("hit"), 90);
    }
    ["kick", "snare", "hat"].forEach((r) => {
      const el = $(`h-${r}`);
      if (m.onsets && m.onsets[r]) {
        el.classList.add("on");
        setTimeout(() => el.classList.remove("on"), 90);
      }
    });
    Object.entries(m.bands || {}).forEach(([k, v]) => {
      const bar = $(`m-${k}`), val = $(`v-${k}`);
      if (bar) bar.style.width = pct(v) + "%";
      if (val) val.textContent = v.toFixed(2);
    });
    $("s-music").textContent =
      `energy ${m.energy.toFixed(2)} (sustained ${m.sustained_energy.toFixed(2)})  ·  ` +
      `${m.loudness_db} dBFS  ·  centroid ${m.centroid} Hz  ·  novelty ${m.novelty}` +
      (m.events.length ? `  ·  ${m.events.join(", ")}` : "") +
      (m.silent ? "  ·  SILENT" : "");
  } else {
    $("s-music").textContent = "no audio yet";
  }

  Object.entries(st.manual || {}).forEach(([fid, man]) => {
    const el = fixtureEls[fid];
    if (!el) return;
    el.classList.toggle("taken", man.active);
    const btn = $(`ib-${fid}`);
    if (btn) {
      btn.textContent = man.active ? "Release" : "Take";
      btn.classList.toggle("on", man.active);
    }
    const sw = $(`sw-${fid}`);
    if (sw) {
      sw.style.background = rgbToHex(man.rgb);
      sw.style.opacity = 0.25 + 0.75 * man.intensity;
    }
    if (dragging !== `ii-${fid}`) {
      const ii = $(`ii-${fid}`);
      if (ii) ii.value = pct(man.intensity);
      const iv = $(`iv-${fid}`);
      if (iv) iv.textContent = pct(man.intensity) + "%";
    }
    if (dragging !== `ui-${fid}`) {
      const ui = $(`ui-${fid}`);
      if (ui) ui.value = pct(man.uv);
      const uvv = $(`uv-${fid}`);
      if (uvv) uvv.textContent = pct(man.uv) + "%";
    }
  });
}

/* -- wire up --------------------------------------------------------- */

$("b-mode").onclick = () => post("/api/mode", {});
$("b-blackout").onclick = () => post("/api/cue/blackout");
$("b-freeze").onclick = () => post("/api/cue/freeze");
$("b-resume").onclick = () => post("/api/cue/resume-auto");
$("b-clear").onclick = () => post("/api/cue/clear-manual");
document.querySelector("[data-group-all]").onclick = () =>
  post("/api/fixtures", { ids: rig.fixtures.map((f) => f.id), active: true });

const rm = $("r-master");
rm.oninput = () => { dragging = "r-master"; post("/api/master", { master: rm.value / 100 }); };
rm.onchange = () => (dragging = null);

loadRig().then(() => {
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => { try { apply(JSON.parse(e.data)); } catch (_) {} };
  // The stream dropping is normal when the laptop sleeps; reconnect quietly.
  es.onerror = () => setTimeout(() => location.reload(), 3000);
});
