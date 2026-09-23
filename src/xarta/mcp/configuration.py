from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class MCPConfiguration:
    intake_base_url: str
    document_type_base_url: str
    archive_base_url: str
    request_timeout_seconds: float
    archive_max_resource_bytes: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field, label in (
            ("intake_base_url", "intake"),
            ("document_type_base_url", "document type"),
            ("archive_base_url", "archive"),
        ):
            value = getattr(self, field)
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(f"MCP {label} base URL must use HTTP or HTTPS")
            if parsed.username or parsed.password:
                raise ValueError(f"MCP {label} base URL must not contain credentials")
            if parsed.query or parsed.fragment:
                raise ValueError(
                    f"MCP {label} base URL must not contain a query or fragment"
                )
            object.__setattr__(self, field, value.rstrip("/"))
        if (
            isinstance(self.request_timeout_seconds, bool)
            or self.request_timeout_seconds <= 0
        ):
            raise ValueError("MCP request timeout must be positive")
        if (
            isinstance(self.archive_max_resource_bytes, bool)
            or not 1 <= self.archive_max_resource_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError(
                "MCP archive resource limit must be between 1 and 8388608 bytes"
            )
        if not self.allowed_hosts or any(not host for host in self.allowed_hosts):
            raise ValueError("MCP allowed hosts must contain at least one host")
        if any(not origin for origin in self.allowed_origins):
            raise ValueError("MCP allowed origins must not contain empty values")


def comma_separated_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
