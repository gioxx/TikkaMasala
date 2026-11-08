"""``finish_or_continue``: answer within a grace period, let longer work finish on the loop."""

from __future__ import annotations

import asyncio
import logging

import pytest

import app.background as background
from app.background import finish_or_continue


def test_finish_or_continue_returns_result():
    async def work():
        return 5

    assert asyncio.run(finish_or_continue(work(), name="t")) == (True, 5)


def test_finish_or_continue_propagates_error():
    async def work():
        raise ValueError("bad")

    with pytest.raises(ValueError):
        asyncio.run(finish_or_continue(work(), name="t"))


def test_finish_or_continue_keeps_running():
    flag: list[bool] = []

    async def work():
        await asyncio.sleep(0.05)
        flag.append(True)
        return "late"

    async def scenario():
        result = await finish_or_continue(work(), name="t", grace=0.01)
        pending = len(background._background_tasks)
        assert flag == []
        await asyncio.sleep(0.1)
        return result, pending

    result, pending = asyncio.run(scenario())
    assert result == (False, None)
    assert pending == 1
    assert flag == [True]
    assert background._background_tasks == set()


def test_late_failure_is_logged(caplog):
    caplog.set_level(logging.ERROR, logger="app.background")

    async def work():
        await asyncio.sleep(0.03)
        raise RuntimeError("boom")

    async def scenario():
        result = await finish_or_continue(work(), name="t", grace=0.01)
        await asyncio.sleep(0.1)
        return result

    assert asyncio.run(scenario()) == (False, None)
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert [record.getMessage() for record in errors] == ["Background task t failed."]
    assert isinstance(errors[0].exc_info[1], RuntimeError)


def test_early_failure_is_not_logged(caplog):
    async def work():
        raise ValueError("bad")

    with pytest.raises(ValueError):
        asyncio.run(finish_or_continue(work(), name="t"))
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_grace_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(background, "BACKGROUND_GRACE_SECONDS", 0.01)

    async def work():
        await asyncio.sleep(0.05)

    async def scenario():
        result = await finish_or_continue(work(), name="t")
        await asyncio.sleep(0.1)
        return result

    assert asyncio.run(scenario()) == (False, None)


def test_caller_cancelled_while_waiting_lets_work_finish(caplog):
    flag: list[bool] = []

    async def work():
        await asyncio.sleep(0.05)
        flag.append(True)

    async def scenario():
        waiter = asyncio.create_task(finish_or_continue(work(), name="t", grace=5))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.sleep(0.1)

    asyncio.run(scenario())
    assert flag == [True]
    assert background._background_tasks == set()
