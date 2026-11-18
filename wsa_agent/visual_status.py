"""Observable visual-review status state machine and same-process monotonic timing tracker."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple


_PROVIDER_SHORT_NAMES = {
    "claude": "Claude",
    "gemini": "Gemini",
    "chatgpt": "ChatGPT",
    "deepseek": "DeepSeek",
    "venice": "Venice",
}

_STAGE_ORDER = {
    "idle": 0,
    "capturing": 1,
    "captured": 2,
    "queued": 3,
    "dispatched": 4,
    "submitted": 5,
    "generating": 6,
    "completed": 7,
    "cancelled": 7,
    "failed": 7,
}


def short_provider_name(model_id: str) -> str:
    """Return concise human provider name for compact TUI status lines."""
    raw = str(model_id or "").strip().lower()
    for key, label in _PROVIDER_SHORT_NAMES.items():
        if raw == key or raw.startswith(key):
            return label
    return str(model_id or "Provider").strip().capitalize() or "Provider"


def format_duration_ms(ms: Optional[float]) -> str:
    """Format a measured millisecond duration concisely for TUI display."""
    if ms is None:
        return ""
    try:
        val = float(ms)
    except (TypeError, ValueError):
        return ""
    if val < 0:
        return ""
    secs = val / 1000.0
    if secs < 1.0:
        return f"{int(round(val))}ms"
    if secs < 10.0:
        return f"{secs:.1f}s"
    return f"{int(round(secs))}s"


def format_live_elapsed(seconds: float) -> str:
    """Format live elapsed wait seconds without jittering layout width."""
    secs = max(0.0, float(seconds))
    if secs < 10.0:
        return f"{secs:.1f}s"
    return f"{int(secs)}s"


def describe_capture_viewports(args: Optional[Dict[str, Any]] = None) -> str:
    """Return a concise capture status label matching requested viewports."""
    a = args if isinstance(args, dict) else {}
    vps = a.get("viewports")
    if isinstance(vps, list) and vps:
        names = [str(v.get("name") or "").strip().lower() for v in vps if isinstance(v, dict)]
        names = [n for n in names if n]
        if len(names) == 1:
            return f"Capturing {names[0]} view…"
        if len(names) == 2:
            return f"Capturing {names[0]} and {names[1]} views…"
        if len(names) > 2:
            return f"Capturing {len(names)} viewport views…"
    return "Capturing desktop and mobile views…"


class VisualReviewTracker:
    """Tracks observable visual-review stages and honest same-process timings.

    Strict invariants:
    - Never claims 'uploaded' unless the extension explicitly confirmed `images_attached=True`.
    - Never claims 'waiting for response' until submission is confirmed.
    - Never claims 'passed' without explicit passing verdict evidence.
    - Ignores late/stale events from prior requests, previous reviewers, or closed turns.
    - Stores zero image data, account emails, or conversation URLs.
    """

    def __init__(
        self,
        active_provider: str,
        *,
        delegated: bool = False,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self.active_provider: str = str(active_provider or "").strip().lower() or "gemini"
        self.reviewer_provider: str = self.active_provider
        self.delegated: bool = bool(delegated)
        self.active_request_id: str = ""
        self.stage: str = "idle"
        self.closed: bool = False

        self._capture_start: Optional[float] = None
        self._req_start: Optional[float] = None
        self._stage_rank: int = 0

        self.capture_ms: Optional[int] = None
        self.browser_upload_ms: Optional[int] = None
        self.relay_pickup_ms: Optional[int] = None
        self.relay_attach_ms: Optional[int] = None
        self.provider_response_ms: Optional[int] = None
        self.round_trip_ms: Optional[int] = None

        self.upload_confirmed: bool = False
        self.submit_confirmed: bool = False
        self.outcome: str = ""
        self.defects_count: int = 0
        self.error_kind: str = ""
        self.attempted_reviewers: List[str] = []

    def begin_capture(self, args: Optional[Dict[str, Any]] = None) -> str:
        self.closed = False
        self.stage = "capturing"
        self._stage_rank = _STAGE_ORDER["capturing"]
        self._capture_start = self._clock()
        return describe_capture_viewports(args)

    def end_capture(self, *, ok: bool = True) -> str:
        if self._capture_start is not None:
            elapsed_ms = max(0, int(round((self._clock() - self._capture_start) * 1000.0)))
            self.capture_ms = elapsed_ms
            self._capture_start = None
        if not ok:
            self.stage = "failed"
            self.outcome = "capture_failed"
            self.closed = True
            return "Visual capture failed"
        self.stage = "captured"
        self._stage_rank = _STAGE_ORDER["captured"]
        return "Screenshot ready"

    def set_capture_ms(self, capture_ms: Optional[int]) -> None:
        if capture_ms is not None and capture_ms >= 0:
            self.capture_ms = int(capture_ms)

    def begin_review(
        self,
        request_id: str,
        reviewer: str,
        *,
        fallback_from: str = "",
    ) -> str:
        """Start tracking a direct or delegated review request (clears prior attempt transient state)."""
        self.closed = False
        self.active_request_id = str(request_id or "").strip()
        self.reviewer_provider = str(reviewer or self.active_provider).strip().lower()
        if self.reviewer_provider not in self.attempted_reviewers:
            self.attempted_reviewers.append(self.reviewer_provider)
        self.stage = "queued"
        self._stage_rank = _STAGE_ORDER["queued"]
        self._req_start = self._clock()
        # Reset per-attempt relay timings so a fallback reviewer never inherits stale values.
        self.browser_upload_ms = None
        self.relay_pickup_ms = None
        self.relay_attach_ms = None
        self.provider_response_ms = None
        self.round_trip_ms = None
        self.upload_confirmed = False
        self.submit_confirmed = False
        self.error_kind = ""

        rev_lbl = short_provider_name(self.reviewer_provider)
        act_lbl = short_provider_name(self.active_provider)
        if self.delegated:
            if fallback_from:
                prev_lbl = short_provider_name(fallback_from)
                return f"{prev_lbl} unavailable · {rev_lbl} is reviewing for {act_lbl}"
            return f"{act_lbl} requested visual review · {rev_lbl} is reviewing"
        return f"Screenshot ready · Sending to {rev_lbl}"

    def on_relay_event(self, event: Dict[str, Any]) -> Optional[Tuple[str, bool]]:
        """Process a relay progress event; return `(status_text, with_elapsed)` or None if ignored."""
        if self.closed or not isinstance(event, dict):
            return None
        ev_cid = str(event.get("client_request_id") or "").strip()
        if self.active_request_id and ev_cid and ev_cid != self.active_request_id:
            return None
        ev_prov = str(event.get("provider") or "").strip().lower()
        if ev_prov and self.reviewer_provider and not (
            ev_prov == self.reviewer_provider
            or ev_prov.startswith(self.reviewer_provider)
            or self.reviewer_provider.startswith(ev_prov)
        ):
            return None

        stage = str(event.get("stage") or "").strip().lower()
        if stage not in _STAGE_ORDER:
            return None
        rank = _STAGE_ORDER[stage]
        # Reject out-of-order regressive events from stale polls.
        if rank < self._stage_rank:
            return None
        self._stage_rank = rank
        self.stage = stage

        # Merge any genuinely measured same-process timing fields present on the event.
        if isinstance(event.get("upload_ms"), (int, float)) and event["upload_ms"] >= 0:
            self.browser_upload_ms = int(round(event["upload_ms"]))
        if isinstance(event.get("relay_pickup_ms"), (int, float)) and event["relay_pickup_ms"] >= 0:
            self.relay_pickup_ms = int(round(event["relay_pickup_ms"]))
        if isinstance(event.get("relay_attach_ms"), (int, float)) and event["relay_attach_ms"] >= 0:
            self.relay_attach_ms = int(round(event["relay_attach_ms"]))

        rev_lbl = short_provider_name(self.reviewer_provider)
        act_lbl = short_provider_name(self.active_provider)

        if stage == "queued":
            if self.delegated:
                return (f"{act_lbl} requested visual review · Sending to {rev_lbl}", True)
            return (f"Screenshot ready · Sending to {rev_lbl}", True)

        if stage == "dispatched":
            # Dispatched means the extension picked up the request, NOT that upload succeeded yet.
            if self.delegated:
                return (f"{act_lbl} requested visual review · {rev_lbl} tab preparing", True)
            return (f"Screenshot sent to relay · Waiting on {rev_lbl}", True)

        if stage == "submitted":
            self.submit_confirmed = True
            attached_flag = event.get("images_attached")
            if attached_flag is True:
                self.upload_confirmed = True
                if self.delegated:
                    return (f"Screenshot uploaded · Waiting for {rev_lbl}", True)
                return (f"Screenshot uploaded · Waiting for {rev_lbl}", True)
            # Older extension or unconfirmed image attachment: never claim "uploaded".
            if self.delegated:
                return (f"Submitted to {rev_lbl} · Waiting for response", True)
            return (f"Submitted to {rev_lbl} · Waiting for response", True)

        if stage == "generating":
            if self.delegated:
                return (f"{rev_lbl} is reviewing screenshot · for {act_lbl}", True)
            return (f"{rev_lbl} is reviewing screenshot", True)

        return None

    def record_server_timings(self, timings: Optional[Dict[str, Any]]) -> None:
        """Merge sanitized server/browser timing measurements when available."""
        if not isinstance(timings, dict):
            return
        if isinstance(timings.get("upload_ms"), (int, float)) and timings["upload_ms"] >= 0:
            self.browser_upload_ms = int(round(timings["upload_ms"]))
            if timings.get("images_attached") is True:
                self.upload_confirmed = True
        if isinstance(timings.get("relay_pickup_ms"), (int, float)) and timings["relay_pickup_ms"] >= 0:
            self.relay_pickup_ms = int(round(timings["relay_pickup_ms"]))
        if isinstance(timings.get("relay_attach_ms"), (int, float)) and timings["relay_attach_ms"] >= 0:
            self.relay_attach_ms = int(round(timings["relay_attach_ms"]))
        if isinstance(timings.get("response_ms"), (int, float)) and timings["response_ms"] >= 0:
            self.provider_response_ms = int(round(timings["response_ms"]))
            self.submit_confirmed = True
        if isinstance(timings.get("round_trip_ms"), (int, float)) and timings["round_trip_ms"] >= 0:
            self.round_trip_ms = int(round(timings["round_trip_ms"]))
        if timings.get("images_attached") is True:
            self.upload_confirmed = True
        if timings.get("submit_confirmed") is True:
            self.submit_confirmed = True

    def finish(
        self,
        outcome: str,
        *,
        defects_count: int = 0,
        error_kind: str = "",
        timings: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Finalize the active visual review and return a concise final status summary."""
        if timings:
            self.record_server_timings(timings)
        if self._req_start is not None and self.round_trip_ms is None:
            self.round_trip_ms = max(0, int(round((self._clock() - self._req_start) * 1000.0)))
        self._req_start = None
        self.outcome = str(outcome or "reviewed")
        self.defects_count = max(0, int(defects_count or 0))
        self.error_kind = str(error_kind or "").strip()
        self.closed = True
        self.stage = "cancelled" if self.outcome == "cancelled" else (
            "failed" if self.outcome in ("transport_error", "capture_failed", "unavailable") else "completed"
        )
        self._stage_rank = _STAGE_ORDER[self.stage]
        return self.format_final_line()

    def format_timing_breakdown(self) -> str:
        """Format only the timing boundaries that were genuinely measured."""
        parts: List[str] = []
        if self.capture_ms is not None:
            parts.append(f"capture {format_duration_ms(self.capture_ms)}")
        # Prefer browser-measured upload duration when confirmed; otherwise server attach duration if confirmed.
        if self.upload_confirmed and self.browser_upload_ms is not None:
            parts.append(f"upload {format_duration_ms(self.browser_upload_ms)}")
        elif self.relay_attach_ms is not None:
            parts.append(f"relay {format_duration_ms(self.relay_attach_ms)}")
        if self.provider_response_ms is not None:
            parts.append(f"response {format_duration_ms(self.provider_response_ms)}")
        elif self.round_trip_ms is not None:
            parts.append(f"round trip {format_duration_ms(self.round_trip_ms)}")
        return " · ".join(parts)

    def format_final_line(self) -> str:
        """Render the concise single-line final outcome + measured timings."""
        rev_lbl = short_provider_name(self.reviewer_provider)
        act_lbl = short_provider_name(self.active_provider)
        timing = self.format_timing_breakdown()
        suffix = f" ({timing})" if timing else ""

        if self.outcome == "passed":
            if self.delegated:
                return f"{rev_lbl} passed visual review · Continuing in {act_lbl}{suffix}"
            return f"Visual review passed · {rev_lbl}{suffix}"

        if self.outcome == "defects":
            n = self.defects_count
            noun = "defect" if n == 1 else "defects"
            detail = f"{n} {noun}" if n > 0 else "findings"
            if self.delegated:
                return f"{rev_lbl} returned {detail} · Continuing in {act_lbl}{suffix}"
            return f"Visual review · {rev_lbl} reported {detail}{suffix}"

        if self.outcome == "reviewed":
            return f"Visual review complete · {rev_lbl} replied{suffix}"

        if self.outcome == "reviewed_with_local_defects":
            return f"Visual review · {rev_lbl} replied (local layout/console defects present){suffix}"

        if self.outcome == "unavailable":
            return f"Visual review incomplete · No reviewer returned usable findings{suffix}"

        if self.outcome == "cancelled":
            return f"Visual review cancelled{suffix}"

        if self.outcome == "transport_error":
            kind = self.error_kind or "transport error"
            return f"Visual review failed · {rev_lbl} {kind}{suffix}"

        if self.outcome == "capture_failed":
            return f"Visual capture failed{suffix}"

        return f"Visual review complete{suffix}"

    def to_diagnostics_dict(self) -> Dict[str, Any]:
        """Return sanitized diagnostics payload for `/status` (no images, emails, or URLs)."""
        timings: Dict[str, int] = {}
        if self.capture_ms is not None:
            timings["capture_ms"] = self.capture_ms
        if self.upload_confirmed and self.browser_upload_ms is not None:
            timings["upload_ms"] = self.browser_upload_ms
        if self.relay_pickup_ms is not None:
            timings["relay_pickup_ms"] = self.relay_pickup_ms
        if self.relay_attach_ms is not None:
            timings["relay_attach_ms"] = self.relay_attach_ms
        if self.provider_response_ms is not None:
            timings["response_ms"] = self.provider_response_ms
        if self.round_trip_ms is not None:
            timings["round_trip_ms"] = self.round_trip_ms
        return {
            "outcome": self.outcome or self.stage,
            "mode": "delegated" if self.delegated else "direct",
            "provider": self.active_provider,
            "reviewer": self.reviewer_provider,
            "upload_confirmed": bool(self.upload_confirmed),
            "submit_confirmed": bool(self.submit_confirmed),
            "defects_count": int(self.defects_count),
            "timings": timings,
            "summary": self.format_final_line(),
            "timing_text": self.format_timing_breakdown(),
        }
