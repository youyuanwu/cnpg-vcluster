from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.database_controller import build_database_controller_image


if __name__ == "__main__":
    print(build_database_controller_image(ROOT, load_configuration(ROOT)))
