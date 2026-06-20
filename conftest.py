"""Root conftest: ensure the repo root is on sys.path for Tier-1 tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Add the repo root so `custom_components` is importable without an install step.
sys.path.insert(0, str(Path(__file__).parent))
