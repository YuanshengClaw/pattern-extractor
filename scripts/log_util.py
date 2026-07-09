#!/usr/bin/env python3
"""Minimal logging utility for step-level progress and structured output."""

import os
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StepLogger:
    """Write per-step logs to a file while printing progress to terminal."""

    def __init__(self, log_dir: str, step_name: str):
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.log_path = os.path.join(log_dir, f"{step_name}_{ts}.log")
        self._file = open(self.log_path, "w", encoding="utf-8")

    def log(self, msg: str):
        line = f"[{_now_iso()}] {msg}"
        self._file.write(line + "\n")
        self._file.flush()

    def progress(self, msg: str):
        """Write a progress line to both log and terminal."""
        self.log(msg)
        print(msg, flush=True)

    def done(self):
        self.log("=== DONE ===\n")

    def close(self):
        if not self._file.closed:
            self._file.close()
