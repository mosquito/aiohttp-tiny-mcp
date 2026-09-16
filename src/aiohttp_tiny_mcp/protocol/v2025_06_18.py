"""2025-06-18: differs from 2025-11-25 only in its revision identifier."""

from __future__ import annotations

from typing import ClassVar

from .v2025_11_25 import Adapter2025_11_25


class Adapter2025_06_18(Adapter2025_11_25):  # noqa: N801 -- revision date, greppable against the spec
    version: ClassVar[str] = "2025-06-18"
