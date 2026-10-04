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

## Layout

```
config/
  settings.yaml      runtime: driver, port, audio device, jukebox URL
  rig.yaml           THE PATCH: what exists and where it answers
  profiles/*.yaml    per-model channel maps
partylights/
  dmx/               drivers + the universe and its 40 Hz writer thread
  fixtures/          colour, profiles, patch
tools/
  dmx_sweep.py       Phase 0 hardware and profile verification
```

## Tests

```bash
./.venv/bin/python -m pytest
```

Covers colour conversion, channel-map arithmetic, address collision detection,
and a regression guard on the PAR mode channel staying at zero.
