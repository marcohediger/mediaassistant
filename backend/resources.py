"""Resource limits instead of job slots (docs/UMBAU_MODULE_AUSFUEHRUNG.md 3.3, 3.4).

A job holds only the limit of the module it runs right now:

    ai      the AI backends' slots together (ai_backends.get_total_slots)   IA-05, IA-06
    geo     pipeline.geo_slots — 1 for Nominatim (1 request/s), else 4     IA-03
    immich  pipeline.immich_slots, 2                                        IA-08 (Immich), downloads
    io      pipeline.io_slots, 2                                            IA-08 (library), error/ moves
    cpu     pipeline.cpu_slots, max(2, CPUs / 2)                            IA-01, IA-02, IA-04, IA-07

and the worker runs up to ``max_jobs()`` jobs at once (pipeline.max_jobs,
0 = 2 × AI slots + 2), which only protects database connections and
memory. No module holds two limits, so nothing can deadlock.

The sizes are read from the configuration at every acquisition (cached
for 5 s). Waiters are served by (priority, job order, ticket): priority 0
is the pipeline, 1 a rerun; a job claimed earlier (further along) goes
first; the ticket keeps it first come, first served otherwise. Per
process — a test script next to the app has limits of its own.
"""
import asyncio
import heapq
import itertools
import os
import time
from contextlib import asynccontextmanager

from config import config_manager

CLASSES = ("ai", "geo", "immich", "io", "cpu")
_CACHE_S = 5.0
_cache: dict[str, tuple[float, int]] = {}
_tickets = itertools.count()
_job_order = itertools.count()


def next_job_order() -> int:
    """Claim order of a job in this process (smaller = claimed earlier)."""
    return next(_job_order)


async def _config_int(key: str, default: int) -> int:
    try:
        value = int(await config_manager.get(key, default) or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else default


async def _compute(name: str) -> int:
    if name == "ai":
        from ai_backends import get_total_slots
        return max(1, await get_total_slots())
    if name == "geo":
        provider = await config_manager.get("geo.provider", "nominatim") or "nominatim"
        return await _config_int("pipeline.geo_slots", 1 if provider == "nominatim" else 4)
    if name == "immich":
        return await _config_int("pipeline.immich_slots", 2)
    if name == "io":
        return await _config_int("pipeline.io_slots", 2)
    if name == "cpu":
        return await _config_int("pipeline.cpu_slots", max(2, (os.cpu_count() or 2) // 2))
    if name == "jobs":
        explicit = await _config_int("pipeline.max_jobs", 0)
        return explicit if explicit > 0 else 2 * await size("ai") + 2
    raise ValueError(f"unknown resource {name!r}")


async def size(name: str) -> int:
    hit = _cache.get(name)
    now = time.monotonic()
    if hit and now - hit[0] < _CACHE_S:
        return hit[1]
    value = await _compute(name)
    _cache[name] = (now, value)
    return value


def forget_sizes() -> None:
    """Drop the cached sizes (tests change the configuration)."""
    _cache.clear()


async def max_jobs() -> int:
    """How many jobs the worker runs at once."""
    return await size("jobs")


class _Limit:
    def __init__(self, name: str):
        self.name = name
        self.in_use = 0
        self.waiting: list = []      # heap of (priority, order, ticket, future)
        self.peak = 0

    def _grant_waiters(self, capacity: int) -> None:
        while self.waiting and self.in_use < capacity:
            _p, _o, _t, fut = heapq.heappop(self.waiting)
            if fut.done():
                continue  # cancelled while waiting
            self.in_use += 1
            self.peak = max(self.peak, self.in_use)
            fut.set_result(True)


_limits: dict[str, _Limit] = {name: _Limit(name) for name in CLASSES}


def _known_size(name: str) -> int:
    hit = _cache.get(name)
    return hit[1] if hit else 1


def would_wait(name: str) -> bool:
    """True if a new request for `name` cannot be served right away (as
    far as known without reading the configuration)."""
    limit = _limits.get(name)
    if limit is None:
        return False
    return any(not w[3].done() for w in limit.waiting) or limit.in_use >= _known_size(name)


@asynccontextmanager
async def use(name: str | None, *, priority: int = 0, order: int = 0):
    """Hold one unit of `name` for the block ("none" / None: no limit)."""
    limit = _limits.get(name) if name else None
    if limit is None:
        yield
        return
    capacity = await size(name)
    limit._grant_waiters(capacity)  # the size may have grown since the last release
    if limit.in_use < capacity and not any(not w[3].done() for w in limit.waiting):
        limit.in_use += 1
        limit.peak = max(limit.peak, limit.in_use)
    else:
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(limit.waiting, (priority, order, next(_tickets), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # Granted just before the cancel arrived: give it back.
                limit.in_use -= 1
                limit._grant_waiters(_known_size(name))
            else:
                fut.cancel()
            raise
    try:
        yield
    finally:
        limit.in_use -= 1
        limit._grant_waiters(_known_size(name))


def snapshot() -> dict:
    """In use, waiting and the known size per class (diagnostics)."""
    out = {}
    for name, limit in _limits.items():
        hit = _cache.get(name)
        out[name] = {"in_use": limit.in_use, "waiting": sum(1 for w in limit.waiting if not w[3].done()),
                     "size": hit[1] if hit else None, "peak": limit.peak}
    hit = _cache.get("jobs")
    out["jobs"] = {"size": hit[1] if hit else None}
    return out


def reset_peaks() -> None:
    for limit in _limits.values():
        limit.peak = limit.in_use
