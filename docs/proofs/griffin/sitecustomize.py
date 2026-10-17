"""Synthetic-proof-only Python 3.11 shim for Snakemake 5.19.2 ratelimiter."""

import asyncio
import types

if not hasattr(asyncio, 'coroutine'):
    asyncio.coroutine = types.coroutine
