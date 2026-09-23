"""Printer backend implementations."""

from __future__ import annotations

from postal_station.printing.base import Printer
from postal_station.printing.cups import CupsPrinter
from postal_station.printing.fake import FakePrinter

__all__ = ["CupsPrinter", "FakePrinter", "Printer"]
