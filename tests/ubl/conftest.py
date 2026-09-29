from __future__ import annotations

import pytest

import xarta.protocol.document.source as source_module
import xarta.services.v1.ubl.nats as ubl_worker

from xarta.storage import FilesystemTemporaryStorage
from xarta.ubl.editor import CAC
from xarta.ubl.editor import CBC
from xarta.ubl.editor import UBLEditor


@pytest.fixture
def editor() -> UBLEditor:
    return UBLEditor()


@pytest.fixture
def invoice():
    def build(name="Invoice", references="", extension="", project="") -> bytes:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
        <{name} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{name}-2"
          xmlns:cbc="{CBC}" xmlns:cac="{CAC}">
          {extension}
          <cbc:ID>INV-001</cbc:ID><cbc:IssueDate>2026-01-01</cbc:IssueDate>
          {references}{project}
          <cac:AccountingSupplierParty/><cac:AccountingCustomerParty/>
          <cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="EUR">10.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
          <cac:{name}Line><cbc:ID>1</cbc:ID><cbc:LineExtensionAmount currencyID="EUR">10.00</cbc:LineExtensionAmount>
            <cac:Item><cbc:Name>Example</cbc:Name></cac:Item></cac:{name}Line>
        </{name}>""".encode()

    return build


@pytest.fixture
def storage(tmp_path, monkeypatch):
    storage = FilesystemTemporaryStorage(str(tmp_path / "storage"))
    monkeypatch.setattr(source_module, "get_temporary_storage", lambda: storage)
    monkeypatch.setattr(ubl_worker, "get_temporary_storage", lambda: storage)
    return storage
