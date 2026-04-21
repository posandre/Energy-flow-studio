from __future__ import annotations

import os


# Force headless Qt backend for GUI tests to avoid AppKit crashes in CLI/sandboxed runs.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
