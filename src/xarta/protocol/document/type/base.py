r"""
Base definitions related to document type and their global utilities.
"""

from __future__ import annotations

import datetime

from dataclasses import dataclass
from dataclasses import field

from isodate import duration_isoformat
from isodate import parse_duration

from xarta.protocol.template.engine import TemplateEngineIdentifier


@dataclass
class DocumentTypeIdentifier:
    value: str


@dataclass
class DocumentType:
    # TODO Introduce typing of TemplateEngineOptions
    identifier: (
        DocumentTypeIdentifier  # Unique identification code of the document type.
    )
    default_template_engine: (
        TemplateEngineIdentifier  # Identifier of the default template engine.
    )
    default_content_type: str  # Default content-type
    metadata: dict = field(default_factory=dict)  # Default metadata.
    default_business_data: dict = field(default_factory=dict)  # Default business data.
    default_template_engine_options: dict = field(
        default_factory=dict
    )  # Special (default) template engine configuration.
    default_metadata: dict = field(default_factory=dict)  # Default _document_ metadata.
    default_retention: datetime.timedelta | None = None  # Default retention interval.
    _id: int | None = (
        None  # Internal integer code uniquely identifying the document type.
    )

    def dict(self) -> dict:
        return {
            "identifier": self.identifier.value,
            "defaults": {
                "content_type": self.default_content_type,
                "template_engine": self.default_template_engine.value,
                "retention": (
                    duration_isoformat(self.default_retention)
                    if self.default_retention is not None
                    else None
                ),
                "business_data": self.default_business_data,
                "metadata": self.default_metadata,
                "template_engine_options": self.default_template_engine_options,
            },
            "metadata": self.metadata,
        }

    @staticmethod
    def fromdict(data: dict) -> DocumentType:
        r"""
        Parses a DocumentType dataclass from the specified dictionary.
        """
        defaults = data["defaults"]

        return DocumentType(
            identifier=DocumentTypeIdentifier(data["identifier"]),
            default_retention=(
                parse_duration(defaults["retention"])
                if defaults.get("retention")
                else None
            ),
            metadata=data.get("metadata", {}),
            default_business_data=defaults.get("business_data", {}),
            default_template_engine=TemplateEngineIdentifier(
                defaults["template_engine"]
            ),
            default_template_engine_options=defaults.get("template_engine_options", {}),
            default_content_type=defaults["content_type"],
            default_metadata=defaults.get("metadata", {}),
        )
