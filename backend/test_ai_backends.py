"""Tests for ai_backends load balancer.

Run:  python test_ai_backends.py
"""

import asyncio
import sys

# Stub config_manager before importing ai_backends
_module_states = {}
_config_values = {}


class FakeConfigManager:
    async def is_module_enabled(self, name):
        return _module_states.get(name, False)

    async def get(self, key, default=None):
        return _config_values.get(key, default)


# Patch config module
import types
config_mod = types.ModuleType("config")
config_mod.config_manager = FakeConfigManager()
sys.modules["config"] = config_mod

import ai_backends
ai_backends.config_manager = config_mod.config_manager

PASS = 0
FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  PASS  {name}")


def fail(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL  {name}  {detail}")


def _reset():
    """Clear semaphores between tests."""
    ai_backends._semaphores.clear()


def _setup_both():
    """Configure both backends with 1 slot each."""
    _module_states.clear()
    _module_states["ki_analyse"] = True
    _module_states["ki_analyse_2"] = True
    _config_values.clear()
    _config_values["ai.backend_url"] = "http://ai1:1234/v1"
    _config_values["ai.model"] = "model1"
    _config_values["ai2.backend_url"] = "http://ai2:5678/v1"
    _config_values["ai2.model"] = "model2"


async def test_no_backends():
    """Both disabled → yields None."""
    _reset()
    _module_states.clear()
    _module_states["ki_analyse"] = False
    _module_states["ki_analyse_2"] = False
    _config_values.clear()

    async with ai_backends.acquire_ai_backend() as b:
        if b is None:
            ok("no_backends → None")
        else:
            fail("no_backends → None", f"got {b}")


async def test_only_backend1():
    """Only backend 1 enabled."""
    _reset()
    _module_states.clear()
    _module_states["ki_analyse"] = True
    _module_states["ki_analyse_2"] = False
    _config_values.clear()
    _config_values["ai.backend_url"] = "http://ai1:1234/v1"
    _config_values["ai.model"] = "model1"

    async with ai_backends.acquire_ai_backend() as b:
        if b and b["url"] == "http://ai1:1234/v1":
            ok("only_backend1 → ai1")
        else:
            fail("only_backend1 → ai1", f"got {b}")


async def test_only_backend2():
    """Only backend 2 enabled."""
    _reset()
    _module_states.clear()
    _module_states["ki_analyse"] = False
    _module_states["ki_analyse_2"] = True
    _config_values.clear()
    _config_values["ai2.backend_url"] = "http://ai2:5678/v1"
    _config_values["ai2.model"] = "model2"

    async with ai_backends.acquire_ai_backend() as b:
        if b and b["url"] == "http://ai2:5678/v1":
            ok("only_backend2 → ai2")
        else:
            fail("only_backend2 → ai2", f"got {b}")


async def test_both_idle_picks_first():
    """Both enabled and idle → picks backend 1."""
    _reset()
    _setup_both()

    async with ai_backends.acquire_ai_backend() as b:
        if b and b["url"] == "http://ai1:1234/v1":
            ok("both_idle → picks first")
        else:
            fail("both_idle → picks first", f"got {b}")


async def test_backend1_busy_picks_backend2():
    """Backend 1 busy → picks backend 2."""
    _reset()
    _setup_both()

    # Acquire backend 1's semaphore
    sem0 = ai_backends._get_semaphore(0, 1)
    await sem0.acquire()
    try:
        async with ai_backends.acquire_ai_backend() as b:
            if b and b["url"] == "http://ai2:5678/v1":
                ok("backend1_busy → picks ai2")
            else:
                fail("backend1_busy → picks ai2", f"got {b}")
    finally:
        sem0.release()


async def test_backend2_busy_picks_backend1():
    """Backend 2 busy → picks backend 1."""
    _reset()
    _setup_both()

    sem1 = ai_backends._get_semaphore(1, 1)
    await sem1.acquire()
    try:
        async with ai_backends.acquire_ai_backend() as b:
            if b and b["url"] == "http://ai1:1234/v1":
                ok("backend2_busy → picks ai1")
            else:
                fail("backend2_busy → picks ai1", f"got {b}")
    finally:
        sem1.release()


async def test_both_busy_waits():
    """Both busy → waits for first free."""
    _reset()
    _setup_both()

    sem0 = ai_backends._get_semaphore(0, 1)
    sem1 = ai_backends._get_semaphore(1, 1)
    await sem0.acquire()
    await sem1.acquire()

    result = {"backend": None, "done": False}

    async def consumer():
        async with ai_backends.acquire_ai_backend() as b:
            result["backend"] = b
            result["done"] = True

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)

    if result["done"]:
        fail("both_busy → waits", "consumer finished too early")
        sem0.release()
        sem1.release()
        return

    # Release backend 2
    sem1.release()
    await asyncio.sleep(0.05)
    await task

    if result["done"] and result["backend"]["url"] == "http://ai2:5678/v1":
        ok("both_busy → waits, gets ai2 after release")
    else:
        fail("both_busy → waits", f"got {result}")

    sem0.release()


async def test_enabled_but_not_configured():
    """Module enabled but URL/model empty → None."""
    _reset()
    _module_states.clear()
    _module_states["ki_analyse"] = True
    _module_states["ki_analyse_2"] = True
    _config_values.clear()

    async with ai_backends.acquire_ai_backend() as b:
        if b is None:
            ok("enabled_but_not_configured → None")
        else:
            fail("enabled_but_not_configured → None", f"got {b}")


async def test_total_slots_default():
    """Total slots = 2 with two 1-slot backends."""
    _reset()
    _setup_both()

    total = await ai_backends.get_total_slots()
    if total == 2:
        ok("total_slots default = 2")
    else:
        fail("total_slots default = 2", f"got {total}")


async def test_total_slots_custom():
    """Total slots = 4+2 with custom slot counts."""
    _reset()
    _setup_both()
    _config_values["ai.slots"] = 4
    _config_values["ai2.slots"] = 2

    total = await ai_backends.get_total_slots()
    if total == 6:
        ok("total_slots 4+2 = 6")
    else:
        fail("total_slots 4+2 = 6", f"got {total}")


async def test_multi_slot_allows_concurrent():
    """Backend with 3 slots allows 3 concurrent acquires."""
    _reset()
    _module_states.clear()
    _module_states["ki_analyse"] = True
    _module_states["ki_analyse_2"] = False
    _config_values.clear()
    _config_values["ai.backend_url"] = "http://ai1:1234/v1"
    _config_values["ai.model"] = "model1"
    _config_values["ai.slots"] = 3

    acquired = []

    async def grab():
        async with ai_backends.acquire_ai_backend() as b:
            acquired.append(b["url"])
            await asyncio.sleep(0.1)  # hold the slot

    # All 3 should run concurrently
    tasks = [asyncio.create_task(grab()) for _ in range(3)]
    await asyncio.sleep(0.05)  # let them start

    if len(acquired) == 3:
        ok("multi_slot: 3 concurrent acquires")
    else:
        fail("multi_slot: 3 concurrent acquires", f"only {len(acquired)} acquired")

    await asyncio.gather(*tasks)


async def test_multi_slot_blocks_at_limit():
    """Backend with 2 slots blocks the 3rd request."""
    _reset()
    _module_states.clear()
    _module_states["ki_analyse"] = True
    _module_states["ki_analyse_2"] = False
    _config_values.clear()
    _config_values["ai.backend_url"] = "http://ai1:1234/v1"
    _config_values["ai.model"] = "model1"
    _config_values["ai.slots"] = 2

    hold = asyncio.Event()
    acquired_count = 0

    async def grab():
        nonlocal acquired_count
        async with ai_backends.acquire_ai_backend() as b:
            acquired_count += 1
            await hold.wait()

    t1 = asyncio.create_task(grab())
    t2 = asyncio.create_task(grab())
    t3 = asyncio.create_task(grab())

    await asyncio.sleep(0.05)

    if acquired_count == 2:
        ok("multi_slot: 3rd blocked at limit of 2")
    else:
        fail("multi_slot: 3rd blocked at limit of 2", f"acquired={acquired_count}")

    hold.set()
    await asyncio.gather(t1, t2, t3)


async def test_total_slots_none_enabled():
    """No backends enabled → total_slots = 1 (default)."""
    _reset()
    _module_states.clear()
    _module_states["ki_analyse"] = False
    _module_states["ki_analyse_2"] = False
    _config_values.clear()

    total = await ai_backends.get_total_slots()
    if total == 1:
        ok("total_slots none enabled = 1")
    else:
        fail("total_slots none enabled = 1", f"got {total}")


async def main():
    print("\n=== ai_backends Load Balancer Tests ===\n")

    await test_no_backends()
    await test_only_backend1()
    await test_only_backend2()
    await test_both_idle_picks_first()
    await test_backend1_busy_picks_backend2()
    await test_backend2_busy_picks_backend1()
    await test_both_busy_waits()
    await test_enabled_but_not_configured()
    await test_total_slots_default()
    await test_total_slots_custom()
    await test_multi_slot_allows_concurrent()
    await test_multi_slot_blocks_at_limit()
    await test_total_slots_none_enabled()

    print(f"\n{'='*40}")
    print(f"  {PASS} passed, {FAIL} failed")
    print(f"{'='*40}\n")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
