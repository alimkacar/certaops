#!/usr/bin/env python3
"""CLI baslatici. `pip install -e .` yapmadan da calisir."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from certaops.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
