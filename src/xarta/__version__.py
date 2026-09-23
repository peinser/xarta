r"""
Returns the current version of the `xarta` module through the `__version__` variable.
"""

from __future__ import annotations

from importlib.metadata import version

__version__ = version("xarta")
