from __future__ import annotations

import hmac

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class StationIdentity:
    id: str
    site: str


class StationRegistry:
    def __init__(self, stations: Mapping[str, Mapping[str, Any]]) -> None:
        validated: dict[str, tuple[str, str]] = {}
        for identifier, value in stations.items():
            site = value.get("site")
            token = value.get("api_token")
            enabled = value.get("enabled", True)
            if (
                not isinstance(identifier, str)
                or not identifier
                or not isinstance(site, str)
                or not site
            ):
                raise ValueError("Postal stations require non-empty IDs and sites")
            if not isinstance(enabled, bool):
                raise ValueError(f"Postal station {identifier} enabled must be boolean")
            if enabled and (not isinstance(token, str) or not token):
                raise ValueError(
                    f"Enabled postal station {identifier} requires an API token"
                )
            if enabled:
                assert isinstance(token, str)
                validated[identifier] = (site, token)
        self._stations = MappingProxyType(validated)

    def authenticate(self, authorization: str | None) -> StationIdentity:
        if not authorization or not authorization.startswith("Bearer "):
            raise PermissionError("Bearer authentication is required")
        supplied = authorization[7:]
        for identifier, (site, token) in self._stations.items():
            if hmac.compare_digest(supplied, token):
                return StationIdentity(identifier, site)
        raise PermissionError("Invalid station bearer token")

    def require_station(self, identity: StationIdentity, station_id: str) -> None:
        if identity.id != station_id:
            raise PermissionError("Station is outside the authenticated scope")
