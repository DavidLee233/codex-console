import asyncio
from types import SimpleNamespace

from src.web.routes import registration as registration_routes


class _DummyDbContext:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeLoop:
    def __init__(self):
        self.executor_args = None

    async def run_in_executor(self, executor, func, *args):
        self.executor_args = (executor, func, args)
        return None


def _patch_task_manager(monkeypatch, loop):
    monkeypatch.setattr(registration_routes.task_manager, "get_loop", lambda: loop)
    monkeypatch.setattr(registration_routes.task_manager, "set_loop", lambda _loop: None)
    monkeypatch.setattr(registration_routes.task_manager, "update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration_routes.task_manager, "add_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration_routes.task_manager, "executor", None, raising=False)


def test_run_registration_task_loads_bound_email_service_id(monkeypatch):
    loop = _FakeLoop()
    _patch_task_manager(monkeypatch, loop)
    monkeypatch.setattr(registration_routes, "get_db", lambda: _DummyDbContext())
    monkeypatch.setattr(
        registration_routes.crud,
        "get_registration_task",
        lambda db, task_uuid: SimpleNamespace(email_service_id=42),
    )

    asyncio.run(
        registration_routes.run_registration_task(
            task_uuid="task-1",
            email_service_type="outlook",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
        )
    )

    assert loop.executor_args is not None
    _, _, args = loop.executor_args
    # _run_sync_registration_task(..., email_service_id, ...)
    assert args[4] == 42


def test_run_registration_task_keeps_explicit_email_service_id(monkeypatch):
    loop = _FakeLoop()
    _patch_task_manager(monkeypatch, loop)
    monkeypatch.setattr(registration_routes, "get_db", lambda: _DummyDbContext())
    monkeypatch.setattr(
        registration_routes.crud,
        "get_registration_task",
        lambda db, task_uuid: SimpleNamespace(email_service_id=42),
    )

    asyncio.run(
        registration_routes.run_registration_task(
            task_uuid="task-2",
            email_service_type="outlook",
            proxy=None,
            email_service_config=None,
            email_service_id=99,
        )
    )

    assert loop.executor_args is not None
    _, _, args = loop.executor_args
    assert args[4] == 99
