"""Composition entry point for embedding in a station UI."""

from __future__ import annotations

from postal_station.api_client import UrllibPostalStationApi
from postal_station.application import PostalStation
from postal_station.configuration import StationConfig
from postal_station.packages import PackageVerifier
from postal_station.printing.cups import CupsPrinter


def create_station(config: StationConfig | None = None) -> PostalStation:
    resolved = StationConfig.from_env() if config is None else config
    return PostalStation(
        resolved,
        UrllibPostalStationApi(resolved),
        CupsPrinter(resolved.printer),
        PackageVerifier(),
    )
