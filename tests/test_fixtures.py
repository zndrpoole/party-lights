"""Tests for the fixture layer: colour conversion, channel maps, patching.

These are the parts where a silent error costs an evening of staring at lights,
so they get covered properly: an off-by-one in a channel map or an address
collision is invisible until the rig is up.
"""

import pytest

from partylights.fixtures.color import (
    AMBER_RGB, Emission, gamma_byte, hsv, mix, split_amber, split_white,
)
from partylights.fixtures.patch import Patch, PatchError, PatchedFixture
from partylights.fixtures.profile import ChannelSpec, FixtureProfile, ProfileError
from partylights.config import DEFAULT_RIG, load_patch


# -- colour -----------------------------------------------------------------

def test_gamma_byte_endpoints_are_exact():
    assert gamma_byte(0.0) == 0
    assert gamma_byte(1.0) == 255


def test_gamma_byte_is_monotonic_and_perceptually_shaped():
    vals = [gamma_byte(i / 20) for i in range(21)]
    assert vals == sorted(vals)
    # Half level should sit well below half byte value: that is the whole point
    # of gamma encoding, and it is what makes a fade look even.
    assert gamma_byte(0.5) < 128


def test_split_white_extracts_the_neutral_component():
    residual, w = split_white((0.8, 0.8, 0.8))
    assert w == pytest.approx(0.8)
    assert residual == pytest.approx((0.0, 0.0, 0.0))


def test_split_white_leaves_saturated_colour_alone():
    residual, w = split_white((1.0, 0.0, 0.0))
    assert w == 0.0
    assert residual == (1.0, 0.0, 0.0)


def test_split_white_bias_scales_extraction():
    _, full = split_white((0.5, 0.5, 0.5), bias=1.0)
    _, half = split_white((0.5, 0.5, 0.5), bias=0.5)
    assert half == pytest.approx(full * 0.5)


def test_split_amber_claims_a_warm_colour():
    residual, a = split_amber(AMBER_RGB)
    assert a == pytest.approx(1.0)
    assert residual[0] == pytest.approx(0.0)
    assert residual[1] == pytest.approx(0.0)


def test_split_amber_ignores_cool_colour():
    _, a = split_amber((0.0, 0.0, 1.0))
    assert a == 0.0


def test_mix_endpoints():
    a, b = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    assert mix(a, b, 0.0) == a
    assert mix(a, b, 1.0) == b
    assert mix(a, b, 0.5) == pytest.approx((0.5, 0.0, 0.5))


def test_hsv_wraps_hue_rather_than_clamping():
    # A chase that walks hue past 1.0 must not stick at red.
    assert hsv(1.25) == pytest.approx(hsv(0.25))


def test_emission_blend_crossfades_every_field():
    a = Emission(rgb=(1, 0, 0), intensity=1.0, uv=0.0)
    b = Emission(rgb=(0, 0, 1), intensity=0.0, uv=1.0)
    mid = a.blended(b, 0.5)
    assert mid.rgb == pytest.approx((0.5, 0.0, 0.5))
    assert mid.intensity == pytest.approx(0.5)
    assert mid.uv == pytest.approx(0.5)


# -- profiles ---------------------------------------------------------------

@pytest.fixture
def rgb_profile():
    return FixtureProfile(
        model="TEST-RGB", footprint=4,
        channels=[ChannelSpec(0, "dimmer"), ChannelSpec(1, "red"),
                  ChannelSpec(2, "green"), ChannelSpec(3, "blue")],
    )


def test_profile_rejects_offset_beyond_footprint():
    with pytest.raises(ProfileError, match="exceeds footprint"):
        FixtureProfile(model="X", footprint=2, channels=[ChannelSpec(5, "red")])


def test_profile_rejects_duplicate_role():
    with pytest.raises(ProfileError, match="declared twice"):
        FixtureProfile(model="X", footprint=3,
                       channels=[ChannelSpec(0, "red"), ChannelSpec(1, "red")])


def test_profile_rejects_unknown_role():
    with pytest.raises(ProfileError, match="unknown channel role"):
        ChannelSpec(0, "sparkle")


def test_render_length_always_matches_footprint(rgb_profile):
    assert len(rgb_profile.render(Emission())) == 4


def test_render_puts_colour_on_the_right_channels(rgb_profile):
    out = rgb_profile.render(Emission(rgb=(1, 0, 0)))
    assert out[1] == 255 and out[2] == 0 and out[3] == 0


def test_dimmer_channel_keeps_colour_at_full_resolution(rgb_profile):
    """With a dimmer channel, fading must not scale the colour channels.

    Scaling colour to fade loses hue resolution exactly where it shows most --
    at the bottom of a fade.
    """
    out = rgb_profile.render(Emission(rgb=(1, 0, 0), intensity=0.25))
    assert out[1] == 255
    assert 0 < out[0] < 255


def test_intensity_folds_into_colour_when_there_is_no_dimmer():
    p = FixtureProfile(model="NODIM", footprint=3,
                       channels=[ChannelSpec(0, "red"), ChannelSpec(1, "green"),
                                 ChannelSpec(2, "blue")])
    full = p.render(Emission(rgb=(1, 0, 0), intensity=1.0))
    half = p.render(Emission(rgb=(1, 0, 0), intensity=0.5))
    assert half[0] < full[0]


def test_fixed_channels_are_asserted_every_frame():
    """The mode channel is the whole reason `fixed` exists.

    Above 10 these fixtures run an internal program and ignore our colour, so
    the constant must appear in every rendered frame -- not just at startup.
    """
    p = FixtureProfile(model="MODE", footprint=3,
                       channels=[ChannelSpec(0, "red"),
                                 ChannelSpec(1, "fixed", value=0),
                                 ChannelSpec(2, "fixed", value=17)])
    for em in (Emission(), Emission(rgb=(1, 1, 1)), Emission(strobe=1.0, uv=1.0)):
        out = p.render(em)
        assert out[1] == 0
        assert out[2] == 17


def test_strobe_open_value_when_not_strobing():
    p = FixtureProfile(model="S", footprint=2,
                       channels=[ChannelSpec(0, "red"),
                                 ChannelSpec(1, "strobe", open_value=3,
                                             min_rate=8, max_rate=255)])
    assert p.render(Emission(rgb=(1, 0, 0), strobe=0.0))[1] == 3
    assert p.render(Emission(rgb=(1, 0, 0), strobe=1.0))[1] == 255
    mid = p.render(Emission(rgb=(1, 0, 0), strobe=0.5))[1]
    assert 8 <= mid <= 255


def test_uv_is_never_derived_from_rgb():
    """UV sits outside the visible gamut, so white must not light it up."""
    p = FixtureProfile(model="UV", footprint=4,
                       channels=[ChannelSpec(0, "red"), ChannelSpec(1, "green"),
                                 ChannelSpec(2, "blue"), ChannelSpec(3, "uv")])
    assert p.render(Emission(rgb=(1, 1, 1)))[3] == 0
    assert p.render(Emission(rgb=(0, 0, 0), uv=1.0))[3] == 255


# -- patch ------------------------------------------------------------------

def _fixture(fid, profile, address, position=0):
    return PatchedFixture(fid=fid, label=fid, profile=profile,
                          address=address, position=position)


def test_patch_detects_address_collision(rgb_profile):
    with pytest.raises(PatchError, match="claimed by both"):
        Patch([_fixture("a", rgb_profile, 1), _fixture("b", rgb_profile, 3)])


def test_patch_allows_adjacent_addresses(rgb_profile):
    patch = Patch([_fixture("a", rgb_profile, 1), _fixture("b", rgb_profile, 5)])
    assert patch.max_channel == 8


def test_patch_rejects_duplicate_ids(rgb_profile):
    with pytest.raises(PatchError, match="duplicate fixture id"):
        Patch([_fixture("a", rgb_profile, 1), _fixture("a", rgb_profile, 5)])


def test_patch_rejects_running_past_the_universe(rgb_profile):
    with pytest.raises(PatchError, match="outside 1"):
        Patch([_fixture("a", rgb_profile, 510)])


# -- the real rig -----------------------------------------------------------

def test_real_rig_loads_and_matches_the_documented_address_map():
    patch = load_patch(DEFAULT_RIG)
    assert len(patch) == 13
    assert patch.max_channel == 94
    expected = [1, 8, 15, 22, 29, 36, 43, 50, 57, 64, 71, 78]
    assert [patch.by_id[f"par{i + 1}"].address for i in range(12)] == expected
    assert patch.by_id["accent"].address == 85


def test_real_rig_pins_the_par_mode_channel_to_zero():
    """Regression guard on the single most consequential value in the rig."""
    patch = load_patch(DEFAULT_RIG)
    out = patch.by_id["par1"].profile.render(Emission(rgb=(1, 1, 1), intensity=1.0))
    assert out[5] == 0, "PAR mode channel must stay 0 or the fixture ignores DMX colour"


def test_real_rig_accent_has_the_emitters_the_pars_lack():
    patch = load_patch(DEFAULT_RIG)
    assert patch.by_id["accent"].profile.has_uv
    assert not patch.by_id["par1"].profile.has_uv
    assert set(patch.by_id["accent"].profile.emitters) >= {"red", "green", "blue",
                                                           "white", "amber", "uv"}
