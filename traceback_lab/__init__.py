"""Traceback Lab: run every cfDNA algorithm a kit BAM supports, unsigned.

The Lab is a parallel subsystem to the signed path (``traceback_runner``).  It
never edits signed code; it reuses public helpers by import.  Results carry a
provenance stamp only (no signing).  Research use only; not CLIA-validated.
"""

from __future__ import annotations

__version__ = "0.1.0"

# The on-disk layout version of ``ROOT/lab``.  A store written by a newer
# checkout is refused (TBX-LAB-018); a store is never downgraded.
STORE_VERSION = 1

__all__ = ["STORE_VERSION", "__version__"]
