"""Small durable guard around non-idempotent printer submission."""

from __future__ import annotations

import json
import os

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class JobJournal:
    path: Path

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) - {
            "status",
            "rendered_sha256",
            "rendered_byte_count",
            "attempt_id",
            "printer_job_id",
        }:
            raise ValueError("invalid station print journal")
        if value.get("status") not in {
            "rendered",
            "submission_started",
            "printer_accepted",
            "server_reported",
            "uncertain",
        }:
            raise ValueError("invalid station print journal status")
        return value

    def write(self, status: str, **values: Any) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {"status": status, **values}
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(payload, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
