"""omni-homevlog: a continuity-preserving home-vlog video agent.

Built on Gemini Omni via the Interactions API. See `README.md` for the two
surfaces, their verification status, and the design rules this package enforces.

The package is deliberately import-light: nothing here reaches for credentials,
network, or ffmpeg at import time, so `import omni_homevlog` is always safe.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
