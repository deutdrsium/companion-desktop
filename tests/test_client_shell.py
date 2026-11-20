"""Protocol helpers, subtitle feed, external-command services, link backoff."""

import json
from types import SimpleNamespace as NS
from urllib.request import urlopen

import pytest

from companion_desktop.client import config as config_mod
from companion_desktop.client.connection import BACKOFF_S, Link, TunnelError
from companion_desktop.client.protocol import read_sse, sentence_spans, sentences, visible_text
from companion_desktop.client.services import RhythmService, VoiceKeyService
from companion_desktop.client.subtitle_server import SubtitleServer


def test_sentences_and_tags():
    assert sentences("你好！一起学习。") == ["你好！", "一起学习。"]
    assert sentences('你好。<soullink>{"emotion":"happy"}</soullink><wardrobe>{}</wardrobe><l2d:微笑>') == ["你好。"]
    clean = "第一句。第二句！\n第三句"
    assert "".join(clean[s:e] for s, e in sentence_spans(clean)) == clean
    assert len(sentences("a" * 2500)) == 3
    assert visible_text("看<tts>不读</tts>这里") == "看这里"
    assert list(read_sse([b'data: {"type":"plain","data":"ok"}', b"", b"data: "])) == [{"type": "plain", "data": "ok"}]


def test_subtitle_feed_states_and_clear_on_stop(tmp_path):
    server = SubtitleServer(tmp_path / "token", port=0, name="测试")
    try:
        page = urlopen(server.item_url).read().decode("utf-8")
        assert server.state_url in page and '"测试"' in page and "ENDPOINT_JSON" not in page
        server.set_state("speaking", "第一句。", 0.2)
        data = json.loads(urlopen(server.state_url).read())
        assert data["state"] == "speaking" and data["text"] == "第一句。" and data["seq"] == 1
        server.set_state("speaking", "第一句。", 0.5)
        assert server.snapshot()["seq"] == 1 and server.snapshot()["progress"] == 0.5
        server.set_state("interrupted")
        snap = server.snapshot()
        assert snap["state"] == "interrupted" and snap["text"] == "" and snap["progress"] == 0
        server.set_style("outline", 1.2, 2500, False)
        snap = server.snapshot()
        assert (snap["style"], snap["size"], snap["hold_ms"], snap["nameplate"]) == ("outline", 1.2, 2500, False)
        with pytest.raises(Exception):
            urlopen(server.item_url.replace(server.item_url.split("/")[3], "0" * 48))
    finally:
        server.close()


def test_rhythm_service_contract():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        out = {"scope": "desktop-window", "applied": True,
               "before": {"private_debounce_min_ms": 600, "private_debounce_max_ms": 1500,
                          "private_debounce_total_ms": 4000},
               "after": {"private_debounce_min_ms": 300, "private_debounce_max_ms": 1000,
                         "private_debounce_total_ms": 3000}}
        return NS(returncode=0, stdout=("noise\n" + json.dumps(out)).encode())
    svc = RhythmService({"argv": ["tool", "run {args}"]}, runner=runner)
    assert svc.load()["private_debounce_min_ms"] == 600
    assert calls[-1] == ["tool", "run"]
    assert svc.save((300, 1000, 3000))["private_debounce_total_ms"] == 3000
    assert calls[-1][-1] == "run --apply --min-ms 300 --max-ms 1000 --total-ms 3000"
    with pytest.raises(ValueError):
        svc.save((2000, 1000, 3000))
    svc.reset()
    assert calls[-1][-1] == "run --reset"


def test_voice_key_goes_over_stdin_only():
    seen = {}

    def runner(argv, input=None, **kw):
        seen["argv"], seen["input"] = argv, input
        return NS(returncode=0, stdout=b'{"ok": true}')
    svc = VoiceKeyService({"argv": ["setup"]}, runner=runner)
    assert svc.submit(" sk-test ")
    assert seen["input"] == b"sk-test" and "sk-test" not in " ".join(seen["argv"])
    assert not svc.submit("bad\nkey")


def test_config_lookup_and_validation(tmp_path, monkeypatch):
    p = tmp_path / "client.json"
    p.write_text(json.dumps({"character": {"name": "甲"}, "server": {"base_url": "http://x"}}), encoding="utf-8")
    cfg = config_mod.load(p)
    assert cfg["character"]["name"] == "甲" and cfg["server"]["chat_path"] == "/api/v1/chat"
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate(cfg)
    monkeypatch.setenv("COMPANION_DESKTOP_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(config_mod.ConfigError):
        config_mod.load()


def test_example_config_has_only_placeholders():
    text = (config_mod.REPO_ROOT / "config" / "client.example.json").read_text(encoding="utf-8")
    cfg = json.loads(text)
    assert cfg["server"]["ssh_target"].endswith(".example")
    assert set(cfg["account"]["session_id"]) == {"0"}


class FakeProcess:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -1


def test_link_reconnects_with_backoff(qapp):
    opened = []
    ok = {"probe": True, "tunnel": True}

    def factory(server):
        if not ok["tunnel"]:
            raise TunnelError("refused")
        proc = FakeProcess()
        opened.append(proc)
        return proc, 4000 + len(opened)

    def probe(base):
        if not ok["probe"]:
            raise RuntimeError("down")
    link = Link({"ssh_target": "user@server.example"}, probe, tunnel_factory=factory)
    events, ups = [], []
    link.changed.connect(lambda s, a, n: events.append((s, a, n)))
    link.up.connect(ups.append)
    base, proc = link.start_blocking()
    link.adopt(base, proc)
    assert base == "http://127.0.0.1:4001"
    proc.code = 255  # tunnel died
    link._watch()
    assert events[-1] == ("offline", 1, BACKOFF_S[0] * 1000) and link.retry.isActive()
    ok["tunnel"] = False
    link._finished(False, "", None)   # what a failed attempt reports
    link.attempt = 1
    link._schedule()
    assert events[-1] == ("offline", 2, BACKOFF_S[1] * 1000)
    link._finished(True, "http://127.0.0.1:4002", FakeProcess())
    assert link.state == "connected" and ups == ["http://127.0.0.1:4002"] and events[-1][0] == "connected"
    link.close()


def test_direct_base_url_needs_no_tunnel(qapp):
    link = Link({"base_url": "http://127.0.0.1:6185/"}, lambda b: None,
                tunnel_factory=lambda s: (_ for _ in ()).throw(AssertionError("no tunnel")))
    assert link.start_blocking() == ("http://127.0.0.1:6185", None)
