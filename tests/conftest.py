"""Pytest configuration shared across the PharmaPulse test tree.

Ensures the repository root is importable so ``shared``, ``servers``,
``backend``, and ``stacks`` resolve without an editable install.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
