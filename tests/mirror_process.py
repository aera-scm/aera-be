"""Start the real SAP Mirror (Node, SQLite in memory, reference scenario at a fixed T0)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIRROR = Path(__file__).resolve().parents[1] / "sap-mirror"
SERVE = MIRROR / "node_modules" / "@sap" / "cds" / "bin" / "serve.js"
NODE = shutil.which("node")
T0 = "2026-10-05T08:00:00Z"
AVAILABLE = NODE is not None and SERVE.exists()
MISSING = "Mirror dependencies not installed (make setup)"


@contextmanager
def running_mirror(t0: str = T0) -> Iterator[str]:
    assert NODE is not None
    process = subprocess.Popen(
        [NODE, str(SERVE)],
        cwd=MIRROR,
        env={**os.environ, "PORT": "0", "NODE_ENV": "development", "SCENARIO_T0": t0},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    url = None
    for line in process.stdout:
        match = re.search(r"server listening on \{ url: '([^']+)'", line)
        if match:
            url = match.group(1)
            break
    if url is None:
        process.kill()
        raise RuntimeError("Mirror did not start")
    # Keep draining the log so the server never blocks on a full pipe.
    threading.Thread(target=lambda: [None for _ in process.stdout or []], daemon=True).start()
    try:
        yield url
    finally:
        process.kill()
        process.wait()
