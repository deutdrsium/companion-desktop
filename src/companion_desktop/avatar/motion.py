"""Local pose synthesis in Cubism output coordinates. All times are in seconds."""

from math import cos, expm1, pi, sin
from random import Random


FRAME_PERIOD = 1 / 30
STEP = 1 / 120
PAUSE_GAP = 0.5

NEUTRAL = {
    "head_yaw": 0.0, "head_pitch": 0.0, "head_roll": 0.0,
    "body_yaw": 0.0, "body_roll": 0.0, "breath": 0.0,
    "look_x": 0.0, "look_y": 0.0, "eye_left": 1.0, "eye_right": 1.0,
    "brow_left": 0.0, "brow_right": 0.0, "eye_smile": 0.0,
    "mouth_form": 0.0, "mouth_open": 0.0,
}

THINKING = {
    "head_roll": -5.5, "look_x": -0.63, "look_y": 0.37,
    "brow_left": -0.46, "brow_right": 0.20, "mouth_form": -0.52,
}
LISTENING = {"brow_left": 0.38, "brow_right": 0.34, "head_roll": 4.5, "head_pitch": 2.0}
FOLLOW_SECONDS = 2.5  # how long gaze keeps following after the pointer stops
LAUGH = {"eye_left": 0.72, "eye_right": 0.72, "eye_smile": 0.85, "mouth_form": 0.87}
GESTURE_SECONDS = {"hold_laugh": 2.8, "nod": 0.65, "head_tilt": 1.05}


def unit(value):
    return max(0.0, min(1.0, value))


def ease(value):
    value = unit(value)
    return value * value * (3 - 2 * value)


def follow(current, target, speed, seconds):
    return current + (target - current) * -expm1(-speed * seconds)


class Motion:
    """Idle and life state form the base. Overlays own only the listed channels.

    Gaze reflexes override conversational gaze. Gesture offsets can coexist
    with idle head motion. Blink multiplies the composed lids last, and only
    the local playback envelope writes mouth opening.
    """

    def __init__(self, *, seed=None):
        self._random = Random(seed)
        self.time = 0.0
        self._remaining = 0.0
        self.values = NEUTRAL.copy()
        self._life = (0.0, 0.0, 0.0)  # reading, fatigue, happiness
        self.role_state = None
        self.typing = False
        self.thinking = False
        self.reply_smile_until = 0.0
        self.mouth_target = 0.0
        self._gaze = (0.0, 0.0)
        self._weight_shift = 0.0
        self._next_gaze = self._delay(1.0, 1.4)
        self._next_shift = self._delay(25.0, 30.0)
        self._blink_start = self._delay(1.5, 2.0)
        self._blink_length = 0.22
        self._lid = 1.0
        self._reflex = None
        self._last_pointer = None
        self._follow = None  # (position, seen_at) of the latest desktop pointer
        self._next_pointer = 0.0
        self._next_click = 0.0

    def _delay(self, minimum, mean):
        return minimum + self._random.expovariate(1 / mean)

    def pointer(self, position):
        """Accept mapped model-window coordinates in [-1, 1], or None outside it."""
        previous, self._last_pointer = self._last_pointer, position
        if position is not None and position != previous:
            self._follow = (position, self.time)
        if position is None or previous is None or self.time < self._next_pointer:
            return
        if max(abs(a - b) for a, b in zip(previous, position)) >= 0.18:
            self._next_pointer = self.time + self._delay(3.5, 1.8)
            self._reflex = (self.time, 0.65, tuple(0.42 * (2 * unit((n + 1) / 2) - 1) for n in position), 0.10, 0.20)

    def click(self):
        if self.time < self._next_click:
            return
        self._next_click = self.time + 1.4
        self._next_pointer = self.time + 1.0
        # A click is a soft glance back, not a snap.
        self._reflex = (self.time, 1.4, (0.0, 0.06), 0.30, 0.45)

    def blink(self):
        self._blink_start = self.time
        self._blink_length = 0.22 + 0.14 * self._life[1]

    def stop_mouth(self):
        self.mouth_target = 0.0
        self.values["mouth_open"] = 0.0

    def clear_reflexes(self):
        self.typing = self.thinking = False
        self.reply_smile_until = 0.0
        self._reflex = self._last_pointer = None
        self.stop_mouth()

    def reset_pose(self):
        self.values = NEUTRAL.copy()
        self._lid = 1.0
        self._blink_start = self.time + self._delay(1.5, 2.0)
        self._next_click = self._next_pointer = self.time

    def advance(self, elapsed, now, gestures):
        if elapsed >= PAUSE_GAP:
            self._remaining = 0.0
            self._reflex = self._last_pointer = None
            self.reply_smile_until = 0.0
            self._blink_start = self.time + self._delay(1.5, 2.0)
            self.stop_mouth()
            return self._frame()

        reading, fatigue, happiness = (0.0, 0.0, 0.0)
        if self.role_state is not None:
            body, deadline = self.role_state
            if now >= deadline:
                self.role_state = None
            else:
                reading = float(body["activity"] in {"read", "reading", "阅读", "读书"})
                energy = body["energy"]
                fatigue = max(body["sleepiness"] or 0.0, 0.0 if energy is None else 1 - energy)
                if body["emotion"] in {"sleepy", "困倦"}:
                    fatigue = max(fatigue, 0.75)
                happiness = float(body["emotion"] in {"happy", "joy", "开心"})

        self._remaining += elapsed
        # Clock samples accumulate fractional steps. A tiny tolerance keeps
        # equivalent nominal frame times on the same side of the step boundary.
        while self._remaining + 1e-9 >= STEP:
            self._remaining -= STEP
            self.time += STEP
            self._step((reading, fatigue, happiness), now - self._remaining, gestures)

        return self._frame()

    def _frame(self):
        result = self.values.copy()
        for channel in ("eye_left", "eye_right"):
            result[channel] *= self._lid
        return result

    def _step(self, state, now, gestures):
        self._life = tuple(follow(old, new, 2.0, STEP) for old, new in zip(self._life, state))
        reading, fatigue, happiness = self._life
        t = self.time
        if t >= self._next_gaze:
            self._gaze = (self._random.uniform(-0.16, 0.16), self._random.uniform(-0.07, 0.07))
            self._next_gaze = t + self._delay(1.2 + 1.5 * reading, 1.6)
        if t >= self._next_shift:
            self._weight_shift = self._random.uniform(-0.9, 0.9)
            self._next_shift = t + self._delay(25.0, 30.0)
        if t >= self._blink_start + self._blink_length:
            self._blink_start = t + self._delay(1.7, 2.0)
            self._blink_length = 0.22 + 0.14 * fatigue
        phase = (t - self._blink_start) / self._blink_length
        if 0 <= phase < 1:
            self._lid = 1 - ease(phase / 0.30) if phase < 0.30 else ease((phase - 0.50) / 0.50)
        else:
            self._lid = follow(self._lid, 1.0, 22.0, STEP)

        sway = (1 + 0.35 * happiness) * (1 - 0.34 * fatigue)
        targets = {
            "head_yaw": sway * (1.3 * sin(0.67 * t) + 0.36 * sin(1.07 * t + 0.8)),
            "head_pitch": 0.7 * sin(0.50 * t + 2.1) - 2.4 * reading - 1.5 * fatigue,
            "head_roll": 0.52 * sin(0.43 * t + 0.6) + 0.3 * self._weight_shift,
            "body_yaw": 0.78 * sway * sin(0.37 * t + 2.0) + self._weight_shift,
            "body_roll": 0.45 * sway * sin(0.29 * t + 0.5) + 0.5 * self._weight_shift,
            "breath": 0.50 - 0.42 * cos(1.38 * t),
            "look_x": self._gaze[0], "look_y": self._gaze[1] - 0.39 * reading - 0.12 * fatigue,
            "eye_left": 1 - 0.22 * fatigue, "eye_right": 1 - 0.22 * fatigue,
            "brow_left": -0.14 * fatigue, "brow_right": -0.14 * fatigue,
            "eye_smile": 0.17 * happiness, "mouth_form": 0.26 * happiness,
            "mouth_open": self.mouth_target,
        }
        if self._follow is not None and t - self._follow[1] < FOLLOW_SECONDS and not self.thinking:
            (px, py), seen = self._follow
            weight = ease((FOLLOW_SECONDS - (t - seen)) / 0.8)
            targets["look_x"] += weight * (0.75 * px - targets["look_x"])
            targets["look_y"] += weight * (-0.6 * py - targets["look_y"])
            targets["head_yaw"] += weight * 9.0 * px
            targets["head_pitch"] += weight * -5.0 * py
        if self.thinking:
            targets.update(THINKING)
            targets["eye_left"] *= 0.97
            targets["eye_right"] *= 0.97
        elif self.typing:
            targets.update(LISTENING)

        laugh = gestures.get("hold_laugh")
        strength = 0.0
        if laugh is not None:
            age = now - laugh.started
            strength = laugh.strength * ease(age / 0.16) * ease((laugh.duration - age) / 0.45)
        elif self.reply_smile_until > now:
            left = self.reply_smile_until - now
            strength = 0.75 * ease((GESTURE_SECONDS["hold_laugh"] - left) / 0.16) * ease(left / 0.45)
        for channel, pose in LAUGH.items():
            targets[channel] += strength * (pose - targets[channel])
        nod = gestures.get("nod")
        if nod is not None:
            phase = unit((now - nod.started) / nod.duration)
            targets["head_pitch"] -= 8.0 * nod.strength * sin(phase * pi) ** 2

        tilt = gestures.get("head_tilt")
        if tilt is not None:
            phase = unit((now - tilt.started) / tilt.duration)
            targets["head_roll"] += 7.0 * tilt.strength * sin(phase * pi) ** 2

        if self._reflex is not None:
            start, duration, gaze, rise, fall = self._reflex
            age = t - start
            if age >= duration:
                self._reflex = None
            else:
                weight = ease(age / rise) * ease((duration - age) / fall)
                for channel, goal in zip(("look_x", "look_y"), gaze):
                    targets[channel] += weight * (goal - targets[channel])

        for channel, goal in targets.items():
            speed = 8.0
            if channel.startswith("look_"):
                speed = 12.0 if self._reflex is not None and self._reflex[3] > 0.2 else 23.0
            elif channel.startswith("body_"):
                speed = 3.0
            elif channel.startswith("head_"):
                speed = 11.0 if nod is not None and channel == "head_pitch" else 7.0 - 2 * fatigue
            elif channel == "mouth_open":
                speed = 32.0 if goal > self.values[channel] else 22.0
            self.values[channel] = follow(self.values[channel], goal, speed, STEP)
