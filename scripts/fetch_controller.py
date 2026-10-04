#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.controller import fetch_controller_dependencies


if __name__ == "__main__":
    fetch_controller_dependencies(ROOT, load_configuration(ROOT))
