from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_LINE_CHARS = 400
RECENT_CHARS = 3000


class ProjectErrorLog:
    """Errors seen by the executor and the reviewer, shared through `.arc/error-log.md`."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._entries: list[str] = []

    def add(self, source: str, lines) -> None:
        fresh = []
        for line in lines:
            text = " ".join(str(line or "").split())[:MAX_LINE_CHARS]
            if text and text not in self._seen:
                fresh.append(text)
        if not fresh:
            return
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            new = [line for line in fresh if line not in self._seen]
            if not new:
                return
            self._seen.update(new)
            block = f"## {stamp} {source}\n" + "\n".join(f"- {line}" for line in new)
            self._entries.append(block)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(block + "\n\n")

    def recent(self, max_chars: int = RECENT_CHARS) -> str:
        with self._lock:
            picked: list[str] = []
            size = 0
            for block in reversed(self._entries):
                if size + len(block) > max_chars:
                    break
                picked.append(block)
                size += len(block)
        return "\n\n".join(reversed(picked))
