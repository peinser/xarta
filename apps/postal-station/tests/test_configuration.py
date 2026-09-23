from __future__ import annotations

import pytest

from postal_station.configuration import StationConfig


def test_configuration_is_typed_from_environment(tmp_path):
    config = StationConfig.from_env(
        {
            "POSTAL_STATION_API_URL": "https://postal.example.test/",
            "POSTAL_STATION_ID": "station-1",
            "POSTAL_STATION_PRINTER": "mailroom",
            "POSTAL_STATION_CACHE_DIR": str(tmp_path),
            "POSTAL_STATION_REQUEST_TIMEOUT_SECONDS": "4.5",
            "POSTAL_STATION_MAX_PACKAGE_BYTES": "2048",
        }
    )

    assert config.api_url == "https://postal.example.test"
    assert config.cache_dir == tmp_path
    assert config.request_timeout_seconds == 4.5
    assert config.max_package_bytes == 2048
    assert config.timezone == "Europe/Brussels"


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_package_size_limit_must_be_a_positive_integer(value):
    values = {
        "POSTAL_STATION_API_URL": "https://postal.example.test",
        "POSTAL_STATION_ID": "station-1",
        "POSTAL_STATION_PRINTER": "mailroom",
        "POSTAL_STATION_MAX_PACKAGE_BYTES": value,
    }

    with pytest.raises(ValueError, match="POSTAL_STATION_MAX_PACKAGE_BYTES"):
        StationConfig.from_env(values)


@pytest.mark.parametrize(
    "name", ["POSTAL_STATION_API_URL", "POSTAL_STATION_ID", "POSTAL_STATION_PRINTER"]
)
def test_required_configuration(name):
    values = {
        "POSTAL_STATION_API_URL": "https://postal.example.test",
        "POSTAL_STATION_ID": "station-1",
        "POSTAL_STATION_PRINTER": "mailroom",
    }
    del values[name]

    with pytest.raises(ValueError, match=f"{name} is required"):
        StationConfig.from_env(values)


def test_station_timezone_must_exist():
    with pytest.raises(ValueError, match="POSTAL_STATION_TIMEZONE"):
        StationConfig.from_env(
            {
                "POSTAL_STATION_API_URL": "https://postal.example.test",
                "POSTAL_STATION_ID": "station-1",
                "POSTAL_STATION_PRINTER": "mailroom",
                "POSTAL_STATION_TIMEZONE": "Mars/Olympus",
            }
        )
