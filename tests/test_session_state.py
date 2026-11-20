from companion_desktop.ui.session_state import SessionState


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(qapp):
    clock = Clock()
    s = SessionState(clock=clock)
    seen = []
    s.changed.connect(lambda st: seen.append(st.state))
    return s, clock, seen


def test_connecting_then_idle_listening_thinking_speaking(qapp):
    s, clock, seen = make(qapp)
    assert s.state == "connecting" and not s.can_send
    s.set_connection("connected")
    assert s.state == "idle" and s.can_send and not s.busy
    s.set_typing(True)
    assert s.state == "listening"
    s.turn_sent("t1")
    assert s.state == "thinking" and s.busy
    assert s.status_text() == "在想…"
    clock.t += 4
    assert s.status_text() == "在想 · 已等 4 秒"
    assert not s.slow
    clock.t += 20
    assert s.slow
    s.reply_arrived("t1", will_speak=True)
    assert s.state == "thinking"  # until the first clip actually plays
    s.segment_started(0, 2, "第一句。")
    assert s.state == "speaking" and s.busy
    s.speech_finished()
    assert s.state == "idle"
    assert seen[0] == "idle"


def test_reply_without_speech_and_stale_reply(qapp):
    s, _, _ = make(qapp)
    s.set_connection("connected")
    s.turn_sent("t1")
    s.reply_arrived("other", will_speak=False)
    assert s.state == "thinking"
    s.reply_arrived("t1", will_speak=False)
    assert s.state == "idle"


def test_interrupt_settles_back_to_idle(qapp):
    s, _, _ = make(qapp)
    s.set_connection("connected")
    s.segment_started(0, 1, "x")
    s.interrupted()
    assert s.state == "interrupted" and not s.busy
    s._settle.timeout.emit()  # what the 1.5 s timer does
    assert s.state == "idle"


def test_thinking_always_exits(qapp):
    s, _, _ = make(qapp)
    s.set_connection("connected")
    s.turn_sent("t1")
    s.reply_arrived("t1", will_speak=True)
    assert s._grace.isActive()
    s._grace.timeout.emit()
    assert s.state == "idle"
    s.turn_sent("t2")
    s.turn_failed("t2")
    assert s.state == "idle"


def test_offline_and_observing_take_precedence(qapp):
    s, clock, _ = make(qapp)
    s.set_connection("connected")
    s.set_observe(capturing=True)
    assert s.state == "observing" and s.status_text() == "正在看当前窗口"
    s.turn_sent("t1")
    s.set_connection("offline", attempt=3, next_retry_ms=8000)
    assert s.state == "offline" and not s.can_send
    assert s.presence == "idle" and not s.capturing
    assert s.retry_in_s == 8
    clock.t += 3
    assert s.retry_in_s == 5
    assert s.status_text() == "连接断开 · 正在重连"


def test_progress_does_not_emit_changed(qapp):
    s, _, seen = make(qapp)
    s.set_connection("connected")
    s.segment_started(0, 1, "x")
    n = len(seen)
    got = []
    s.progressed.connect(got.append)
    s.segment_progress(0.5)
    assert got == [0.5] and len(seen) == n


def test_apply_event_seam_for_u2(qapp):
    s, _, _ = make(qapp)
    s.apply_event({"type": "connection", "state": "connected"})
    assert s.state == "idle"
    s.apply_event({"type": "presence", "state": "thinking"})
    assert s.state == "thinking"
    s.apply_event({"type": "segment.start", "index": 1, "total": 3, "text": "b"})
    assert s.state == "speaking" and s.segment_index == 1
    s.apply_event({"type": "utterance.interrupted"})
    assert s.state == "interrupted"
    s.apply_event({"type": "connection", "state": "reconnecting", "attempt": 2, "next_retry_ms": 4000})
    assert s.state == "offline" and s.attempt == 2
    s.apply_event({"type": "something.new"})  # ignored
    assert s.state == "offline"
