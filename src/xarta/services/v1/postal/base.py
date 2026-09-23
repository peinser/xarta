from __future__ import annotations

from typing import Any
from typing import cast

from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.adapters import AdapterRegistry
from xarta.services.v1.postal.adapters import PostalAdapter
from xarta.services.v1.postal.adapters import build_postal_components
from xarta.services.v1.postal.adapters.local.adapter import LocalPostalAdapterFactory
from xarta.services.v1.postal.adapters.local.artifacts import PostalArtifactStore
from xarta.services.v1.postal.adapters.local.auth import StationRegistry
from xarta.services.v1.postal.adapters.local.bpost import BpostProductionInstructionResolver  # fmt: skip
from xarta.services.v1.postal.adapters.local.repository import LocalPostalRepository
from xarta.services.v1.postal.adapters.local.service import LocalPostalService
from xarta.services.v1.postal.api import acknowledge_package
from xarta.services.v1.postal.api import active_runs
from xarta.services.v1.postal.api import claim_run
from xarta.services.v1.postal.api import create_handover_batch
from xarta.services.v1.postal.api import create_print_attempt
from xarta.services.v1.postal.api import download_package
from xarta.services.v1.postal.api import production_run
from xarta.services.v1.postal.api import production_scan
from xarta.services.v1.postal.api import report_print_event
from xarta.services.v1.postal.configuration import configuration_section
from xarta.services.v1.postal.configuration import load_json_object
from xarta.storage import FilesystemTemporaryStorage
from xarta.tracking import DestinationRegistry
from xarta.tracking import PostgresTrackingStore
from xarta.tracking import TrackedCapabilityService
from xarta.tracking import TrackingPostgresModel

bp = Blueprint(name="postal-v1", url_prefix="/api/v1/postal")

env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "TMP_STORAGE",
        "POSTAL_CONFIGURATIONS_CONFIG_PATH",
        "POSTAL_LOCAL_PROFILES_CONFIG_PATH",
        "POSTAL_BPOST_PROFILES_CONFIG_PATH",
        "POSTAL_LOCAL_CAPACITY_CONFIG_PATH",
        "POSTAL_LOCAL_STATIONS_CONFIG_PATH",
        "POSTAL_LOCAL_STORAGE_CONFIG_PATH",
    },
)
env.verify_postgresql(bp, "TRACKING")


@bp.listener("before_server_start")
async def _setup_postal(app) -> None:
    from xarta.services.v1.postal.nats import PostalNATSModel

    await TrackingPostgresModel.register(app)
    pool = TrackingPostgresModel.pool()
    tracking_store = PostgresTrackingStore(pool)
    repository = LocalPostalRepository(pool)
    destinations_data = await load_json_object(
        env.extract("POSTAL_CONFIGURATIONS_CONFIG_PATH", optional=False)
    )
    profiles_data = await load_json_object(
        env.extract("POSTAL_LOCAL_PROFILES_CONFIG_PATH", optional=False)
    )
    bpost_profiles_data = await load_json_object(
        env.extract("POSTAL_BPOST_PROFILES_CONFIG_PATH", optional=False)
    )
    capacity_data = await load_json_object(
        env.extract("POSTAL_LOCAL_CAPACITY_CONFIG_PATH", optional=False)
    )
    stations_data = await load_json_object(
        env.extract("POSTAL_LOCAL_STATIONS_CONFIG_PATH", optional=False)
    )
    storage_data = await load_json_object(
        env.extract("POSTAL_LOCAL_STORAGE_CONFIG_PATH", optional=False)
    )
    backend = configuration_section(storage_data, "backend")
    if backend.get("kind") != "filesystem" or not isinstance(backend.get("path"), str):
        raise env.ConfigurationError(
            "Postal local storage requires a filesystem backend with a path"
        )
    artifacts = PostalArtifactStore(FilesystemTemporaryStorage(backend["path"]))
    destinations = DestinationRegistry(
        destinations_data, require_explicit_revisions=True
    )
    tracking = TrackedCapabilityService(
        cast("Any", tracking_store), destinations, PostalNATSModel.publish
    )
    factory = LocalPostalAdapterFactory(
        profiles=configuration_section(profiles_data, "profiles"),
        capacity_pools=configuration_section(capacity_data, "capacity_pools"),
        repository=repository,
        artifacts=artifacts,
        tracking=tracking,
        instruction_resolvers={
            "bpost": BpostProductionInstructionResolver(
                configuration_section(bpost_profiles_data, "profiles")
            )
        },
    )
    adapters: AdapterRegistry[PostalAdapter] = AdapterRegistry(
        {"local": cast("Any", factory)}
    )
    components = build_postal_components(destinations_data, adapters)
    app.ctx.postal_components = components
    app.ctx.postal_repository = repository
    app.ctx.postal_artifacts = artifacts
    app.ctx.postal_service = LocalPostalService(repository, tracking)
    app.ctx.postal_station_registry = StationRegistry(
        configuration_section(stations_data, "stations")
    )
    await PostalNATSModel.register(app=app, components=components)


@bp.get("/")
async def health(_):
    return response.empty()


bp.add_route(claim_run, "/local/production-runs/claims", methods=["POST"])
bp.add_route(
    active_runs, "/local/stations/<station_id:str>/active-runs", methods=["GET"]
)
bp.add_route(production_run, "/local/production-runs/<run_id:str>", methods=["GET"])
bp.add_route(
    download_package, "/local/production-runs/<run_id:str>/package", methods=["GET"]
)
bp.add_route(
    acknowledge_package,
    "/local/production-runs/<run_id:str>/package-acknowledgements",
    methods=["POST"],
)
bp.add_route(
    create_print_attempt, "/local/print-jobs/<job_id:str>/attempts", methods=["POST"]
)
bp.add_route(
    report_print_event,
    "/local/print-attempts/<attempt_id:str>/events",
    methods=["POST"],
)
bp.add_route(production_scan, "/local/production-scans", methods=["POST"])
bp.add_route(create_handover_batch, "/local/handover-batches", methods=["POST"])
