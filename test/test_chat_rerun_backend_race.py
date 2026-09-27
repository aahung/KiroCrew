"""Destructive reruns serialize verification and persistence with config PATCH."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.config.loader import KiroCrewConfig, update_config_locked
from kiro_crew.dashboard import chat_persistence, chat_regenerate, chat_rewind, kiro_readiness
from kiro_crew.dashboard.handlers import agents, core

_TIMEOUT = 30
_ROUTES = (
    ("regenerate", chat_regenerate.api_chat_slot_regenerate),
    ("edit-resend", chat_regenerate.api_chat_slot_edit_resend),
    ("rewind", chat_rewind.api_chat_slot_rewind),
)


@web.middleware
async def _owner(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


@asynccontextmanager
async def _rerun_client(tmp_path, monkeypatch):
    await asyncio.to_thread(
        update_config_locked,
        mutate=lambda data: {**data, "agent": {"acp_backend": "", "member_acp_backend": ""}},
    )
    state = _make_state(tmp_path)
    state.owner_id = ""
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    state.sessions._session_map.get.return_value = ""
    run = AsyncMock()
    monkeypatch.setattr(chat_regenerate, "_run_chat", run)
    monkeypatch.setattr(chat_rewind, "_run_chat", run)
    monkeypatch.setattr(core, "_hot_apply_after_write", AsyncMock())
    app = web.Application(middlewares=[_owner])
    app["state"] = state
    for route, handler in _ROUTES:
        app.router.add_post(f"/api/chat/slots/{{slot}}/{route}", handler)
    app.router.add_patch("/api/config/kirocrew", core.api_kirocrew_config_patch)
    async with TestClient(TestServer(app)) as client:
        try:
            yield client, state, run
        finally:
            await asyncio.wait_for(drain_background_tasks(state), _TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [name for name, _ in _ROUTES])
@pytest.mark.parametrize("member", [False, True], ids=["default", "member"])
async def test_backend_switch_during_probe_preserves_history(tmp_path, monkeypatch, route, member):
    async with _rerun_client(tmp_path, monkeypatch) as harness:
        await _switch_during_probe(harness, monkeypatch, route, member)


async def _switch_during_probe(harness, monkeypatch, route, member):
    client, state, run = harness
    slot = state.get_or_create_slot("source")
    if member:
        slot.linked_session_key = "member-reviewer"
    slot.append("user", "original prompt")
    slot.append("assistant", "original answer")
    await asyncio.to_thread(state.flush_slot_now, slot)
    from kiro_crew.dashboard.chat_utils import slot_history_key

    history_key = slot_history_key(slot)
    before = await asyncio.to_thread(state.conversation_log.read_messages, history_key)
    original = list(slot.messages)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_probe(_service):
        entered.set()
        await asyncio.wait_for(release.wait(), _TIMEOUT)
        return True

    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", paused_probe)
    request = asyncio.create_task(
        client.post(
            f"/api/chat/slots/source/{route}",
            json={"index": 0, "content": "edited prompt"},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), _TIMEOUT)
        if member:
            # The member field is config-only; use the dashboard's writer lock.
            from kiro_crew.dashboard.chat_utils import run_config_write

            def switch_member(data):
                data["agent"]["member_acp_backend"] = "codex"
                return data

            await asyncio.wait_for(
                run_config_write(update_config_locked, mutate=switch_member), _TIMEOUT
            )
        else:
            changed = await asyncio.wait_for(
                client.patch(
                    "/api/config/kirocrew",
                    json={"path": "agent.acp_backend", "value": "codex"},
                ),
                _TIMEOUT,
            )
            assert changed.status == 200
        release.set()
        response = await asyncio.wait_for(request, _TIMEOUT)
    finally:
        release.set()
        await asyncio.wait_for(request, _TIMEOUT)

    assert response.status == 503
    assert (await response.json())["code"] == "backend_readiness_unsupported"
    assert slot.messages == original
    after = await asyncio.to_thread(state.conversation_log.read_messages, history_key)
    assert after == before
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("route,handler", _ROUTES, ids=[name for name, _ in _ROUTES])
@pytest.mark.parametrize("cancel", [False, True], ids=["completed", "cancelled"])
async def test_config_patch_waits_for_the_durable_rerun(
    tmp_path, monkeypatch, route, handler, cancel
):
    async with _rerun_client(tmp_path, monkeypatch) as (client, state, _run):
        slot = state.get_or_create_slot("source")
        slot.append("user", "original prompt")
        slot.append("assistant", "original answer")
        await asyncio.to_thread(state.flush_slot_now, slot)
        lock = agents._get_config_lock()
        patch_waiting = asyncio.Event()

        @asynccontextmanager
        async def observed_lock():
            if lock.locked():
                patch_waiting.set()
            async with lock:
                yield

        monkeypatch.setattr(agents, "_get_config_lock", observed_lock)
        entered = threading.Event()
        release = threading.Event()
        save = chat_persistence._save_slot_to_history
        commit_backends = []

        def paused_save(*args, **kwargs):
            entered.set()
            assert release.wait(_TIMEOUT), "test did not release the history worker"
            commit_backends.append(KiroCrewConfig.load().agent.acp_backend)
            return save(*args, **kwargs)

        monkeypatch.setattr(chat_persistence, "_save_slot_to_history", paused_save)
        monkeypatch.setattr(chat_rewind, "_save_slot_to_history", paused_save)
        request = make_mocked_request(
            "POST",
            f"/api/chat/slots/source/{route}",
            app=client.server.app,
            match_info={"slot": "source"},
        )
        request.json = AsyncMock(return_value={"index": 0, "content": "edited prompt"})
        rerun = asyncio.create_task(handler(request))
        patch_task = None
        try:
            assert await asyncio.to_thread(entered.wait, _TIMEOUT), "history worker not reached"
            assert lock.locked(), "rerun must hold the config lock through its durable write"
            if cancel:
                rerun.cancel()
            patch_task = asyncio.create_task(
                client.patch(
                    "/api/config/kirocrew",
                    json={"path": "agent.acp_backend", "value": "codex"},
                )
            )
            await asyncio.wait_for(patch_waiting.wait(), _TIMEOUT)
            assert not patch_task.done()
            assert (await asyncio.to_thread(KiroCrewConfig.load)).agent.acp_backend == ""
            if cancel:
                rerun.cancel()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(rerun, _TIMEOUT)
            else:
                assert (await asyncio.wait_for(rerun, _TIMEOUT)).status == 200
            assert (await asyncio.wait_for(patch_task, _TIMEOUT)).status == 200
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(rerun, return_exceptions=True), _TIMEOUT)
            if patch_task is not None:
                await asyncio.wait_for(patch_task, _TIMEOUT)
        assert commit_backends == [""]
        assert (await asyncio.to_thread(KiroCrewConfig.load)).agent.acp_backend == "codex"
        assert not lock.locked()


@pytest.mark.asyncio
async def test_cancelled_config_patch_retains_lock_until_its_worker_settles(tmp_path, monkeypatch):
    from kiro_crew.config import loader

    async with _rerun_client(tmp_path, monkeypatch) as (client, state, run):
        entered = threading.Event()
        release = threading.Event()
        write = loader.update_config_locked

        def paused_write(*args, **kwargs):
            entered.set()
            assert release.wait(_TIMEOUT), "test did not release the config worker"
            return write(*args, **kwargs)

        monkeypatch.setattr(loader, "update_config_locked", paused_write)
        request = make_mocked_request("PATCH", "/api/config/kirocrew", app=client.server.app)
        request["user"] = "local-app"
        request["app"] = ""
        request.json = AsyncMock(return_value={"path": "agent.acp_backend", "value": "codex"})
        task = asyncio.create_task(core.api_kirocrew_config_patch(request))
        lock = agents._get_config_lock()
        lock_attempted = asyncio.Event()

        async def wait_for_config():
            lock_attempted.set()
            async with lock:
                return (await asyncio.to_thread(KiroCrewConfig.load)).agent.acp_backend

        waiter = None
        try:
            assert await asyncio.to_thread(entered.wait, _TIMEOUT), "config worker not reached"
            task.cancel()
            waiter = asyncio.create_task(wait_for_config())
            await asyncio.wait_for(lock_attempted.wait(), _TIMEOUT)
            assert lock.locked()
            assert not waiter.done()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, _TIMEOUT)
            assert await asyncio.wait_for(waiter, _TIMEOUT) == "codex"
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), _TIMEOUT)
            if waiter is not None:
                await asyncio.wait_for(waiter, _TIMEOUT)
        assert not lock.locked()
        run.assert_not_awaited()
