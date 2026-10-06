"""Scenes: looks assembled from layers.

Each is a colour field, a stack of masks and an accent role -- a few lines
rather than a file of animation code. Every move reads fixture positions from
the stage view, so the same scene works whether the PARs end up in a line on a
wall, a U around the lawn, or two banks facing each other: drag them in /viz
and watch it follow.

Notes from designing for an outdoor yard with no haze: the light only shows
where it lands, so contrast between neighbours (lit next to dark, orange next
to purple) does the work that visible beams do indoors.
"""

from __future__ import annotations

from ..layers import (Accent, Alternate, Breathe, Gradient, Knockout, Noise, Pulse,
                      Ripple, SidePulse, Solid, Split, Travel)
from .layered import LayeredLook, Stack


class RotorScene(LayeredLook):
    name = "rotor"
    description = "Two heads spinning round the centre of the rig, colour wheel underneath"
    needs_tempo = True
    release_s = 0.18
    floor = 0.06

    def build(self):
        return Stack(
            colour=Gradient(axis="angle", spread=0.5, drift_bars=16),
            masks=[Travel(axis="angle", beats=8, width=0.09, pingpong=False, wrap=True,
                          heads=2)],
            accent=Accent(punch="snare"),
        )


class SweepScene(LayeredLook):
    name = "sweep"
    description = "A wave front washing left to right and back, once a bar"
    needs_tempo = True
    release_s = 0.20
    floor = 0.05

    def build(self):
        return Stack(
            colour=Gradient(axis="along", spread=0.35, drift_bars=8),
            masks=[Travel(axis="along", beats=8, width=0.16, pingpong=True)],
            accent=Accent(punch="snare"),
        )


class RippleScene(LayeredLook):
    name = "ripple"
    description = "Kicks send rings out from the centre and back, two colours split"
    release_s = 0.15

    def build(self):
        return Stack(
            colour=Split(axis="side", bars=8),
            masks=[Pulse(region="kick", decay_s=0.6, floor=0.10, mode="mul"),
                   Ripple(region="kick", travel_s=0.45, width=0.18)],
            accent=Accent(punch="snare"),
        )


class PingPongScene(LayeredLook):
    name = "pingpong"
    description = "The two halves of the rig trade every two beats, punching on kicks"
    needs_tempo = True
    release_s = 0.12

    def build(self):
        return Stack(
            colour=Split(axis="side", bars=8, swap_bars=4),
            masks=[Alternate(axis="side", beats=2, low=0.10),
                   Pulse(region="kick", decay_s=0.35, floor=0.55, downbeat=0.2)],
            accent=Accent(offset=0, punch="snare"),
        )


class CallResponseScene(LayeredLook):
    name = "callresponse"
    description = "Left half answers the kick, right half the snare"
    release_s = 0.15

    def build(self):
        return Stack(
            colour=Split(axis="side", bars=8),
            masks=[SidePulse(regions=("kick", "snare"), decay_s=0.4, floor=0.12)],
            accent=Accent(uv=0.25),
        )


class StepperScene(LayeredLook):
    name = "stepper"
    description = "Marquee: every third fixture lit, stepping on the beat"
    needs_tempo = True
    release_s = 0.10
    floor = 0.03

    def build(self):
        return Stack(
            colour=Gradient(axis="path", spread=0.5, drift_bars=8, blend=False),
            masks=[Alternate(axis="path", groups=3, beats=1, low=0.05, interleave=True),
                   Pulse(region="kick", decay_s=0.3, floor=0.6)],
            accent=Accent(punch="snare"),
        )


class KnockoutScene(LayeredLook):
    name = "knockout"
    description = "Room lit full, every kick cuts it dark — hard edges, great in fog"
    release_s = 0.05
    floor = 0.0
    peak = 0.85

    def build(self):
        return Stack(
            colour=Split(axis="side", bars=8, swap_bars=2),
            masks=[Breathe(beats=16, low=0.8, axis=None),
                   Knockout(region="kick", depth=0.9, decay_s=0.16)],
            accent=Accent(offset=1, bed=0.2, uv=0.3),
        )


class DriftScene(LayeredLook):
    name = "drift"
    description = "Slow drifting colour clouds, breathing with the music — no hits"
    release_s = 0.6
    floor = 0.12

    def build(self):
        return Stack(
            colour=Noise(scale=2.2, speed=0.06, spread=1.4),
            masks=[Breathe(beats=32, low=0.35)],
            accent=Accent(bed=0.2, uv=0.35, uv_bass=0.3),
        )


class BeamsScene(LayeredLook):
    name = "beams"
    description = "Everything snaps on with the kick and straight off — for foggy nights"
    release_s = 0.05
    floor = 0.0

    def build(self):
        return Stack(
            colour=Solid(bars=4),
            masks=[Pulse(region="kick", decay_s=0.14, floor=0.0, downbeat=0.25)],
            accent=Accent(offset=1, bed=0.0, uv=0.2, punch="snare", punch_decay_s=0.15),
        )


SCENES = (RotorScene, SweepScene, RippleScene, PingPongScene, CallResponseScene,
          StepperScene, KnockoutScene, DriftScene, BeamsScene)
