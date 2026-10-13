"""Per-node conversation ids so `session = continue` can resume the previous run."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional


class SessionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            tmp.replace(self.path)
        except Exception:
            pass

    def get(self, node_key: str, backend: str) -> Optional[str]:
        with self._lock:
            return (self._data.get(str(node_key)) or {}).get(backend)

    def set(self, node_key: str, backend: str, session_id: Optional[str]) -> None:
        if not session_id:
            return
        with self._lock:
            self._data.setdefault(str(node_key), {})[backend] = session_id
            self._save()

    def clear(self, node_key: str, backend: Optional[str] = None) -> None:
        with self._lock:
            if backend is None:
                self._data.pop(str(node_key), None)
            else:
                (self._data.get(str(node_key)) or {}).pop(backend, None)
            self._save()
