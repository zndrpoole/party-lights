# party-lights

Music-reactive DMX lighting for the [party jukebox](../juke-box). Analyses the
audio coming out of the host Mac, reads track context from the jukebox, and
drives 13 DMX fixtures in time with the music — with manual override from a
phone and a Stream Deck for panic cues.

Runs as its own process. The jukebox is not modified and does not depend on
this: if the lighting rig falls over mid-party, the music and the guests' queue
page carry on untouched.

## Hardware

| | |
|---|---|
| Interface | ENTTEC Open DMX USB (FTDI FT232R, `0x0403:0x6001`) |
| Fixtures | 12 × U'King **ZQ01104** 36-LED RGB PAR, 7ch each |
| | 1 × U'King **ZQ01430** 6×18W RGBWA+UV PAR, 10ch |
| Channels | 94 of 512 |
| Host | 2019 Intel i9 MacBook Pro |

The Open DMX USB has no microcontroller: there is no firmware doing DMX framing
for us. The host generates the BREAK and the whole frame, continuously, forever.
If this process stops, the fixtures hold their last value — which is why
shutdown always sends a final all-zero frame.

### Two things that will bite you

**The PAR mode channel.** Offset 5 of the ZQ01104 (channel 6 for `par1`) selects
between manual DMX control and five internal programs. Anything above 10 and the
fixture runs its own show and silently ignores every colour we send, which from
across the room looks exactly like a wiring fault. The profile pins it to 0 on
*every* frame, so a glitched frame cannot strand a fixture in auto mode.

**Run the ZQ01430 on AC.** 108 W against its 9600 mAh battery is well under an
hour at full output.

## Setup

```bash
python3.13 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Check `config/settings.yaml` — mainly `dmx.port`:

```bash
ls /dev/cu.usbserial-*
```

### Output driver

Set `dmx.driver` in `config/settings.yaml`:

- **`serial`** (default) — Apple's FTDI virtual serial port. No setup, works on
  both Intel and Apple Silicon. Measured on the real hardware: a steady 40.0 fps
  with zero late frames, ~1.2 ms median per frame.
- **`ftdi`** — direct libusb, tighter BREAK timing. Needs Apple's driver out of
  the way, which only Intel allows:
  `sudo kextunload -b com.apple.driver.AppleUSBFTDI`. SIP blocks this on Apple
  Silicon.
- **`null`** — no hardware. Everything else still runs, so looks and the UI can
  be developed and tested with nothing plugged in.

Start with `serial`. It is good enough that `ftdi` is an optimisation, not a
requirement.

## Phase 0: prove the output path

Do this before anything else, and before a party.

```bash
./.venv/bin/python tools/dmx_sweep.py              # guided sequence
```

Individually:

```bash
python tools/dmx_sweep.py flood          # everything to full — is anything alive?
python tools/dmx_sweep.py identify       # light each fixture in turn, by name
python tools/dmx_sweep.py fixture par1   # exercise one fixture via its profile
python tools/dmx_sweep.py sweep          # walk every channel, one at a time
python tools/dmx_sweep.py address        # addressing aid (see below)
python tools/dmx_sweep.py off
```

`flood` tests the *link*. `fixture` tests the *profile*. If `flood` lights things
up but `fixture` produces the wrong colours, the channel map is wrong, not the
cable.

### Addressing

These fixtures ship on **address 1**, which means all 13 behave identically until
re-addressed — a very common first-time confusion.

```bash
python tools/dmx_sweep.py address
```

That holds channel 1 at full, so every fixture still on address 1 is lit. Walk
the room setting each fixture's address from the printed list; each one goes dark
as you move it off address 1. When the room is dark, you are done.

Target addresses (from `config/rig.yaml`):

```
par1..par12  ->  1, 8, 15, 22, 29, 36, 43, 50, 57, 64, 71, 78
accent       ->  85
```

Then `identify` confirms `par1..par12` are in the physical order you listed them
in `rig.yaml`. Get that order wrong and chases look like random flashing instead
of movement.

### Wireless DMX

A wireless receiver is a transparent cable replacement carrying the whole
512-channel universe, so it changes nothing about addressing and you do **not**
need one per fixture. But cheap 2.4 GHz kits add 20–80 ms of often *variable*
latency and drop frames when the Wi-Fi is busy — and a house party is a room
full of phones. Prefer wired XLR for the main chain. `audio.output_delay_ms`
compensates a fixed offset; it cannot compensate jitter.

## Adding more lights

Edit `config/rig.yaml`. Nothing else.

More of a model already in use — bump `count`:

```yaml
- id: par
  profile: zq01104
  count: 16          # was 12; addresses re-expand automatically
  address: 1
  groups: [pars, rgb]
```

A new model — drop a profile into `config/profiles/` describing its channels by
*role*, then reference it by filename stem. Address collisions and
past-the-end-of-the-universe mistakes are hard errors at load, naming both
culprits.

## Running it

```bash
./.venv/bin/python -m partylights.cli run
```

Then open **http://localhost:5055/** — bound to `0.0.0.0`, so use the Mac's LAN
address from your phone and run the rig from anywhere in the house.

| | |
|---|---|
| `/` | Full control: mode, master, looks, palettes, live meters, per-fixture override |
| `/live` | Six big cue buttons with keyboard shortcuts, mirroring the Stream Deck |
| `/api/cue/<name>` | The cue API — what the Stream Deck hits |

Useful flags while setting up: `--driver null` (no hardware), `--no-audio`,
`--no-jukebox`. All three let you work on part of the system in isolation.

`--audio-file track.wav` analyses a file instead of the loopback, silently and
looping, so you can design looks in `/viz` at a laptop. For a track with a
known build, gap and drop, `python tools/make_test_audio.py /tmp/audio` writes
`edm_arc_128.wav` (drops at 60 s and 120 s); `/viz` shows the song arc
(groove / build / predrop / drop / breakdown) in its header.

### Audio capture

The Mac has no system loopback, so the signal has to be split:

```
Spotify ─► Multi-Output Device ─┬─► your real speakers   (the room hears this)
                                └─► BlackHole            (we analyse this)
```

```bash
brew install --cask blackhole-2ch       # needs your password
```

Then in **Audio MIDI Setup**: create a Multi-Output Device, tick your real
speakers *and* BlackHole, set **your real speakers as the Master device**, and
tick **Drift Correction on BlackHole only**.

That orientation is the whole trick. Every device clocks samples off its own
crystal, so a nominal 48000 Hz is really 48000.4 on one and 47999.6 on the
other. Feed one stream to two devices and one drains its buffer faster than the
other fills it, until you hear clicks, dropouts, or the outputs sliding out of
sync. Drift Correction resamples the secondary device to match the Master's
clock. Get the Master backwards and you get the clicking — which is why people
say Multi-Output "doesn't work". BlackHole is virtual and has no crystal of its
own, so with the real speakers as Master there is nothing left to drift.

Known cost: with a Multi-Output Device active the **F11/F12 volume keys stop
working**. Set level inside Spotify instead.

`python -m partylights.cli audio-devices` lists what's available and tells you
what to put in `audio.device`.

**Not Loopback's free trial.** Rogue Amoeba's Loopback has no free tier — the
trial runs fully featured for 20 minutes per launch and then *overlays noise on
the audio passing through it*, which at a party is your music. BlackHole is
genuinely free and does the one thing needed here perfectly.

## The look engine

Looks are pure functions from the state of the music to what each fixture should
do. They own no DMX, no timing and no fixtures — they receive a `MusicState` and
return an `Emission` per fixture id, and the engine handles blending, overrides,
the master dimmer and output. A look therefore cannot break the output path,
which is what makes one safe to edit mid-party.

| Look | |
|---|---|
| `ambient` | Slow breathing wash. The graceful idle, and the fallback whenever anything is unavailable |
| `wash` | Colour gradient along the run, brightness from the low end |
| `pulse` | Alternating halves hit on every kick |
| `chase` | Bright head sweeping the room, locked to beat phase |
| `sparkle` | Scattered flashes on hi-hats over a dim bed |
| `uv` | Blacklight from the accent fixture over a dark bed |
| `strobe` | **Manual only**, time-limited — see below |
| `blinder` | **Manual only** — everything full white |

Auto mode picks from sustained energy, tempo lock and structural events (a drop
goes kinetic, a breakdown goes to UV), holding a look for 22 seconds so it reads
as a lighting operator rather than an energy meter driving a selector switch.
Leaving the idle look bypasses that dwell, so starting the rig mid-song reacts
immediately.

**Strobe safety.** Rapid full-field flashing between roughly 5 and 30 Hz is the
photosensitive-seizure risk band. `strobe` and `blinder` are excluded from
automatic selection — nothing should put that on a room unprompted — and the
engine caps how long a strobe can run (`engine.max_strobe_seconds`) so a stuck
button or a forgotten cue cannot leave the room flashing. That cap is a safety
rail, not a style choice.

### Adding a look

One file in `partylights/engine/looks/`, one entry in that package's `LOOKS`
tuple. Subclass `Look`, implement `render(music, palette, dt)`, return a dict of
fixture id to `Emission`. Omitted fixtures go black, so a look only describes
what it drives.

## Stream Deck and keyboard

Every manual action is a named cue, and every trigger routes through the same
endpoint — web buttons, keyboard, Stream Deck, MIDI later. The Stream Deck needs
**no plugin**: its built-in "System → Open" action can run a script.

```bash
python -m partylights.cli streamdeck ~/party-cues
```

That writes one script per suggested button; point each Stream Deck button at
one. Reassigning is a one-line edit because every cue is just a URL.

```
blackout  freeze  mode  next-look  palette  master-up  master-down
clear-manual  resume-auto  look/<name>  palette/<name>
```

`python -m partylights.cli cues` lists them all. `resume-auto` is the "undo all
my fiddling" cue: releases every override, unfreezes, back to music-driven.

## Tuning the analysis offline

```bash
python tools/make_test_audio.py /tmp/audio     # known tempo, known hit counts
python tools/analyze_file.py track.mp3 --timeline
python tools/analyze_file.py /tmp/audio/click_128.wav --expect-bpm 128
```

`analyze_file.py` drives the *same analyser object* the live engine drives, fed
the same way. That equivalence is the point: it means tuning against a file
genuinely tunes the live rig, with no hardware, no speakers and no waiting for
the right moment in a song.

Measured against synthetic ground truth: all five tempos from 90 to 174 BPM
within 2% and locked 100% of each track; onsets 31/32 kicks, 33/32 snares,
64/64 hats; a drop detected 0.73 s after the real one.

Spotify's `audio-features` / `audio-analysis` endpoints were deprecated for new
apps in November 2024, so a pre-baked beat grid is not available. Real-time DSP
is the only honest option.

## Party-night runbook

**Beforehand**

1. `python tools/dmx_sweep.py` — confirm the link and the profiles.
2. `python tools/dmx_sweep.py address` — set every fixture's address.
3. `python tools/dmx_sweep.py identify` — confirm `par1..par12` are in physical order.
4. Install BlackHole, build the Multi-Output Device, check `audio-devices`.
5. Plug the ZQ01430 into AC, not battery.
6. Run for a full album at volume. Thermal throttling, USB dropouts and clock
   drift only show up after many minutes — not in a two-minute test.

**Startup order**

1. Start the jukebox (port 5000), authorise Spotify, pin the playback device.
2. Set macOS output to the Multi-Output Device.
3. Start party-lights. Check the three status dots at the top of the page.
4. Open `/live` on your phone and leave it open.

**When something goes wrong**

| Symptom | Cause |
|---|---|
| One fixture does its own colour show | Its mode channel is not 0 — wrong DMX mode on the fixture's own menu |
| All fixtures do the same thing | They are all still on address 1 |
| Nothing responds | Cable direction, or the fixture is not in DMX mode. `dmx_sweep.py flood` tests the link alone |
| Lights lag the music | `audio.output_delay_ms`. Wireless DMX adds 20–80 ms; AirPlay adds ~2 s |
| Lights stutter | Check `dropouts` and DMX `late` in the UI. Usually the i9 throttling |
| Lights frozen on one frame | `freeze` is on, or the engine died — the writer keeps sending the last good frame by design |
| Fixtures stuck on after quitting | Should not happen: shutdown sends an all-zero frame. If the process was `kill -9`, re-run and blackout |

**Panic**: Space on `/live`, or the blackout button, or
`curl -X POST localhost:5055/api/cue/blackout`.

## Layout

```
config/
  settings.yaml      runtime: driver, port, audio device, jukebox URL
  rig.yaml           THE PATCH: what exists and where it answers
  profiles/*.yaml    per-model channel maps
partylights/
  dmx/               drivers, the universe, and its 40 Hz writer thread
  fixtures/          colour conversion, profiles, patch
  audio/             capture, features, tempo, structure, analyser facade
  engine/            engine, state, cues, palettes, looks/
  jukebox/           polls the jukebox for track context
  web/               Flask UI and JSON API
tools/
  dmx_sweep.py       hardware and profile verification
  analyze_file.py    run the live analysis chain offline against a file
  make_test_audio.py synthetic tracks with known tempo and hit counts
  audio_check.py     confirm the loopback tap is live
```

## Tests

```bash
./.venv/bin/python -m pytest
```

105 tests. Most of the audio and engine ones are regression guards on bugs that
produced plausible-looking output while being wrong — the dangerous failure mode
here, because nothing crashes and the lights just do not quite follow the music.
Each of those tests names the bug it guards and what the symptom was.
