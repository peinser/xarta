from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from xarta.services.v1.peppol.models import PeppolParticipant


@dataclass(frozen=True)
class EInvoiceBeTenantConfiguration:
    tenant_id: str
    api_key: str
    webhook_secret: str
    peppol_ids: tuple[PeppolParticipant, ...]


@dataclass(frozen=True)
class EInvoiceBeConfiguration:
    base_url: str
    tenants: tuple[EInvoiceBeTenantConfiguration, ...]
    timeout: float
    tenants_by_id: Mapping[str, EInvoiceBeTenantConfiguration]
    tenants_by_participant: Mapping[PeppolParticipant, EInvoiceBeTenantConfiguration]

    @classmethod
    def parse(cls, configuration: Mapping[str, Any]) -> EInvoiceBeConfiguration:
        base_url = configuration.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError("e-invoice.be configuration requires a non-empty base_url")
        if not base_url.startswith("https://"):
            raise ValueError("e-invoice.be base_url must use HTTPS")
        raw_tenants = configuration.get("tenants")
        if not isinstance(raw_tenants, list) or not raw_tenants:
            raise ValueError(
                "e-invoice.be configuration requires a non-empty tenants array"
            )
        try:
            timeout = float(configuration.get("timeout", 30))
        except (TypeError, ValueError) as ex:
            raise ValueError("e-invoice.be timeout must be a positive number") from ex
        if timeout <= 0:
            raise ValueError("e-invoice.be timeout must be a positive number")

        tenants: list[EInvoiceBeTenantConfiguration] = []
        tenants_by_id: dict[str, EInvoiceBeTenantConfiguration] = {}
        tenants_by_participant: dict[
            PeppolParticipant, EInvoiceBeTenantConfiguration
        ] = {}
        for raw_tenant in raw_tenants:
            if not isinstance(raw_tenant, Mapping):
                raise ValueError("e-invoice.be tenants must be objects")
            tenant_id = raw_tenant.get("tenant_id")
            api_key = raw_tenant.get("api_key")
            webhook_secret = raw_tenant.get("webhook_secret")
            if not isinstance(tenant_id, str) or not tenant_id:
                raise ValueError("e-invoice.be tenants require a non-empty tenant_id")
            if tenant_id in tenants_by_id:
                raise ValueError(f"Duplicate e-invoice.be tenant_id: {tenant_id}")
            for name, value in (
                ("api_key", api_key),
                ("webhook_secret", webhook_secret),
            ):
                if not isinstance(value, str) or not value:
                    raise ValueError(f"e-invoice.be tenant {tenant_id} requires {name}")
            raw_participants = raw_tenant.get("peppol_ids")
            if not isinstance(raw_participants, list) or not raw_participants:
                raise ValueError(f"e-invoice.be tenant {tenant_id} requires peppol_ids")
            participants: list[PeppolParticipant] = []
            for raw_participant in raw_participants:
                if not isinstance(raw_participant, Mapping):
                    raise ValueError("e-invoice.be peppol_ids must be objects")
                scheme = raw_participant.get("scheme")
                identifier = raw_participant.get("identifier")
                if not isinstance(scheme, str) or not scheme:
                    raise ValueError("e-invoice.be peppol_ids require scheme")
                if not isinstance(identifier, str) or not identifier:
                    raise ValueError("e-invoice.be peppol_ids require identifier")
                participant = PeppolParticipant(scheme, identifier)
                if participant in tenants_by_participant:
                    other = tenants_by_participant[participant]
                    raise ValueError(
                        f"Peppol participant {scheme}:{identifier} maps to both "
                        f"{other.tenant_id} and {tenant_id}"
                    )
                participants.append(participant)
            tenant = EInvoiceBeTenantConfiguration(
                tenant_id, str(api_key), str(webhook_secret), tuple(participants)
            )
            tenants.append(tenant)
            tenants_by_id[tenant_id] = tenant
            for participant in participants:
                tenants_by_participant[participant] = tenant
        return cls(
            base_url.rstrip("/"),
            tuple(tenants),
            timeout,
            MappingProxyType(tenants_by_id),
            MappingProxyType(tenants_by_participant),
        )
