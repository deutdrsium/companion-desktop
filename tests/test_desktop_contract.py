import json
import unittest
from pathlib import Path

from companion_desktop.contracts.types import PROTOCOL
from companion_desktop.contracts.wire import ProtocolError, decode, encode, message, negotiate
from companion_desktop.session.client import InMemorySession


class ContractTests(unittest.TestCase):
    def test_p0_replay(self):
        path = Path(__file__).parent / "fixtures" / "contract" / "replay.json"
        replay = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(negotiate(["other.v1", replay["protocol"]]), PROTOCOL)
        for case in replay["cases"]:
            with self.subTest(case=case["name"]):
                items = [(kind, case[kind]) for kind in ("intent", "cancel") if kind in case]
                items.extend(("receipt", body) for body in case["receipts"])
                for kind, body in items:
                    frame = message(kind, replay["session"], body)
                    self.assertEqual(decode(encode(frame)), frame)

    def test_reject_unknown_version_and_completion_without_evidence(self):
        with self.assertRaisesRegex(ProtocolError, "unsupported_protocol"):
            negotiate(["other.v1"])
        ref = {"device_id": "dev-demo", "desktop_session_id": "desk-demo", "generation": 1}
        body = {"task_id": "task-demo", "status": "completed", "reason_code": None,
                "evidence": None, "occurred_at": "2026-01-01T00:00:00Z"}
        with self.assertRaisesRegex(ProtocolError, "invalid_body"):
            message("receipt", ref, body)

    def test_in_memory_ui_exercises_retry_resend_and_cancel(self):
        session = InMemorySession(capabilities={"subtitle": True})
        events = []
        session.subscribe(events.append)
        session.connect()
        session.send_message("msg-demo", "Hello")
        accepted = events[-1]
        session.reconnecting(attempt=2, next_retry_ms=2000)
        self.assertEqual(events[-1]["next_retry_ms"], 2000)
        session.connect()
        session.send_message("msg-demo", "Hello")
        self.assertEqual(events[-1], accepted)
        session.reply(accepted["turn_id"], "Example", seq=1)
        self.assertEqual(events[-1]["type"], "turn.text")
        session.cancel_turn(accepted["turn_id"])
        self.assertEqual(events[-1]["status"], "cancelled")
        session.cancel_turn("turn-demo-missing")
        self.assertEqual(events[-1]["status"], "unknown")
