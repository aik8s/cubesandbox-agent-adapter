"""Redacted, fan-out audit delivery with a bounded asynchronous queue."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Protocol
from urllib.request import Request, urlopen

from .config import AdapterConfig
from .metrics import AdapterMetrics


class AuditSink(Protocol):
    def emit(self, event: Dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class FileAuditSink:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def emit(self, event: Dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def close(self) -> None:
        return


class StdoutAuditSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def emit(self, event: Dict[str, Any]) -> None:
        line = json.dumps(
            {"stream": "cube-adapter-audit", **event},
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._lock:
            print(line, file=sys.stdout, flush=True)

    def close(self) -> None:
        return


class HttpAuditSink:
    def __init__(self, url: str, token: Optional[str]) -> None:
        self.url = url
        self.token = token

    def emit(self, event: Dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            self.url,
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            if response.status >= 300:
                raise RuntimeError(f"audit collector returned HTTP {response.status}")

    def close(self) -> None:
        return


class AuditManager:
    def __init__(
        self,
        sinks: Iterable[AuditSink],
        *,
        metrics: AdapterMetrics,
        recent_limit: int = 200,
        queue_size: int = 4096,
    ) -> None:
        self._sinks = tuple(sinks)
        self._metrics = metrics
        self._recent: deque[Dict[str, Any]] = deque(maxlen=recent_limit)
        self._recent_lock = threading.Lock()
        self._queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(
            maxsize=queue_size
        )
        self._closed = False
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    @classmethod
    def from_config(cls, config: AdapterConfig, metrics: AdapterMetrics) -> "AuditManager":
        sinks: list[AuditSink] = []
        if "file" in config.audit_sinks:
            sinks.append(FileAuditSink(config.audit_log))
        if "stdout" in config.audit_sinks:
            sinks.append(StdoutAuditSink())
        if "http" in config.audit_sinks:
            assert config.audit_http_url is not None
            sinks.append(HttpAuditSink(config.audit_http_url, config.audit_http_token))
        manager = cls(sinks, metrics=metrics)
        if "file" in config.audit_sinks:
            manager.load_recent(config.audit_log)
        return manager

    def emit(self, event: Dict[str, Any]) -> None:
        snapshot = dict(event)
        with self._recent_lock:
            self._recent.append(snapshot)
        try:
            self._queue.put_nowait(snapshot)
        except queue.Full:
            self._metrics.audit_dropped.inc()

    def recent(self) -> list[Dict[str, Any]]:
        with self._recent_lock:
            return list(self._recent)

    def load_recent(self, path: str) -> None:
        try:
            with Path(path).open("r", encoding="utf-8") as stream:
                lines = deque(stream, maxlen=self._recent.maxlen)
        except FileNotFoundError:
            return
        except OSError:
            return
        with self._recent_lock:
            for line in lines:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    self._recent.append(value)

    def _worker(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                for sink in self._sinks:
                    try:
                        sink.emit(event)
                    except Exception:
                        # The primary operation must never fail because an audit
                        # transport is temporarily unavailable. Queue overflow is
                        # exposed as a metric; sink-level failures belong in the
                        # collector's health monitoring.
                        self._metrics.audit_failures.labels(type(sink).__name__).inc()
                        continue
            finally:
                self._queue.task_done()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=2)
        except queue.Full:
            pass
        self._thread.join(timeout=5)
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                pass
