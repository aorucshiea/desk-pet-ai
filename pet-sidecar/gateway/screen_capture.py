"""Screen capture utility using MSS for fast cross-platform screen capture.

Captures the primary monitor and returns base64-encoded PNG images
compatible with OmniParser's /parse/ endpoint.
"""

from __future__ import annotations

import base64
import io
from typing import Optional


def capture(monitor: int = 0) -> str:
    """Capture the specified monitor and return a base64-encoded PNG.

    Args:
        monitor: Monitor index (0 = primary, 1+ = secondary).
                 Uses mss monitor indexing (index 0 = all monitors combined).

    Returns:
        Base64-encoded PNG string (without ``data:image/`` prefix).
    """
    import mss
    from PIL import Image

    with mss.mss() as sct:
        idx = max(0, min(monitor, len(sct.monitors) - 2))
        monitor_info = sct.monitors[idx + 1]
        sct_img = sct.grab(monitor_info)
        img = Image.frombytes("RGB", sct_img.size, sct_img.rgb)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")
