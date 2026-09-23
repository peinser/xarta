"""Deterministic station-owned rendering with retained atomic cache output."""

from __future__ import annotations

import hashlib
import os

from dataclasses import dataclass
from pathlib import Path

from postal_station.models import PackageLetter
from postal_station.models import VerifiedPackage
from postal_station.rendering.composition import compose_letter_pdf
from postal_station.rendering.composition import merge_pdf_documents
from postal_station.rendering.composition import page_count
from postal_station.rendering.traveller import render_traveller

SUPPORTED_TRAVELLER_REVISIONS = frozenset({"1", "v1", "development-v1"})


@dataclass(frozen=True, slots=True)
class RenderedLetter:
    path: Path
    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class Renderer:
    supported_revisions: frozenset[str] = SUPPORTED_TRAVELLER_REVISIONS

    def render(
        self,
        package: VerifiedPackage,
        letter: PackageLetter,
        cache_directory: Path | None = None,
    ) -> RenderedLetter:
        if letter.traveller.template_revision not in self.supported_revisions:
            raise ValueError(
                f"unsupported traveller template revision: {letter.traveller.template_revision}"
            )
        documents = []
        for source in letter.sources:
            value = package.path_for(source).read_bytes()
            if (
                len(value) != source.byte_count
                or hashlib.sha256(value).hexdigest() != source.sha256
            ):
                raise ValueError(
                    f"source changed after package verification: {source.path}"
                )
            if page_count(value) != source.page_count:
                raise ValueError(
                    f"source page count does not match manifest: {source.path}"
                )
            documents.append(value)
        expected_content_pages = sum(source.page_count for source in letter.sources)
        expected_blanks = 0
        if (
            letter.print_instructions.sides.value != "simplex"
            and letter.print_instructions.document_boundary.value != "continuous"
        ):
            expected_blanks = sum(
                source.page_count % 2 for source in letter.sources[:-1]
            )
        quantities = letter.traveller.quantities
        if (
            quantities["content_pages"] != expected_content_pages
            or quantities["blank_pages"] != expected_blanks
        ):
            raise ValueError(
                "traveller quantities do not match source PDFs and boundary rules"
            )
        expected_sheets = expected_content_pages + expected_blanks
        if letter.print_instructions.sides.value != "simplex":
            expected_sheets = (expected_sheets + 1) // 2
        if quantities["sheets"] != expected_sheets:
            raise ValueError("traveller sheet quantity does not match source PDFs")
        content = merge_pdf_documents(
            tuple(documents),
            sides=letter.print_instructions.sides,
            boundary=letter.print_instructions.document_boundary,
        )
        traveller = render_traveller(
            letter.task_id, len(letter.sources), letter.traveller
        )
        rendered = compose_letter_pdf(
            traveller, content, sides=letter.print_instructions.sides
        )
        digest = hashlib.sha256(rendered).hexdigest()
        directory = cache_directory or package.root.parent / "rendered"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / f"{letter.job.id}-g{letter.generation}-{digest}.pdf"
        if target.exists():
            cached = target.read_bytes()
            if cached != rendered:
                raise ValueError("render cache digest collision")
            return RenderedLetter(target, digest, len(rendered))
        temporary = directory / f".{target.name}.tmp-{os.getpid()}"
        try:
            with temporary.open("xb") as output:
                output.write(rendered)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return RenderedLetter(target, digest, len(rendered))
