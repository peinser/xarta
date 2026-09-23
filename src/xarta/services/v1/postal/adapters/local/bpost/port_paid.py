from __future__ import annotations

import hashlib

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from xarta.services.v1.postal.adapters.local.models import FrankingMethod
from xarta.services.v1.postal.adapters.local.models import decimal_quantity


@dataclass(frozen=True, slots=True)
class PortPaidProfile:
    id: str
    revision: str
    contractual: bool
    port_paid_number: str | None
    asset_path: str
    asset_sha256: str
    width_mm: Decimal
    height_mm: Decimal
    effective_from: date
    effective_until: date | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.revision or not self.asset_path:
            raise ValueError(
                "Port Paid profile identity and asset path must be non-empty"
            )
        if len(self.asset_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.asset_sha256
        ):
            raise ValueError("Port Paid asset SHA-256 must be lowercase hexadecimal")
        for name in ("width_mm", "height_mm"):
            value = decimal_quantity(getattr(self, name), name=name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if self.contractual and not self.port_paid_number:
            raise ValueError(
                "A contractual Port Paid profile requires a Port Paid number"
            )
        if (
            self.effective_until is not None
            and self.effective_until < self.effective_from
        ):
            raise ValueError("Port Paid profile effective date range is invalid")


@dataclass(frozen=True, slots=True)
class PortPaidEligibilityPolicy:
    id: str
    revision: str
    service_type: str
    speed: str | None
    minimum_items: int
    maximum_items: int | None
    permitted_deposit_channels: frozenset[str]
    permitted_sites: frozenset[str]
    grouping_dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.revision or not self.service_type:
            raise ValueError(
                "Port Paid policy identity and service type must be non-empty"
            )
        if self.minimum_items < 1:
            raise ValueError("Port Paid minimum_items must be positive")
        if self.maximum_items is not None and self.maximum_items < self.minimum_items:
            raise ValueError("Port Paid maximum_items must not be below minimum_items")
        allowed = {
            "service_type",
            "speed",
            "postal_format",
            "weight_band",
            "profile_id",
            "site",
            "channel",
            "service_date",
        }
        unknown = set(self.grouping_dimensions) - allowed
        if unknown:
            raise ValueError(
                f"Unknown Port Paid grouping dimensions: {sorted(unknown)}"
            )
        if not self.grouping_dimensions or len(set(self.grouping_dimensions)) != len(
            self.grouping_dimensions
        ):
            raise ValueError(
                "Port Paid grouping dimensions must be non-empty and unique"
            )
        if not self.permitted_deposit_channels or not self.permitted_sites:
            raise ValueError(
                "Port Paid policy must permit at least one channel and site"
            )


@dataclass(frozen=True, slots=True)
class PortPaidContext:
    service_type: str
    speed: str | None
    item_count: int
    site: str
    channel: str
    service_date: date | None = None

    def __post_init__(self) -> None:
        if self.item_count < 1:
            raise ValueError("Port Paid item count must be positive")
        if not self.service_type or not self.site or not self.channel:
            raise ValueError(
                "Port Paid context service, site, and channel must be non-empty"
            )


@dataclass(frozen=True, slots=True)
class FrankingPlan:
    method: FrankingMethod
    profile_id: str | None = None
    profile_revision: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DepositItem:
    operation_id: str
    service_type: str
    speed: str | None
    postal_format: str
    weight_band: str
    profile_id: str
    site: str
    channel: str
    service_date: date


def validate_port_paid_asset(
    profile: PortPaidProfile,
    asset: bytes,
    *,
    measured_width_mm: Decimal,
    measured_height_mm: Decimal,
    on_date: date,
) -> None:
    if hashlib.sha256(asset).hexdigest() != profile.asset_sha256:
        raise ValueError("Port Paid asset checksum does not match its profile")
    width = decimal_quantity(measured_width_mm, name="measured_width_mm")
    height = decimal_quantity(measured_height_mm, name="measured_height_mm")
    if width != profile.width_mm or height != profile.height_mm:
        raise ValueError("Port Paid asset dimensions do not match its profile")
    if on_date < profile.effective_from or (
        profile.effective_until is not None and on_date > profile.effective_until
    ):
        raise ValueError("Port Paid profile is not effective on the requested date")


def is_port_paid_eligible(
    policy: PortPaidEligibilityPolicy | None, context: PortPaidContext
) -> bool:
    if policy is None or context.item_count < policy.minimum_items:
        return False
    if policy.maximum_items is not None and context.item_count > policy.maximum_items:
        return False
    return (
        context.service_type == policy.service_type
        and (policy.speed is None or context.speed == policy.speed)
        and context.channel in policy.permitted_deposit_channels
        and context.site in policy.permitted_sites
    )


def plan_franking(
    *,
    profile: PortPaidProfile | None,
    policy: PortPaidEligibilityPolicy | None,
    context: PortPaidContext,
    stamps_fallback: bool,
) -> FrankingPlan:
    if (
        profile is not None
        and context.service_date is not None
        and context.service_date >= profile.effective_from
        and (
            profile.effective_until is None
            or context.service_date <= profile.effective_until
        )
        and is_port_paid_eligible(policy, context)
    ):
        return FrankingPlan(FrankingMethod.PORT_PAID, profile.id, profile.revision)
    if stamps_fallback:
        return FrankingPlan(
            FrankingMethod.STAMPS, reason="port_paid_not_configured_or_ineligible"
        )
    raise ValueError("No contract-configured valid franking method is available")


def group_deposit_items(
    items: tuple[DepositItem, ...], policy: PortPaidEligibilityPolicy
) -> dict[tuple[str, ...], tuple[DepositItem, ...]]:
    groups: dict[tuple[str, ...], list[DepositItem]] = {}
    for item in items:
        key = tuple(
            str(getattr(item, dimension)) for dimension in policy.grouping_dimensions
        )
        groups.setdefault(key, []).append(item)
    return {
        key: tuple(sorted(group, key=lambda item: item.operation_id))
        for key, group in sorted(groups.items())
    }
