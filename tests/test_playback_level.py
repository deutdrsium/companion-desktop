"""U2: the panel halo and reply waveform follow the real playback level."""

from companion_desktop.client.timeline import PlaybackLevel


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms / 1000.0


def test_level_is_unavailable_until_playback_reports_one():
    assert PlaybackLevel(Clock()).get() is None


def test_level_holds_then_decays_to_idle_and_clamps():
    clock = Clock()
    level = PlaybackLevel(clock)
    level.set(0.8)
    assert level.get() == 0.8
    clock.advance(PlaybackLevel.HOLD_MS)
    assert level.get() == 0.8
    clock.advance(PlaybackLevel.DECAY_MS / 2)
    assert 0.35 < level.get() < 0.45
    clock.advance(PlaybackLevel.DECAY_MS)
    assert level.get() == 0.0
    level.set(3)
    assert level.get() == 1.0
    level.set(-1)
    assert level.get() == 0.0


class Source:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def test_avatar_halo_uses_live_level_and_falls_back_to_placeholder(qapp):
    from companion_desktop.ui.paint import Character, StateAvatar

    avatar = StateAvatar(Character("测"), "speaking", 46)
    avatar.tick(0)
    placeholder = avatar.level
    assert placeholder > 0
    avatar.source = Source(None)
    avatar.tick(0)
    assert avatar.level == placeholder
    avatar.source = Source(0.9)
    avatar.tick(0)
    assert avatar.level == 0.9
    avatar.source.value = 0.0   # silence / decayed: idle halo, not the sine
    avatar.tick(500)
    assert avatar.level == 0.0
    avatar.grab()


def test_waveform_scrolls_live_level_and_falls_back_to_placeholder(qapp):
    from companion_desktop.ui.paint import Waveform

    wave = Waveform(bars=4)
    wave.tick(0)
    placeholder = list(wave.levels)
    wave.source = Source(None)
    wave.tick(0)
    assert wave.levels == placeholder
    wave.source = Source(0.7)
    wave.tick(0)
    wave.source.value = 0.2
    wave.tick(33)
    assert wave.levels[-2:] == [0.7, 0.2]
    wave.grab()
