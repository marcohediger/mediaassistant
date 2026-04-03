"""Load balancer for multiple OpenAI-compatible AI backends.

Picks whichever backend has a free slot. Each backend can have
multiple slots (configurable), allowing parallel requests to
the same server. Each backend can be independently enabled/disabled.
"""

import asyncio
from contextlib import asynccontextmanager
from config import config_manager


# Semaphore per backend — value = number of slots
_semaphores: dict[int, asyncio.Semaphore] = {}


async def _load_backends() -> list[dict]:
    """Load all configured and enabled backends from config."""
    backends = []

    # Backend 1 — enabled via module "ki_analyse"
    if await config_manager.is_module_enabled("ki_analyse"):
        url1 = await config_manager.get("ai.backend_url")
        model1 = await config_manager.get("ai.model")
        if url1 and model1:
            slots = int(await config_manager.get("ai.slots", 1))
            slots = max(1, slots)
            backends.append({
                "url": url1,
                "model": model1,
                "api_key": await config_manager.get("ai.api_key", "not-needed"),
                "id": 0,
                "slots": slots,
            })

    # Backend 2 — enabled via module "ki_analyse_2"
    if await config_manager.is_module_enabled("ki_analyse_2"):
        url2 = await config_manager.get("ai2.backend_url")
        model2 = await config_manager.get("ai2.model")
        if url2 and model2:
            slots = int(await config_manager.get("ai2.slots", 1))
            slots = max(1, slots)
            backends.append({
                "url": url2,
                "model": model2,
                "api_key": await config_manager.get("ai2.api_key", "not-needed"),
                "id": 1,
                "slots": slots,
            })

    return backends


def _get_semaphore(backend_id: int, slots: int) -> asyncio.Semaphore:
    """Get or create semaphore for a backend, recreating if slots changed."""
    existing = _semaphores.get(backend_id)
    if existing is None:
        _semaphores[backend_id] = asyncio.Semaphore(slots)
    elif getattr(existing, '_initial_slots', None) != slots:
        # Slot count changed — only recreate if no slots are currently in use
        if not existing.locked():
            _semaphores[backend_id] = asyncio.Semaphore(slots)
            _semaphores[backend_id]._initial_slots = slots
    sem = _semaphores[backend_id]
    sem._initial_slots = slots
    return sem


async def get_total_slots() -> int:
    """Return total number of available slots across all enabled backends.

    Used by the pipeline worker to determine max concurrent jobs.
    """
    backends = await _load_backends()
    if not backends:
        return 1
    return sum(b["slots"] for b in backends)


@asynccontextmanager
async def acquire_ai_backend():
    """Context manager that yields an idle AI backend.

    Usage:
        async with acquire_ai_backend() as backend:
            if backend is None:
                return  # not configured / all disabled
            # use backend["url"], backend["model"], backend["api_key"]

    Picks a backend with free slots. If all slots are busy,
    waits for the first one to become free.
    """
    backends = await _load_backends()
    if not backends:
        yield None
        return

    sems = {b["id"]: _get_semaphore(b["id"], b["slots"]) for b in backends}

    # Try to acquire any free backend (non-blocking first pass)
    for backend in backends:
        sem = sems[backend["id"]]
        if sem.locked():
            continue
        await sem.acquire()
        try:
            yield backend
        finally:
            sem.release()
        return

    # All busy — wait for the first one that becomes free
    backend_ids = [b["id"] for b in backends]
    done, pending = await asyncio.wait(
        [asyncio.create_task(_acquire_sem(sems[bid], bid)) for bid in backend_ids],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel the other waiters
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Get the backend id from the completed task
    won_id = done.pop().result()
    backend = next(b for b in backends if b["id"] == won_id)
    try:
        yield backend
    finally:
        sems[won_id].release()


async def _acquire_sem(sem: asyncio.Semaphore, backend_id: int) -> int:
    """Acquire semaphore and return the backend id."""
    await sem.acquire()
    return backend_id
