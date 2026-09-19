from __future__ import annotations

import asyncio

from kotsin_crypto.bus import Bus, Policy, Topic


async def test_market_data_drops_oldest_and_counts() -> None:
    bus = Bus()
    sub = bus.subscribe(Topic.BOOK, maxsize=2)
    for i in range(5):
        await bus.publish(Topic.BOOK, i)
    assert sub.policy is Policy.DROP_OLDEST
    assert sub.dropped == 3
    assert [sub.queue.get_nowait(), sub.queue.get_nowait()] == [3, 4]
    assert bus.stats()["book"]["published"] == 5


async def test_order_topics_block_instead_of_dropping() -> None:
    bus = Bus()
    sub = bus.subscribe(Topic.ORDER_INTENT, maxsize=1)
    assert sub.policy is Policy.BLOCK
    await bus.publish(Topic.ORDER_INTENT, "a")
    second = asyncio.create_task(bus.publish(Topic.ORDER_INTENT, "b"))
    await asyncio.sleep(0)
    assert not second.done()  # blocked, nothing lost
    assert sub.queue.get_nowait() == "a"
    await second
    assert sub.queue.get_nowait() == "b"
    assert sub.dropped == 0
