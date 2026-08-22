"""Validate bundled Home Assistant and HACS brand artwork."""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
INTEGRATION_BRAND = ROOT / "custom_components" / "fortigate_policy" / "brand"
REPOSITORY_BRAND = ROOT / "brand"


def _png_dimensions(path: Path) -> tuple[int, int]:
    """Read PNG dimensions directly from its IHDR chunk."""
    payload = path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a valid PNG")
    return struct.unpack(">II", payload[16:24])


class TestBrandAssets(unittest.TestCase):
    """Keep local and repository artwork valid and synchronized."""

    def test_integration_icons_have_home_assistant_dimensions(self) -> None:
        self.assertEqual((256, 256), _png_dimensions(INTEGRATION_BRAND / "icon.png"))
        self.assertEqual(
            (512, 512), _png_dimensions(INTEGRATION_BRAND / "icon@2x.png")
        )

    def test_hacs_repository_icons_match_integration_icons(self) -> None:
        for filename in ("icon.png", "icon@2x.png"):
            self.assertEqual(
                (INTEGRATION_BRAND / filename).read_bytes(),
                (REPOSITORY_BRAND / filename).read_bytes(),
            )
