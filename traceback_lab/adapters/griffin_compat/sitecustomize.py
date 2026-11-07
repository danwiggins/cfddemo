"""Compatibility for Snakemake 5.19.2's ratelimiter on Python 3.11+."""

import asyncio
from types import coroutine


if not hasattr(asyncio, "coroutine"):
    asyncio.coroutine = coroutine
