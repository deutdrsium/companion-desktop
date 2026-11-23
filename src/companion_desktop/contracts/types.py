"""Public desktop session types."""

from typing import Any, Literal, TypedDict

PROTOCOL = "companion.desktop.v1"
RECEIPT_STATUSES = (
    "queued", "accepted", "running", "completed", "rejected", "failed",
    "cancelled", "expired", "unknown", "unsupported",
)
TERMINAL_STATUSES = frozenset(RECEIPT_STATUSES[3:])
PERMISSIONS = ("avatar", "game", "observe", "speak")


class SessionRef(TypedDict):
    device_id: str
    desktop_session_id: str
    generation: int


class Message(TypedDict):
    protocol: str
    type: str
    session: SessionRef
    body: dict[str, Any]


class _RequiredCapabilities(TypedDict):
    revision: int
    model_id: str | None
    asset_version: str | None
    avatar: dict[str, list]
    observations: list[str]
    games: list[dict[str, Any]]


class Capabilities(_RequiredCapabilities, total=False):
    tts: bool
    mouth: bool
    subtitle: bool


class Connection(TypedDict):
    state: Literal["connecting", "connected", "reconnecting", "closed"]
    attempt: int
    next_retry_ms: int | None
    generation: int | None
    reason: str | None


class ObservationState(TypedDict):
    request_id: str
    source: Literal["periodic", "autonomous", "user"]
    phase: Literal["requested", "capturing", "done"]
    result: Literal["observed", "skipped", "unchanged", "unavailable", "failed", "cancelled"] | None
    captured_at: str | None
    license_generation: int


class Evidence(TypedDict):
    source: str
    ref: str


class Receipt(TypedDict):
    task_id: str
    status: str
    reason_code: str | None
    evidence: Evidence | None
    occurred_at: str
