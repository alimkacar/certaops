"""Public paketlerin import sirasi birbirinden bagimsiz kalmali."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "statement",
    [
        "from certaops.runtime import AgentTurn, SAPAgentRuntime, ToolCall",
        "from robotics_agent import SAPAgentRuntime, Settings, get_settings",
        "from robotics_agent.compat_agent import SAPDomainAgent, SAPMultiAgent",
    ],
)
def test_public_import_clean_process(statement):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c", statement],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
