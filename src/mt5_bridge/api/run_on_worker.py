"""The one bridge between FastAPI's async handlers and the worker thread.

Every terminal call from the API layer — reads included — goes through
``BridgeWorker`` (plan §114: the terminal connection is single-threaded).
"""

import asyncio
from collections.abc import Callable

from mt5_bridge.core.worker import BridgeWorker, CommandPriority
from mt5_bridge.mt5.client import Mt5Client


async def run_on_worker[T](
    worker: BridgeWorker, priority: CommandPriority, command: Callable[[Mt5Client], T]
) -> T:
    return await asyncio.wrap_future(worker.submit(priority, command))
