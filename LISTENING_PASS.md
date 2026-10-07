# The listening pass

Weeks before the party, the Mac plays every song on the party playlist once,
silently, and writes down everything a lighting designer would want to know
about it: where every beat and bar falls, where the verses and choruses are,
when the drop lands, where the band stops dead for a beat, how the song ends.
That record is the song's **map**.

Each song's light show is then designed against its map ahead of time. At the
party, when a guest's song comes on, the rig recognises where it is in the
song to within a few milliseconds and plays the show that was written for it.
To the room it looks like someone is busking the lights live, and nailing every
moment of the song.

---

## Running it

### Once, before the first run

1. **Spotify app settings** (Settings → Playback):
   - **Crossfade: off.** Crossfading would blend each song's ending into the
     next, and the map would get a fade that isn't really in the song.
   - **Autoplay: off.** The pass copes if it's on, but off is cleaner.
   - **Normalize volume:** whatever you'll use at the party. Same either way is fine.
2. **The jukebox queue must be empty.** The jukebox can stay running, but if a
   song were waiting it would grab Spotify to play it. The check below tells you.
3. **Sound output → BlackHole 2ch** for a silent pass: Option-click the volume
   icon in the menu bar and pick it. Choose *Multi-Output Device* instead if you
   want to hear it as it goes. Put it back afterwards, because at the party the
   output needs to be the Multi-Output Device.
4. **Power and lid:** plugged in, with the lid open. The screen can turn off, and
   the pass keeps the Mac from sleeping on its own.

### Then

```bash
cd ~/Desktop/workSpace/party-lights

# Checks only: Spotify login, the Mac's Spotify app, the jukebox queue, sound output.
./.venv/bin/python -m partylights.cli listen --check

# A short trial first: three songs, about ten minutes.
./.venv/bin/python -m partylights.cli listen "https://open.spotify.com/playlist/…" --limit 3

# The real thing. Leave it running overnight.
./.venv/bin/python -m partylights.cli listen "https://open.spotify.com/playlist/…"

# Later nights: no link needed, it carries on where it stopped.
./.venv/bin/python -m partylights.cli listen

# How far along it is, and anything that failed.
./.venv/bin/python -m partylights.cli listen --status
```

You can stop it with **Ctrl+C** at any time. It pauses Spotify, and running the
same command again carries on from where it stopped, with at most one song
replayed. Re-running the command with the playlist link also picks up songs
added to the playlist since the last run.

It plays in real time, so about **3.5 minutes a song**: roughly 29 hours for
500 songs, or three nights.

To read what it heard in a song:

```bash
./.venv/bin/python -m partylights.cli songmap show "test song"
```

```
Test Song — Synth
  118.0 BPM  ·  quality 0.91
  0:00.49  intro     A   8 bars  energy 0.00
  0:16.76  verse     B  16 bars  energy 0.71
  0:49.29  chorus    C  16 bars  energy 0.98
  …
  3:11.69  SLAM (+34 dB low end)
  3:09.65  gap, 3.9 beats of silence
  3:32.03  final hit, rings 1.1 s
```

(That one is a synthetic test song from `tools/make_test_song.py`, not a real track.)

---

## What it does, song by song

1. **Arms the recorder** on BlackHole, which hears exactly what Spotify plays.
2. **Tells Spotify to play the track from 0:00** on this Mac's Spotify app.
3. **Watches it every 1.5 s.** Each reading of Spotify's position is stamped on
   the same clock as the recording. Each one is a vote for where 0:00 fell in
   the recording, and the median of ~150 votes pins it down even though any
   single reading wobbles by a tenth of a second or so. From then on, every time
   in the map is *Spotify's song time*. That matches what the jukebox reports
   at the party, which is what the live rig needs.
4. **Stops a moment after the end** and pauses Spotify.
5. **Checks the take**, and plays the song again (up to three times) if:
   - the audio driver lost any samples, or the recording's clock slipped.
     One lost block shifts everything after it, so this is never accepted;
   - something else played, playback paused, or the position jumped (a skip or seek);
   - an advert played.

   If the recording is **silent**, the whole pass stops and says so, because
   that means the Mac's sound isn't reaching BlackHole, and every song would fail.
6. **Analyses** the take, which takes under a second.
7. **Saves** the map, the fingerprint, the raw features, and the album art.

---

## What it's listening for

Everything below is measured relative to the song itself (its own loud level,
its own low end), because a quiet acoustic track and a brick-walled EDM master
both have choruses and drops, at very different absolute levels.

### Timing: every beat and every bar

- **Tempo** from the whole song at once, scored by how well the full metrical
  grid fits, with a gentle preference for a danceable 120 BPM to break
  half/double-time ties. If the kick hits between the beats as hard as on them
  (drum & bass at 174 read as 87), the tempo is doubled.
- **Beats** by dynamic programming (Ellis 2007): each beat chosen knowing where
  the next one lands. The onset signal is weighted towards kick and snare,
  because hi-hats sit *between* beats and would otherwise drag the beat onto
  the offbeat.
- **Grid fitting:** party music is cut to a fixed tempo grid, and even a live
  band holds tempo over a few bars, so each beat is replaced by its place on a
  robust line through the beats around it.
- **Quiet stretches** (an intro of pads, a breakdown) have nothing on the beat
  to track. There the grid is carried through from the well-marked beats on
  either side, as a listener would.
- **Bars:** each beat is beat 1, 2, 3 or 4 of its bar. The one is where the bass
  hits and the chords change. A phase change mid-song (a 2/4 bar before a
  chorus) is allowed only where the evidence is strong.
- **Beat accents:** how hard the kick, the snare and the hats hit on each beat,
  so a design can accent where the snare actually is.

### Sections: what part of the song this is

- Every bar is described by its harmony (chroma), timbre (MFCCs), band levels
  and drum activity, then compared with every other bar.
- A **boundary** is where the past stops resembling the future (Foote novelty),
  combined with jumps in level. Boundaries prefer 4- and 8-bar phrase lines,
  because that's where songwriters put them.
- **Labels:** sections that match bar for bar, *in order*, are the same part
  (A, B, C…). A verse matches a verse by its chord sequence, not just its
  average sound.
- **Roles:** a quiet opening is the intro and a quiet close the outro. A dip
  between louder parts is a breakdown. The loudest repeated part is the chorus,
  the most repeated other part the verse, and a one-off is the bridge. Sections
  that build into a drop, and the drops themselves, are named from what's heard (below).

### Moments: the instants a room feels

| Moment | What it is | What a show might do with it |
|---|---|---|
| **Drop** | The low end slams back in on a downbeat after a build | Blackout through the gap, everything on the one |
| **Slam** | A drop with no build: the band stops and crashes back in | A hit, without the tension before it |
| **Build** | The bars rising into a drop (low end held back, risers and snare rolls climbing) | Tighten, speed up, wash to white |
| **Gap** | The music stops dead for a beat or more, then comes back | Cut to black, exactly as long as the silence |
| **Lift** | A section arriving noticeably bigger (a chorus kicking in) | Change look on the downbeat |
| **Hit** | A stab or impact out of a quieter moment | A flash on that one hit |
| **Ending** | A hard final hit (and how long it rings) or a fade-out (and when it starts) | Blackout on the last hit, or a long fade with the song |

Each moment is snapped onto the exact hit that makes it, and carries a strength,
so a design can tell a real event from a ripple.

### Quality: how far to trust the map

Each map gets a score from 0 to 1, built from tempo confidence, downbeat
confidence and timing steadiness, plus plain-English notes ("downbeats
uncertain", "loose timing"). A high score means designing bold, timed moments.
A low score (a rubato ballad, a live recording) means the design leans on
the rig's live reactions instead, with the song's colours and character.

---

## How accurate it is

Measured against synthetic songs with known structure (`tools/make_test_song.py`).
`tests/test_songmap.py` holds these numbers in place:

| | |
|---|---|
| Tempo | within 0.1 BPM at 96, 118, 128, 150 and 174 BPM |
| Beats | 99–100% within 30 ms, typically ~1 ms off (96% on a fade-out, whose last bars are too quiet to call) |
| Bars (downbeats) | 97–100% correct |
| Section boundaries | every one found, to the bar |
| Drops | within 5 ms |
| Gaps | start within 30 ms |
| Final hit | within 5 ms; fade-out start within 0.3 s |
| Live re-alignment, on two real recordings | within 5 ms in every trial, even with no hint and with the audio filtered and turned down |

Synthetic songs are cleaner than real ones. On two real recordings, the beats
land on the drum hits, and real timing wobbles 12–14 ms, which the grid
fitting absorbs. The first real nights of the pass are the real test: I'll go
through the maps from the trial run, compare them against the songs, and tune
anything that's off. Because the raw features are kept, every map can then be
rebuilt in minutes without replaying anything:

```bash
./.venv/bin/python -m partylights.cli songmap reanalyse
```

**Known limits.**
- Songs with no steady pulse (rubato ballads, spoken intros) get low quality
  scores rather than wrong confident ones.
- It can't hear vocals as such, so "the moment the singer comes in" is only
  found when the band changes with it.
- In loop-built tracks whose bars repeat *exactly*, the live alignment can be a
  bar off between identical bars. The live rig handles this (see below).

---

## How it's stored

```
songs/
  playlist.json              the track list, as fetched
  pass-log.jsonl             one line per take: ok, or why not
  maps/<track id>.json       the map (~20 KB)
  fingerprints/<id>.npz      what the live rig matches against (~35 KB)
  frames/<id>.npz            raw features, for re-analysis (~0.9 MB)
  art/<album id>.jpg         640 px album art, for designing
```

- A **map** is plain JSON. It holds the track's details (name, artists,
  album, release date, album art) and the capture's evidence (how many takes,
  how tightly the position votes agreed). It also holds the timing (every
  beat, which beats are downbeats), the sections, the moments, the per-beat
  accents and the quality score. All times are in seconds of Spotify song time.
- The **fingerprint** is 8 channels (when things hit in the low, mid and high
  range, plus band levels) at 50 frames a second, stored as bytes.
- The **frames** are the full analysis features at 100 frames a second: 64-band
  spectrum, the seven light bands, chroma, onsets and level, stored as bytes.
  They're about 450 MB for 500 songs, and are kept out of git (see `.gitignore`).
  **Back up `songs/` after each night.** The frames can only be recreated by
  replaying the songs.
- **No audio is kept.** The recording exists in memory just long enough to be
  analysed, and you can't turn the stored features back into music.

---

## How it will be used at the party

This part isn't built yet; it's the next step after the pass. The plan:

1. **A song starts.** The jukebox already tells the lights the track ID. The
   rig loads that song's map, fingerprint and design.
2. **The rig finds its place.**
   - Spotify's position gives a first guess, good to a few tenths of a second.
   - The rig then keeps the last few seconds of what it hears, describes them
     the way the pass did, and slides that along the song's fingerprint until
     they line up. The pass heard the song through the identical path (Spotify →
     BlackHole → the same analysis), so they match closely, to within a few
     milliseconds on real music.
   - The rig only commits once consecutive windows agree. After that its own
     last lock is the guess, and it re-checks every couple of seconds, so a
     pause, skip or seek is caught and corrected within a moment.
   - Section changes, which is where the big cues are, are also what break any
     tie between identical loop bars.
3. **Cues are scheduled ahead, not reacted to.** Because the rig knows what's
   coming, it can start a cue *before* the moment: blackout on the beat of the
   gap, build the tension through the riser, hit everything on the exact
   frame of the drop, already compensated for the delay to the fixtures
   (`audio.output_delay_ms`). This is the difference from today's reactive rig,
   which can only respond after it hears something.
4. **The vibe slider** shifts each song's designed baseline, so leaving it in
   the middle plays every song exactly as designed.
5. **Fallbacks, so nothing ever looks broken:**
   - **Not mapped yet?** Use the artist's design if one of their other songs is
     mapped, else colours from the album art, else today's auto mode.
   - **Lock lost or unsure?** The timed cues hold off and the live engine
     carries on reacting, still in the song's designed colours and character.
   - **Rig problem?** Exactly as today: the jukebox and the music are never
     affected by the lights.

---

## Spotify rules to know about (as of October 2026)

- This app can only read the songs in playlists **you own**. Spotify's
  February 2026 rules return "forbidden" for followed playlists, so the party
  playlist should be one you created (or one this app creates).
- Spotify no longer gives artist genres to apps like this one. Genre and mood
  for each design come from knowing the music instead.
- Listening history over the API covers only the last 50 plays. Older history
  (like last Halloween) is only in Spotify's privacy data export:
  spotify.com → Account → Privacy settings → *Extended streaming history*.
