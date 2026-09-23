"""Station configuration loaded from the process environment."""

from __future__ import annotations

import os

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class StationConfig:
    api_url: str
    station_id: str
    printer: str
    cache_dir: Path
    api_token: str | None = None
    request_timeout_seconds: float = 30.0
    max_package_bytes: int = 1_073_741_824
    timezone: str = "Europe/Brussels"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> StationConfig:
        values = os.environ if env is None else env
        api_url = _required(values, "POSTAL_STATION_API_URL").rstrip("/")
        station_id = _required(values, "POSTAL_STATION_ID")
        printer = _required(values, "POSTAL_STATION_PRINTER")
        cache_dir = Path(
            values.get("POSTAL_STATION_CACHE_DIR", "~/.cache/xarta-postal-station")
        ).expanduser()
        api_token = values.get("POSTAL_STATION_API_TOKEN") or None
        timezone = values.get("POSTAL_STATION_TIMEZONE", "Europe/Brussels").strip()
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(
                "POSTAL_STATION_TIMEZONE must be an IANA timezone"
            ) from error

        raw_timeout = values.get("POSTAL_STATION_REQUEST_TIMEOUT_SECONDS", "30")
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ValueError(
                "POSTAL_STATION_REQUEST_TIMEOUT_SECONDS must be a number"
            ) from error
        if timeout <= 0:
            raise ValueError(
                "POSTAL_STATION_REQUEST_TIMEOUT_SECONDS must be greater than zero"
            )

        raw_max_package_bytes = values.get(
            "POSTAL_STATION_MAX_PACKAGE_BYTES", "1073741824"
        )
        try:
            max_package_bytes = int(raw_max_package_bytes)
        except ValueError as error:
            raise ValueError(
                "POSTAL_STATION_MAX_PACKAGE_BYTES must be an integer"
            ) from error
        if max_package_bytes <= 0:
            raise ValueError(
                "POSTAL_STATION_MAX_PACKAGE_BYTES must be greater than zero"
            )

        parsed_url = urlparse(api_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("POSTAL_STATION_API_URL must be an absolute HTTP(S) URL")
        return cls(
            api_url,
            station_id,
            printer,
            cache_dir,
            api_token,
            timeout,
            max_package_bytes,
            timezone,
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value
