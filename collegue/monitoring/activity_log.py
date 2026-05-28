"""
Disk-backed activity log for the Collegue MCP expert system.

Records every significant event to a JSON-lines file so that
the Streamlit dashboard (separate process) can read them in
real-time.

Event types:
- llm_call: LLM request/response for an expert
- expert_result: Final result from an expert execution
- delegation: Inter-expert delegation event
- memory_write: Entry stored to ProjectMemory
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(".collegue") / "monitoring"


class ActivityLog:
    """Append-only, disk-backed activity log.

    Each event is a single JSON line written to ``activity.jsonl``.
    The dashboard reads this file to display real-time activity.
    """

    MAX_EVENTS = 2000  # keep the last N events on disk

    def __init__(self, base_dir: Optional[Path] = None):
        self._dir = base_dir or _DEFAULT_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "activity.jsonl"
        self._lock = threading.Lock()

    # ── write ────────────────────────────────────────────────────────────

    def _append(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        event.setdefault("time", time.strftime("%H:%M:%S"))
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.warning("activity_log write error: %s", exc)

    def log_llm_call(
        self,
        expert: str,
        prompt_preview: str,
        response_preview: str,
        duration_s: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        iteration: int = 1,
        model: str = "",
    ) -> None:
        self._append(
            {
                "type": "llm_call",
                "expert": expert,
                "prompt": prompt_preview[:500],
                "response": response_preview[:1000],
                "duration_s": round(duration_s, 2),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "iteration": iteration,
                "model": model,
            }
        )

    def log_expert_result(
        self,
        expert: str,
        status: str,
        duration_s: float,
        score: Optional[float] = None,
        summary: str = "",
        iterations: int = 0,
        findings_count: int = 0,
    ) -> None:
        self._append(
            {
                "type": "expert_result",
                "expert": expert,
                "status": status,
                "duration_s": round(duration_s, 2),
                "score": score,
                "summary": summary[:300],
                "iterations": iterations,
                "findings_count": findings_count,
            }
        )

    def log_delegation(
        self,
        source: str,
        target: str,
        reason: str,
        params_preview: Optional[Dict[str, Any]] = None,
    ) -> None:
        preview = {}
        if params_preview:
            for k, v in list(params_preview.items())[:5]:
                preview[k] = str(v)[:200]
        self._append(
            {
                "type": "delegation",
                "source": source,
                "target": target,
                "reason": reason[:200],
                "params": preview,
            }
        )

    def log_memory_write(
        self,
        expert: str,
        entry_type: str,
        category: str,
        title: str,
        score: float = 0.0,
    ) -> None:
        self._append(
            {
                "type": "memory_write",
                "expert": expert,
                "entry_type": entry_type,
                "category": category,
                "title": title[:200],
                "score": score,
            }
        )

    # ── read ─────────────────────────────────────────────────────────────

    def read_events(
        self,
        event_type: Optional[str] = None,
        expert: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Read recent events, optionally filtered."""
        events: List[Dict[str, Any]] = []
        try:
            if not self._path.exists():
                return []
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event_type and ev.get("type") != event_type:
                        continue
                    if expert and ev.get("expert") != expert:
                        continue
                    events.append(ev)
        except OSError:
            pass
        return events[-limit:]

    def clear(self) -> None:
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass

    def prune(self) -> None:
        """Keep only the last MAX_EVENTS events."""
        events = self.read_events(limit=self.MAX_EVENTS)
        with self._lock:
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    for ev in events:
                        f.write(json.dumps(ev, default=str, ensure_ascii=False) + "\n")
            except OSError:
                pass


# ── singleton ────────────────────────────────────────────────────────────

_activity_log: Optional[ActivityLog] = None
_activity_lock = threading.Lock()


def get_activity_log() -> ActivityLog:
    """Get or create the global ActivityLog singleton."""
    global _activity_log
    if _activity_log is None:
        with _activity_lock:
            if _activity_log is None:
                _activity_log = ActivityLog()
    return _activity_log
