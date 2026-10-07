/* Live cue page.
 *
 * Six cues, matching the Stream Deck Mini's six buttons, with the same keyboard
 * shortcut shown on each. Every one POSTs to /api/cue/<name> -- exactly what a
 * Stream Deck button does -- so the phone, the keyboard and the hardware are
 * all the same code path and cannot drift apart.
 */

const $ = (id) => document.getElementById(id);
const fire = (name) => fetch(`/api/cue/${name}`, { method: "POST" }).then((r) => r.json());

/* Keep this list and the Stream Deck's six buttons in agreement. The `on`
   function says when a button should light up as engaged. */
const CUES = [
  { key: " ",  label: "Space", name: "blackout",  title: "Blackout",
    danger: true, on: (s) => s.state.blackout },
  { key: "f",  label: "F", name: "freeze",        title: "Freeze",
    on: (s) => s.state.freeze },
  { key: "m",  label: "M", name: "mode",          title: "Auto / Manual",
    on: (s) => s.state.mode === "manual" },
  { key: "n",  label: "N", name: "next-look",     title: "Next look" },
  { key: "p",  label: "P", name: "palette",       title: "Next palette" },
  { key: "b",  label: "B", name: "look/blinder",  title: "Blinder",
    on: (s) => s.engine.look === "blinder" },
];

const els = {};
const container = $("cues");
CUES.forEach((c) => {
  const el = document.createElement("div");
  el.className = "cue" + (c.danger ? " danger" : "");
  el.innerHTML = `<div class="t">${c.title}</div><div class="k">${c.label}</div>`;
  el.onclick = () => fire(c.name);
  container.appendChild(el);
  els[c.name] = el;
});

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "arrowup")   { e.preventDefault(); return void fire("master-up"); }
  if (k === "arrowdown") { e.preventDefault(); return void fire("master-down"); }
  const cue = CUES.find((c) => c.key === k || c.key === e.key);
  if (cue) { e.preventDefault(); fire(cue.name); }
});

const pct = (x) => Math.round(Math.max(0, Math.min(1, x)) * 100);
let lastBeat = -1;

function apply(s) {
  $("s-dmx").textContent = `${s.dmx.fps} fps`;
  $("s-dmx").previousElementSibling.className =
    "dot " + (s.dmx.fps > s.dmx.target_fps * 0.9 ? "ok" : "bad");
  $("s-look").textContent = s.engine.look;
  $("s-mode").textContent = s.state.mode;
  $("s-palette-auto").textContent = s.state.palette_auto ? "auto" : "manual";

  const m = s.music;
  $("s-bpm").textContent = m.available && m.bpm > 0 ? m.bpm.toFixed(0) : "—";
  if (m.available) {
    $("s-energy").textContent = m.energy.toFixed(2);
    $("m-energy").style.width = pct(m.energy) + "%";
    if (m.beat_index !== lastBeat) {
      lastBeat = m.beat_index;
      const b = $("beat");
      b.classList.add("hit");
      setTimeout(() => b.classList.remove("hit"), 90);
    }
  }
  $("s-master").textContent = pct(s.state.master) + "%";
  $("m-master").style.width = pct(s.state.master) + "%";

  const t = s.jukebox.track;
  $("s-track").textContent = t && t.title ? `${t.artist} — ${t.title}` : "";

  CUES.forEach((c) => {
    if (c.on) els[c.name].classList.toggle("on", !!c.on(s));
  });
}

const es = new EventSource("/api/stream");
es.onmessage = (e) => { try { apply(JSON.parse(e.data)); } catch (_) {} };
es.onerror = () => setTimeout(() => location.reload(), 3000);
