"""Wires the backend adapter, player, subtitle feed and link to SessionState and the views.

This is the only place that knows today's backend. U2 replaces the backend/player inputs
with desktop-session and cerebellum events (``SessionState.apply_event``); the views stay.
"""

import logging
import time
from uuid import uuid4

from PyQt5.QtCore import QObject, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QApplication

from ..contracts.lines import parse_reply, sentence_spans
from ..ui.observe import ConsentDialog
from ..ui.panel import Panel
from ..ui.session_state import SessionState
from ..ui.settings import SettingsWindow
from ..ui.together import Together
from .audio import PcmClip
from .connection import frame_glance_allowed
from .protocol import desktop_locked, sentences, visible_text, vts_running
from .timeline import CueTrack, PlaybackLevel

log = logging.getLogger(__name__)

OBSERVE_INTERVAL_MS = 305_000      # server keeps a five-minute minimum; allow for jitter
ONE_SHOT_HOLD_MS = 600_000         # server uses an observation for 10 minutes
GUARD_INTERVAL_MS = 3000


class Controller(QObject):
    def __init__(self, character, title, prefs, backend, link=None, player=None, subtitles=None,
                 rhythm_service=None, voice_service=None, log_dir=None, tray=None, version="0.2", *, device=False,
                 barge_in=None):
        super().__init__()
        self.character, self.title, self.prefs = character, title, prefs
        self.backend, self.link, self.player, self.subtitles = backend, link, player, subtitles
        self.rhythm_service, self.voice_service = rhythm_service, voice_service
        self.log_dir, self.version = log_dir, version
        self.state = SessionState(self)
        self.panel = Panel(character, title, self.state, pinned=prefs.get("pinned"))
        self.panel.on_close = self._close_requested
        self.settings = None
        self.tray = tray
        self.quitting = False
        self.ready = False
        # turns
        self.turns = {}            # turn_id -> {"text", "bubble", "resent"}
        self.active_turn = None
        self.outbox = []           # bubbles waiting for the current reply (sent while thinking)
        self.resend = []           # bubbles that never reached the server; resent once on reconnect
        # speech
        self.reading = None        # ReplyBubble being read
        self.reading_gen = None
        self.device = device
        self.avatar = backend.avatar if device else None
        self.level = PlaybackLevel()   # read by the panel avatar halo and the reply waveform
        self.panel.header.avatar.source = self.level
        self.barge_in = barge_in
        if barge_in is not None:
            barge_in.speech_started.connect(self._mic_started)
        self.shared_media = getattr(backend, "shared_media", None) if device else None
        self._media_reaction_id = self._media_segments = None
        self.utterance_id = None
        self.cue_track = None
        self.model_generation = None
        self.gestures = {}
        self.text_lines = None
        self.text_timer = QTimer(self)
        self.text_timer.setInterval(30)
        self.text_timer.timeout.connect(self._text_tick)
        # observation
        self.obs_gen = 0
        self.periodic = False
        self.server_enabled = False
        self.one_shot_pending = False   # waiting for "enabled" before the one-off observe
        self.one_shot_active = False    # a one-off look is in flight
        self.obs_timer = QTimer(self)
        self.obs_timer.setInterval(OBSERVE_INTERVAL_MS)
        self.obs_timer.timeout.connect(self._observe_periodic)
        self.one_shot_stop = QTimer(self)
        self.one_shot_stop.setSingleShot(True)
        self.one_shot_stop.timeout.connect(self._end_one_shot)
        self.guard = QTimer(self)
        self.guard.setInterval(GUARD_INTERVAL_MS)
        self.guard.timeout.connect(self._guard)
        self._wire()
        if self.shared_media is not None:
            self.shared_media.set_frame_glance(prefs.get("together_frame_glance"), store=prefs.store)
            self.shared_media.can_present = self._media_ready
            self.shared_media.reaction.connect(self._play_media_reaction)
            self.shared_media.reaction_cancelled.connect(self._cancel_media_reaction)
            if self.barge_in is not None:
                self.shared_media.state_changed.connect(self._media_mic)
                self.barge_in.speech_ended.connect(lambda: self.shared_media.set_owner_speaking(False))
        if not device:
            off = "一起听/看需要新版桌面会话：本机设置里的 server.device_session 还没打开。"
        elif self.shared_media is None:
            off = "一起听/看还没开启：本机设置里的 shared_media_enabled 是关闭的。打开后重启面板。"
        else:
            off = None
        self.together = Together(self.shared_media, self.panel, prefs, self.state, character.name, off,
                                 presented=lambda reaction_id: reaction_id == self._media_reaction_id)
        self.together.active_changed.connect(self._together_active)
        self._update_glance_settings()

    # ------------------------------------------------------------ wiring
    def _wire(self):
        p, b = self.panel, self.backend
        p.send.connect(self.send)
        p.stop.connect(self.stop)
        p.typing.connect(self._typing)
        p.reconnect_now.connect(self.reconnect_now)
        p.pin_toggled.connect(lambda on: self.prefs.set("pinned", on))
        p.open_settings.connect(self.open_settings)
        p.copy_subtitle_url.connect(self.copy_subtitle_url)
        p.quit.connect(self.quit)
        p.popover.periodic_toggled.connect(self.set_periodic)
        p.popover.autonomous_toggled.connect(self.set_autonomous)
        p.popover.look_now.connect(self.look_now)
        b.history.connect(self._history)
        b.history_failed.connect(self._history_failed)
        b.turn_accepted.connect(self._accepted)
        b.chat_done.connect(self._chat_done)
        b.chat_failed.connect(self._chat_failed)
        b.audio.connect(self._audio)
        b.speech_error.connect(self._speech_error)
        b.screen_result.connect(self._screen_result)
        if self.device:
            b.connection.connect(self._device_connection)
            b.permissions_changed.connect(self.state.set_permissions)
            b.utterance_cancelled.connect(self._utterance_cancelled)
            b.voice_revoked.connect(self._voice_revoked)
            b.pcm.connect(self._pcm)
            b.pcm_lost.connect(self._pcm_lost)
        if self.link is not None:
            b.unreachable.connect(self.link.lost)
            self.link.changed.connect(self._link_changed)
            self.link.up.connect(self._link_up)
        if self.player is not None:
            self.player.set_volume(self.prefs.get("volume"))
            self.player.started.connect(self._clip_started)
            self.player.progress.connect(self._clip_progress)
            self.player.finished.connect(self._clip_finished)
            self.player.failed.connect(self._clip_failed)
            if self.avatar is not None:
                self.player.paused.connect(self.avatar.stop_mouth)
        if self.subtitles is not None:
            self._subtitle_style()
            self.state.progressed.connect(lambda f: self.subtitles.set_state("speaking", None, f)
                                          if self.state.presence == "speaking" else None)
        self.state.changed.connect(self._state_changed)
        self.state.speech_start_timed_out.connect(self._speech_start_timeout)
        self.prefs.changed.connect(self._pref)
        if self.tray is not None:
            self.attach_tray(self.tray)

    def attach_tray(self, t):
        self.tray = t
        if t is not None:
            t.show_panel.connect(self.show_panel)
            t.stop.connect(self.stop)
            t.periodic_toggled.connect(self.set_periodic)
            t.autonomous_toggled.connect(self.set_autonomous)
            t.look_now.connect(self.look_now)
            t.together.connect(self._open_together)
            t.copy_subtitle_url.connect(self.copy_subtitle_url)
            t.pin_toggled.connect(lambda on: self.prefs.set("pinned", on))
            t.open_settings.connect(lambda: self.open_settings(0))
            t.quit.connect(self.quit)
            t.set_checks(False, False, self.prefs.get("pinned"))
            if self.device and getattr(self.backend, "stardew", None) is not None:
                action = t.menu.addAction("开启星露谷联机桥接（60 秒）")

                def update_stardew():
                    status = self.backend.stardew_status()
                    active = status["state"] in {"connecting", "active"}
                    action.setText("停止星露谷联机桥接" if active else "开启星露谷联机桥接（60 秒）")
                    action.setEnabled(active or status["authorized"])

                t.menu.aboutToShow.connect(update_stardew)
                action.triggered.connect(self._toggle_stardew)

    def _toggle_stardew(self):
        if self.backend.stardew_status()["state"] in {"connecting", "active"}:
            self.backend.stop_stardew()
        else:
            self.backend.start_stardew()

    def start(self):
        """Connected: open the screen session and load history."""
        if not self.device:
            self.state.set_connection("connected")
        self.backend.screen(self.obs_gen, "open")
        self.backend.load_history()

    # ------------------------------------------------------------ window
    def show_panel(self):
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def _close_requested(self):
        if self.quitting:
            return True
        if self.prefs.get("close_to_tray") and self.tray is not None and self.tray.icon.isVisible():
            self.panel.hide()
            if not self.prefs.get("tray_hint_shown"):
                self.prefs.set("tray_hint_shown", True)
                self.tray.notify_once(f"{self.character.name}还在托盘里。退出请用托盘菜单。")
            return False
        self.quit()
        return True

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        try:
            self.prefs.store.setValue("geometry", self.panel.saveGeometry())
            self._observation_off(notify=False)
            self.stop_speech(collapse=True)
            self.obs_gen += 1
            self.backend.close(self.obs_gen)
        finally:
            if self.tray is not None:
                self.tray.hide()
            if self.settings is not None:
                self.settings.close()
            self.panel.close()
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def open_settings(self, page=0):
        if self.settings is None:
            url = self.subtitles.item_url if self.subtitles is not None else ""
            s = SettingsWindow(self.character, self.prefs, self.state, url, self.rhythm_service,
                               self.voice_service, self.version)
            s.periodic_toggled.connect(self.set_periodic)
            s.autonomous_toggled.connect(self.set_autonomous)
            s.look_now.connect(self.look_now)
            s.reconnect.connect(self.reconnect_now)
            s.open_logs.connect(self.open_logs)
            s.copy_subtitle_url.connect(self.copy_subtitle_url)
            self.settings = s
        self.settings.set_observe_switches(self.periodic, False)
        self.settings.set_together_note(self.together.reason(ignore_pref=True))
        self._update_glance_settings()
        self.settings.show_page(page)
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def _open_together(self):
        self.show_panel()
        self.together.open()

    def _together_active(self, active):
        if self.tray is not None:
            self.tray.set_together(active, "一起看" if self.together.kind == "movie" else "一起听")

    def open_logs(self):
        if self.log_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_dir)))

    def copy_subtitle_url(self):
        if self.subtitles is None:
            self.panel.flash("offline", "字幕服务没有启动")
            return
        QApplication.clipboard().setText(self.subtitles.item_url)
        self.panel.flash("idle", "已复制字幕地址")

    def _pref(self, key, value):
        if key == "pinned":
            if self.panel.pinned != value:
                self.panel.set_pinned(value)
            if self.tray is not None:
                self.tray.set_checks(pinned=value)
        elif key == "volume" and self.player is not None:
            self.player.set_volume(value)
        elif key.startswith("subtitle_"):
            self._subtitle_style()
        elif key == "together_frame_glance" and self.shared_media is not None:
            self.shared_media.set_frame_glance(value)
        elif key == "voice_enabled" and not value:
            if self.device and (self.reading is not None or self._media_reaction_id is not None):
                self.stop()
            elif not self.device:
                self.stop_speech(collapse=True)

    def _subtitle_style(self):
        if self.subtitles is not None:
            self.subtitles.set_style(self.prefs.get("subtitle_style"), self.prefs.get("subtitle_size") / 100.0,
                                     self.prefs.get("subtitle_hold_ms"), self.prefs.get("subtitle_nameplate"))

    # ------------------------------------------------------------ state reactions
    def _state_changed(self, state):
        if state.presence == "idle" and self.reading is not None and self.reading.mode == "interrupted":
            self.reading.set_mode("stopped")
            self.reading = None
        if state.presence in ("idle", "listening", "interrupted") and self.active_turn is None:
            self._flush_outbox()

    def _typing(self, active):
        if active and self.shared_media is not None:
            self.shared_media.interrupt_reaction()
        self.state.set_typing(active)
        if active and (self.state.presence == "speaking" or (self.device and self.active_turn is None
                                                            and self.utterance_id is not None)):
            self.stop()
        if self.avatar is not None:
            self.avatar.set_typing(active)

    # ------------------------------------------------------------ history
    def _history(self, messages):
        self.panel.chat.load_history(messages, strip=(lambda text: parse_reply(text)[0]) if self.device else visible_text)
        self.ready = True

    def _history_failed(self):
        self.ready = True
        self.panel.chat.add_note("offline", "聊天记录暂时读不到，新的对话不受影响")

    # ------------------------------------------------------------ turns
    def send(self, text):
        if not self.state.can_send:
            self.panel.composer.restore_draft(text)
            return
        if self.state.presence == "speaking" or (self.device and (self.utterance_id is not None
                                                                   or self.reading_gen is not None)):
            self.stop()
        if self.active_turn is not None and (self.device or self.state.presence == "thinking"):
            bubble = self.panel.chat.add_user(text, None)
            bubble.set_receipt("queued")
            bubble.meta_label.setText("等上一条处理完再发" if self.device else "等她回答后再发")
            self.outbox.append(bubble)
            return
        bubble = self.panel.chat.add_user(text, None)
        self._dispatch(bubble)

    def _dispatch(self, bubble, resent=False):
        if self.shared_media is not None:
            self.shared_media.interrupt_reaction()
        if self.device and resent:
            turn_id = bubble.turn_id
        else:
            turn_id = uuid4().hex
        bubble.turn_id = turn_id
        bubble.at = time.time()
        bubble.set_receipt("sending")
        try:
            bubble.retry.disconnect()
        except TypeError:
            pass
        bubble.retry.connect(self._retry)
        self.turns[turn_id] = {"text": bubble.text, "bubble": bubble, "resent": resent}
        self.active_turn = turn_id
        if self.reading is not None and self.reading.mode == "interrupted":
            self.reading.set_mode("stopped")
            self.reading = None
        self.panel.chat.show_thinking()
        self.panel.chat.scroll_to_bottom()
        if self.device:
            if self.reading is not None:
                self.stop_speech()
                self.reading.set_mode("stopped")
                self.reading = None
            else:
                self._release_presentation()
        if self.avatar is not None:
            self.avatar.message_sent(turn_id)
        self.state.turn_sent(turn_id)
        self.backend.chat(bubble.text, turn_id)

    def _retry(self, bubble):
        if bubble in self.resend:
            self.resend.remove(bubble)
        if not self.state.can_send:
            bubble.set_receipt("queued")
            (self.outbox if self.device else self.resend).append(bubble)
            return
        if self.state.presence in ("thinking", "speaking") or (self.device and self.active_turn is not None):
            bubble.set_receipt("queued")
            bubble.meta_label.setText("等上一条处理完再发" if self.device else "等她回答后再发")
            self.outbox.append(bubble)
            return
        self._dispatch(bubble, resent=True)

    def _accepted(self, turn_id):
        turn = self.turns.get(turn_id)
        if turn is not None:
            turn["bubble"].set_receipt("resent" if turn["resent"] else "delivered")

    def _chat_done(self, turn_id, text):
        turn = self.turns.pop(turn_id, None)
        if turn is None or turn_id != self.active_turn:
            return
        self.active_turn = None
        if turn.get("cancel_requested"):
            self._flush_outbox()
            return
        if self.device:
            server_id, frames = self.backend.take_cues(turn_id)
            clean, _ = parse_reply(text)
            server_id = server_id or turn_id
            segs = [clean[start:end] for start, end in sentence_spans(clean)]
        else:
            clean = visible_text(text)
            segs = sentences(clean)
        if not clean:
            if self.avatar is not None:
                self.avatar.end_conversation()
            self.panel.chat.hide_thinking()
            self.state.reply_arrived(turn_id, False)
            self._flush_outbox()
            return
        bubble = self.panel.chat.add_reply(clean, segs or [clean])
        bubble.stop.connect(self.stop)
        bubble.resume.connect(self._resume)
        bubble.replay.connect(lambda b: self._resume(b, 0))
        speak = (self.player is not None and self.prefs.get("voice_enabled")
                 and (not self.device or self.backend.can_speak))
        if self.device:
            self.utterance_id = frames[0]["utterance_id"] if frames else server_id
            self.cue_track = CueTrack(frames)
            self.model_generation = self.avatar.output.generation if self.avatar is not None else None
        if speak:
            self._read(bubble, 0)
        else:
            bubble.set_mode("plain")
            if self.device:
                self._start_text(bubble.segments)
        self.state.reply_arrived(turn_id, speak)
        self._flush_outbox()

    def _chat_failed(self, turn_id, reason, partial):
        turn = self.turns.pop(turn_id, None)
        if turn is None:
            return
        if turn_id == self.active_turn:
            self.active_turn = None
            self.panel.chat.hide_thinking()
            if self.avatar is not None:
                self.avatar.end_conversation()
            self.state.turn_failed(turn_id)
        bubble = turn["bubble"]
        if turn.get("cancel_requested"):
            if reason == "cancelled":
                bubble.meta_label.setText("已取消")
        elif reason == "offline":
            # never reached the server: resend once when the link is back
            bubble.set_receipt("failed")
            if bubble not in self.resend:
                self.resend.append(bubble)
        elif reason in {"rejected", "busy", "unauthorized", "invalid_request", "invalid_config", "result_unknown"}:
            bubble.set_receipt("failed")
            detail = {"busy": "上一条还在处理", "unauthorized": "设备未获对话授权",
                      "invalid_request": "发送内容不符合要求", "invalid_config": "重发内容与原消息不一致",
                      "result_unknown": "无法确认处理结果"}.get(reason)
            if detail:
                bubble.meta_label.setText(detail)
            if reason != "result_unknown":
                self.panel.composer.restore_draft(bubble.text)
        else:  # the reply broke off midway
            bubble.set_receipt("delivered")
            clean = visible_text(partial)
            if clean:
                self.panel.chat.add_reply(clean, [clean]).set_mode("plain")
            self.panel.chat.add_note("offline", "回复中断了，可以再问一次")
        self._flush_outbox()

    def _flush_outbox(self):
        if (self.outbox and self.state.can_send and self.active_turn is None
                and self.state.presence not in ("thinking", "speaking")):
            bubble = self.outbox.pop(0)
            self._dispatch(bubble, resent=self.device and bubble.turn_id is not None)

    # ------------------------------------------------------------ speech
    def _media_ready(self):
        return (not self.quitting and not self.state.typing and self.active_turn is None and not self.outbox
                and self.state.presence == "idle" and self.reading_gen is None
                and self.utterance_id is None and (self.player is None or not self.player.active))

    def _media_mic(self, state):
        self.barge_in.monitor(state["status"] != "idle" and state.get("kind") == "movie")

    def _mic_started(self):
        if self.shared_media is not None and self.barge_in.monitoring:
            self.shared_media.set_owner_speaking(True)
            if self.state.presence == "speaking":
                self.stop()
        else:
            self.stop()

    def _play_media_reaction(self, event):
        reaction_id = event["reaction_id"]
        if not self._media_ready() or not self.shared_media.allow_audio(reaction_id):
            self.shared_media.speech_finished(reaction_id)
            return
        text = event["text"]
        self._media_segments = [text[start:end] for start, end in sentence_spans(text)] or [text]
        self._media_reaction_id = self.utterance_id = reaction_id
        self.cue_track = CueTrack(event["segments"])
        self.model_generation = self.avatar.output.generation if self.avatar is not None else None
        if self.player is not None and self.prefs.get("voice_enabled") and self.backend.can_speak:
            self.reading_gen = self.backend.enqueue_speech(self._media_segments)
        else:
            self._start_text(self._media_segments)

    def _cancel_media_reaction(self, reaction_id):
        self.together.cancel_reaction(reaction_id)   # either path may see the cancellation first
        if reaction_id == self._media_reaction_id:
            self.stop_speech()
            self.state.interrupted()

    def _media_audio_allowed(self):
        reaction_id = self._media_reaction_id
        if reaction_id is None or self.shared_media.allow_audio(reaction_id):
            return True
        self._cancel_media_reaction(reaction_id)
        return False

    def _finish_media_reaction(self):
        if self._media_reaction_id is not None:
            self.shared_media.speech_finished(self._media_reaction_id)
            self._media_reaction_id = self._media_segments = None

    def _read(self, bubble, start):
        self.reading = bubble
        if start == 0:
            bubble.spoken_until = -1
        self.reading_gen = self.backend.enqueue_speech(bubble.segments, start)

    def _resume(self, bubble, start):
        if self.player is None or not self.state.can_send:
            return
        if self.state.presence in ("thinking", "speaking"):
            return
        if self.device:
            if not self.prefs.get("voice_enabled") or not self.backend.can_speak:
                return
            self.stop_speech()
        self._read(bubble, start)
        bubble.set_mode("plain")

    def _audio(self, generation, audio_id, index, sentence, wav, mouth):
        if generation != self.backend.generation or self.player is None or not self._media_audio_allowed():
            return
        self.player.enqueue(audio_id, index, sentence, wav, mouth)

    def _pcm(self, generation, clip):
        if generation == self.backend.generation and self.player is not None and self._media_audio_allowed():
            self.player.enqueue_pcm(clip)

    def _pcm_lost(self, generation, clip):
        if generation == self.backend.generation and self.player is not None:
            if self.player.current is clip and self.barge_in is not None:
                self.barge_in.stop()
            self.player.discard_pcm(clip)

    def _clip_started(self, clip):
        if self.avatar is not None:
            self.avatar.end_conversation()
        if self.device and clip.duration_ms > 0:
            position = 0 if isinstance(clip, PcmClip) else self.player.player.position()
            self._advance_cues(clip.index, clip.text, clip.audio_id, position, clip.duration_ms)
        if self.barge_in is not None:
            self.barge_in.start()
        bubble = self.reading
        total = len(bubble.segments) if bubble is not None else 1
        if bubble is not None:
            bubble.wave.source = self.level
            bubble.set_current(clip.index)
        self.state.segment_started(clip.index, total, clip.text)
        if self.subtitles is not None:
            self.subtitles.set_state("speaking", clip.text, 0.0)
        if not self.device and clip.mouth:
            self.backend.sync("start", clip.audio_id, self.player.player.position())

    def _clip_progress(self, clip, position, fraction):
        if not self._media_audio_allowed():
            return
        self.state.segment_progress(fraction)
        if self.device:
            level = clip.level_at(position)
            self.level.set(level)
            if self.avatar is not None:
                self.avatar.mouth_level(level)
            if clip.duration_ms > 0:
                self._advance_cues(clip.index, clip.text, clip.audio_id, position, clip.duration_ms)
        elif clip.mouth:
            self.backend.sync("position", clip.audio_id, position)

    def _clip_finished(self, clip):
        if not self._media_audio_allowed():
            return
        if self.barge_in is not None:
            self.barge_in.stop()
        if self.device:
            if self.avatar is not None:
                self.avatar.stop_mouth()
            if clip.duration_ms > 0:
                self._advance_cues(clip.index, clip.text, clip.audio_id, clip.duration_ms, clip.duration_ms)
        elif clip.mouth:
            self.backend.sync("stop", clip.audio_id)
        bubble = self.reading
        if bubble is not None:
            bubble.segment_done(clip.index)
            if clip.index >= len(bubble.segments) - 1:
                bubble.current = -1
                bubble.set_mode("plain")
                self.reading = None
                self.reading_gen = None
                self._speech_done()
        elif self._media_segments is not None and clip.index >= len(self._media_segments) - 1:
            self.reading_gen = None
            self._speech_done()

    def _clip_failed(self, clip):
        segments = self.reading.segments if self.reading is not None else self._media_segments
        if isinstance(clip, PcmClip) and segments is not None and self._media_audio_allowed():
            # The local PCM output failed. Retry from this sentence on the existing WAV path.
            self.backend.stop_speech()
            self.backend.disable_pcm()
            if self.avatar is not None:
                self.avatar.stop_mouth()
            if self.barge_in is not None:
                self.barge_in.stop()
            self.player.stop()
            self.reading_gen = self.backend.enqueue_speech(segments, clip.index)
            return
        if self.device:
            self.stop_speech()
        else:
            if clip.mouth:
                self.backend.sync("stop", clip.audio_id)
            self.backend.stop_speech()
            self.player.stop()
        self._speech_failed(clip.index)

    def _speech_error(self, generation, index):
        if generation != self.reading_gen:
            return
        if self.device:
            self.stop_speech()
        self._speech_failed(index)

    def _speech_start_timeout(self):
        self.stop_speech()
        self.reading_gen = None
        self._speech_failed(0)

    def _speech_failed(self, index):
        bubble = self.reading
        if bubble is not None:
            if index <= 0 or bubble.spoken_until < 0:
                bubble.set_mode("speech_failed")
            else:
                if not self.device:
                    bubble.spoken_until = index - 1
                bubble.set_mode("interrupted")
        if self.device:
            self.reading = None
        self._speech_done()

    def _speech_done(self):
        if self.device:
            self._finish_presentation()
        self.state.speech_finished()
        if self.subtitles is not None:
            self.subtitles.set_state("idle")

    def stop_speech(self, collapse=False):
        """Stop audio now, drop queued synthesis, clear the subtitle."""
        self.backend.stop_speech()
        if self.barge_in is not None:
            self.barge_in.stop()
        if self.device:
            self.reading_gen = None
            self._release_presentation()
        clip = self.player.stop() if self.player is not None else None
        if not self.device and clip is not None and clip.mouth:
            self.backend.sync("stop", clip.audio_id)
        if self.subtitles is not None:
            self.subtitles.set_state("interrupted")
        return clip

    def stop(self):
        """Esc / stop key / tray: cancel thinking or stop speaking."""
        if self.shared_media is not None:
            self.shared_media.interrupt_reaction()
        if (self.state.presence == "thinking" or self.device) and self.active_turn is not None:
            turn_id = self.active_turn
            if self.device:
                # The host can still be running this turn after a stop request.
                turn = self.turns[turn_id]
                turn["cancel_requested"] = True
            else:
                turn = self.turns.pop(turn_id, None)
                self.active_turn = None
            outcome = self.backend.cancel(turn_id)
            self.panel.chat.hide_thinking()
            if turn is not None:
                turn["bubble"].meta_label.setText("已取消" if outcome == "cancelled" else "已取消 · 可能仍会回复")
            self.stop_speech()
            self.state.interrupted()
            self._flush_outbox()
            return
        if (self.state.presence in ("speaking", "thinking") or (self.player is not None and self.player.active)
                or (self.device and (self.utterance_id is not None or self.reading_gen is not None))):
            playing = self.reading.current if self.reading is not None else -1
            self.stop_speech()
            if self.reading is not None:
                self.reading.spoken_until = max(self.reading.spoken_until, playing)
                self.reading.set_mode("interrupted")
            self.state.interrupted()

    def _advance_cues(self, index, text, audio_id, position, duration):
        if (self.avatar is None or self.cue_track is None
                or self.avatar.output.generation != self.model_generation):
            return
        for cue, source, delay in self.cue_track.advance(index, text, audio_id, position, duration, time.monotonic()):
            log.debug("Speech cue %s %s position delay %.1f ms", cue["action"], source, delay)
            self.avatar.play_cue(cue, lambda result, cue=cue: self._cue_result(cue, result))

    def _cue_result(self, cue, result):
        task_id = cue["task_id"]
        if result.status == "accepted":
            self.gestures[task_id] = cue
        elif result.status != "running":
            self.gestures.pop(task_id, None)
            log.debug("Speech cue %s finished with %s", cue["action"], result.status)
            if self.cue_track is None and not self.gestures:
                self.utterance_id = None
                self._finish_media_reaction()

    def _finish_presentation(self):
        self.cue_track = None
        if not self.gestures:
            self.utterance_id = None
            self._finish_media_reaction()

    def _start_text(self, segments):
        if self.avatar is not None:
            self.avatar.end_conversation()
        self.text_lines = iter(enumerate(segments))
        self._next_text()
        self.text_timer.start()

    def _next_text(self):
        line = next(self.text_lines, None)
        if line is None:
            self.text_timer.stop()
            self.text_lines = None
            self._finish_presentation()
            if self.subtitles is not None:
                self.subtitles.set_state("idle")
            return
        self.text_index, self.text_line = line
        self.text_start = time.monotonic()
        self.text_duration = max(900, min(6000, 65 * len(self.text_line)))
        if self.subtitles is not None:
            self.subtitles.set_state("speaking", self.text_line, 0)
        self._advance_cues(self.text_index, self.text_line, None, 0, self.text_duration)

    def _text_tick(self):
        if self.text_lines is None or not self._media_audio_allowed():
            return
        position = min(self.text_duration, (time.monotonic() - self.text_start) * 1000)
        self._advance_cues(self.text_index, self.text_line, None, position, self.text_duration)
        if self.subtitles is not None:
            self.subtitles.set_state("speaking", None, position / self.text_duration)
        if position >= self.text_duration:
            self._next_text()

    def _release_presentation(self):
        self.text_timer.stop()
        if self.text_lines is not None and self.subtitles is not None:
            self.subtitles.set_state("interrupted")
        self.text_lines = self.cue_track = self.utterance_id = None
        self._finish_media_reaction()
        if self.avatar is not None:
            self.avatar.end_conversation()
            for cue in self.gestures.values():
                self.avatar.cancel(cue, "cancelled")
        self.gestures.clear()

    def _utterance_cancelled(self, utterance_id):
        if utterance_id == self.utterance_id:
            self.stop()

    def _voice_revoked(self):
        if self.reading_gen is not None:
            self.stop()

    def _device_connection(self, event):
        self.state.apply_event(event)
        if self.active_turn is not None:
            # The session reconciles pending sends and accepted turns after reconnect.
            turn = self.turns[self.active_turn]
            if (event["state"] == "connected" and self.state.presence != "thinking"
                    and not turn.get("cancel_requested")):
                if self.avatar is not None:
                    self.avatar.message_sent(self.active_turn)
                self.state.turn_sent(self.active_turn)
        elif event["state"] != "connected" and not self.quitting:
            self.stop()

    # ------------------------------------------------------------ link
    def _link_changed(self, state, attempt, next_ms):
        if not self.device:
            self.state.set_connection(state, attempt, next_ms)
        if state == "offline":
            if self.state.presence != "idle" or self.reading is not None:
                self.stop_speech()
            self.server_enabled = False

    def _link_up(self, base):
        self.backend.set_base(base)
        if self.shared_media is not None:
            self.shared_media.set_frame_glance(self.prefs.get("together_frame_glance"))
        self._update_glance_settings()
        stamp = time.strftime("%H:%M")
        self.panel.chat.add_note("idle", f"{stamp} 已重新连上")
        self.panel.flash("idle", f"已重新连上 · {stamp}")
        if self.periodic:
            self.obs_gen += 1
            self.backend.screen(self.obs_gen, "start")
        pending, self.resend = self.resend, []
        for bubble in pending:
            if bubble is pending[0] and self.state.presence != "thinking":
                self._dispatch(bubble, resent=True)
            else:
                bubble.set_receipt("queued")
                bubble.meta_label.setText("等她回答后再发")
                self.outbox.append(bubble)

    def _update_glance_settings(self):
        base = getattr(self.backend, "base", "")
        allowed = not base or frame_glance_allowed(base + getattr(self.backend, "api", ""))
        self.together.set_frame_glance_available(allowed)
        if self.settings is not None:
            self.settings.set_frame_glance_available(allowed)

    def reconnect_now(self):
        if self.device:
            self.backend.reconnect_session()
        if self.link is not None:
            self.link.connect_now()

    # ------------------------------------------------------------ observation
    def _sync_switches(self):
        self.panel.popover.set_switches(self.periodic, False)
        if self.settings is not None:
            self.settings.set_observe_switches(self.periodic, False)
        if self.tray is not None:
            self.tray.set_checks(periodic=self.periodic, autonomous=False)
        self.state.set_observe(periodic=self.periodic)

    def set_periodic(self, on):
        if on == self.periodic:
            self._sync_switches()
            return
        if on and not self.prefs.get("consent_periodic"):
            self._sync_switches()   # stays off until consent
            self._ask_consent("periodic")
            return
        self._apply_periodic(on)

    def set_autonomous(self, on):
        # Today's server has no autonomous observation (P5); keep the switch off.
        self._sync_switches()

    def _ask_consent(self, kind):
        dialog = ConsentDialog(self.panel, self.character, kind)
        dialog.decided.connect(self._consent)
        self.panel.popover.hide()
        self.show_panel()
        dialog.open()

    def _consent(self, kind, accepted):
        if accepted and kind == "periodic":
            self.prefs.set("consent_periodic", True)
            self._apply_periodic(True)
        else:
            self._sync_switches()

    def _apply_periodic(self, on):
        self.periodic = on
        self.obs_timer.stop()
        if on:
            self.obs_gen += 1
            self.backend.screen(self.obs_gen, "start")
            self.guard.start()
        elif not self.one_shot_stop.isActive():
            # (a recent one-off look stays usable until its hold expires)
            self.obs_gen += 1
            self.backend.screen(self.obs_gen, "stop")
            self.guard.stop()
            self.server_enabled = False
        self._sync_switches()

    def look_now(self):
        if not self.state.can_send:
            self.panel.flash("offline", "连上以后才能看")
            return
        if self.one_shot_active:
            return
        self.one_shot_active = True
        self.panel.popover.set_busy(True)
        self.state.set_observe(capturing=True)
        self.panel.chat.add_note("observing", f"{self.character.name}正在看当前窗口 · 只看这一次")
        if self.server_enabled:
            self.backend.screen(self.obs_gen, "observe")
        else:
            self.one_shot_pending = True
            self.obs_gen += 1
            self.backend.screen(self.obs_gen, "start")
        self.guard.start()

    def _observe_periodic(self):
        if self.periodic and self.server_enabled:
            self.state.set_observe(capturing=True)
            self.backend.screen(self.obs_gen, "observe")

    def _end_one_shot(self):
        if not self.periodic:
            self.obs_gen += 1
            self.backend.screen(self.obs_gen, "stop")
            self.server_enabled = False
            self.guard.stop()

    def _screen_result(self, generation, result):
        if generation != self.obs_gen:
            return
        state = result.get("state") if isinstance(result, dict) else "error"
        if state == "enabled":
            self.server_enabled = True
            if self.periodic:
                self.obs_timer.start()
            if self.one_shot_pending:
                self.one_shot_pending = False
                self.backend.screen(self.obs_gen, "observe")
            return
        if state == "disabled":
            self.server_enabled = False
            return
        was_one_shot, self.one_shot_active = self.one_shot_active, False
        self.panel.popover.set_busy(False)
        self.state.set_observe(capturing=False)
        if state in ("observed", "skipped", "waiting"):
            self.panel.popover.add_result(state)
            if was_one_shot:
                text = {"observed": "看过了当前窗口", "skipped": "这个窗口被跳过了",
                        "waiting": "刚看过，5 分钟内不会再看"}[state]
                self.panel.chat.add_note("observing" if state == "observed" else "interrupted", text)
                if not self.periodic:
                    self.one_shot_stop.start(ONE_SHOT_HOLD_MS)
            return
        if state == "cancelled":
            return
        # error
        self.panel.popover.add_result("failed")
        self.one_shot_pending = False
        if was_one_shot:
            self.panel.chat.add_note("offline", "这次没看成，识屏暂不可用")
        if self.periodic or self.server_enabled:
            self._observation_off(reason="识屏暂不可用，已关闭观察")

    def _observation_off(self, reason=None, notify=True):
        was = self.periodic or self.server_enabled or self.one_shot_pending
        self.periodic = False
        self.one_shot_pending = self.one_shot_active = False
        self.obs_timer.stop()
        self.one_shot_stop.stop()
        self.guard.stop()
        if was:
            self.obs_gen += 1
            self.backend.screen(self.obs_gen, "stop")
        self.server_enabled = False
        self.state.set_observe(periodic=False, capturing=False)
        self.panel.popover.set_busy(False)
        self._sync_switches()
        if notify and reason and was:
            self.panel.chat.add_note("interrupted", reason)

    def _guard(self):
        if not (self.periodic or self.server_enabled or self.one_shot_pending):
            self.guard.stop()
            return
        if desktop_locked():
            self._observation_off(reason="电脑已锁屏，观察已关闭")
        elif not vts_running():
            self._observation_off(reason="VTube Studio 已退出，观察已关闭")
