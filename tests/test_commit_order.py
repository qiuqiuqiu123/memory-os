"""A visible high cursor must never hide a later-committing lower cursor."""

import concurrent.futures
import threading
import uuid
from test_core import core, event


def test_commit_visibility_and_rollback_clock(core):
    user = str(uuid.uuid4())
    with core.connection() as first:
        one = core.append(user, "a", [event(content="first")], first)
        entered = threading.Event()

        def second():
            entered.set()
            return core.append(user, "a", [event(content="second")])

        pool = concurrent.futures.ThreadPoolExecutor(1)
        future = pool.submit(second)
        assert entered.wait(2)
        # first is uncommitted; no events may be visible to the reader yet.
        assert core.sync(user, 0, 100)["events"] == []
        assert not future.done()
    two = future.result(timeout=5)
    pool.shutdown()
    events = core.sync(user, 0, 100)["events"]
    assert [e["id"] for e in events] == [one["last_version"], two["last_version"]]
    with core.connection() as tx:
        abandoned = core.append(user, "a", [event(content="rollback")], tx)
        tx.rollback()
    next_ack = core.append(user, "a", [event(content="after rollback")])
    assert next_ack["last_version"] == abandoned["last_version"]
