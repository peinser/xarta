from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from xarta.services.v1.peppol.models import PeppolParticipant


@dataclass(frozen=True)
class RecommandCompanyConfiguration:
    company_id: str
    peppol_ids: tuple[PeppolParticipant, ...]


@dataclass(frozen=True)
class RecommandConfiguration:
    base_url: str
    api_key: str
    api_secret: str
    webhook_secret: str
    companies: tuple[RecommandCompanyConfiguration, ...]
    timeout: float
    companies_by_id: Mapping[str, RecommandCompanyConfiguration]
    companies_by_participant: Mapping[PeppolParticipant, RecommandCompanyConfiguration]

    @classmethod
    def parse(cls, configuration: Mapping[str, Any]) -> RecommandConfiguration:
        base_url = configuration.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError("Recommand configuration requires a non-empty base_url")
        if not base_url.startswith("https://"):
            raise ValueError("Recommand base_url must use HTTPS")
        credentials = {
            name: configuration.get(name)
            for name in ("api_key", "api_secret", "webhook_secret")
        }
        for name, value in credentials.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"Recommand configuration requires {name}")
        try:
            timeout = float(configuration.get("timeout", 30))
        except (TypeError, ValueError) as ex:
            raise ValueError("Recommand timeout must be a positive number") from ex
        if timeout <= 0:
            raise ValueError("Recommand timeout must be a positive number")

        raw_companies = configuration.get("companies")
        if not isinstance(raw_companies, list) or not raw_companies:
            raise ValueError(
                "Recommand configuration requires a non-empty companies array"
            )
        companies: list[RecommandCompanyConfiguration] = []
        companies_by_id: dict[str, RecommandCompanyConfiguration] = {}
        companies_by_participant: dict[
            PeppolParticipant, RecommandCompanyConfiguration
        ] = {}
        for raw_company in raw_companies:
            if not isinstance(raw_company, Mapping):
                raise ValueError("Recommand companies must be objects")
            company_id = raw_company.get("company_id")
            if not isinstance(company_id, str) or not company_id:
                raise ValueError("Recommand companies require a non-empty company_id")
            if company_id in companies_by_id:
                raise ValueError(f"Duplicate Recommand company_id: {company_id}")
            raw_participants = raw_company.get("peppol_ids")
            if not isinstance(raw_participants, list) or not raw_participants:
                raise ValueError(f"Recommand company {company_id} requires peppol_ids")
            participants = []
            for raw_participant in raw_participants:
                if not isinstance(raw_participant, Mapping):
                    raise ValueError("Recommand peppol_ids must be objects")
                scheme = raw_participant.get("scheme")
                identifier = raw_participant.get("identifier")
                if not isinstance(scheme, str) or not scheme:
                    raise ValueError("Recommand peppol_ids require scheme")
                if not isinstance(identifier, str) or not identifier:
                    raise ValueError("Recommand peppol_ids require identifier")
                participant = PeppolParticipant(scheme, identifier)
                if participant in companies_by_participant:
                    other = companies_by_participant[participant]
                    raise ValueError(
                        f"Peppol participant {scheme}:{identifier} maps to both "
                        f"{other.company_id} and {company_id}"
                    )
                participants.append(participant)
            company = RecommandCompanyConfiguration(company_id, tuple(participants))
            companies.append(company)
            companies_by_id[company_id] = company
            for participant in participants:
                companies_by_participant[participant] = company
        return cls(
            base_url.rstrip("/"),
            str(credentials["api_key"]),
            str(credentials["api_secret"]),
            str(credentials["webhook_secret"]),
            tuple(companies),
            timeout,
            MappingProxyType(companies_by_id),
            MappingProxyType(companies_by_participant),
        )
