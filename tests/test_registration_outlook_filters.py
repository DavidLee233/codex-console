import asyncio
from contextlib import contextmanager
from pathlib import Path

from fastapi import BackgroundTasks

from src.database.models import Account, Base, EmailService, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.web.routes import registration as registration_routes


def _build_test_db(name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_available_services_hides_registered_outlook_accounts(monkeypatch):
    manager = _build_test_db("registration_outlook_available_services.db")

    with manager.session_scope() as session:
        session.add_all(
            [
                EmailService(
                    name="alpha@outlook.com",
                    service_type="outlook",
                    enabled=True,
                    priority=0,
                    config={"email": "alpha@outlook.com", "client_id": "cid-1", "refresh_token": "rt-1"},
                ),
                EmailService(
                    name="beta@outlook.com",
                    service_type="outlook",
                    enabled=True,
                    priority=1,
                    config={"email": "beta@outlook.com"},
                ),
                EmailService(
                    name="gamma@outlook.com",
                    service_type="outlook",
                    enabled=True,
                    priority=2,
                    config={"email": "gamma@outlook.com"},
                ),
            ]
        )
        session.add(
            Account(
                email="beta@outlook.com",
                password="pwd",
                email_service="outlook",
                status="active",
            )
        )
        session.add(
            Account(
                email="gamma@outlook.com",
                password="pwd",
                email_service="outlook",
                status="failed",
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    result = asyncio.run(registration_routes.get_available_email_services())
    outlook_services = result["outlook"]["services"]

    assert result["outlook"]["available"] is True
    assert result["outlook"]["count"] == 1
    assert [service["name"] for service in outlook_services] == ["alpha@outlook.com"]
    assert outlook_services[0]["email"] == "alpha@outlook.com"


def test_outlook_accounts_endpoint_returns_only_unregistered_accounts(monkeypatch):
    manager = _build_test_db("registration_outlook_accounts_endpoint.db")

    with manager.session_scope() as session:
        session.add_all(
            [
                EmailService(
                    name="one@outlook.com",
                    service_type="outlook",
                    enabled=True,
                    priority=0,
                    config={"email": "one@outlook.com", "client_id": "cid-1", "refresh_token": "rt-1"},
                ),
                EmailService(
                    name="two@outlook.com",
                    service_type="outlook",
                    enabled=True,
                    priority=1,
                    config={"email": "two@outlook.com"},
                ),
                EmailService(
                    name="three@outlook.com",
                    service_type="outlook",
                    enabled=True,
                    priority=2,
                    config={"email": "three@outlook.com"},
                ),
            ]
        )
        session.add(
            Account(
                email="two@outlook.com",
                password="pwd",
                email_service="outlook",
                status="active",
            )
        )
        session.add(
            Account(
                email="three@outlook.com",
                password="pwd",
                email_service="outlook",
                status="failed",
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    response = asyncio.run(registration_routes.get_outlook_accounts_for_registration())

    assert response.total == 1
    assert response.registered_count == 2
    assert response.unregistered_count == 1
    assert [account.email for account in response.accounts] == ["one@outlook.com"]
    assert response.accounts[0].is_registered is False


def test_available_services_hides_existing_openai_failed_outlook_accounts(monkeypatch):
    manager = _build_test_db("registration_outlook_failed_existing_openai.db")

    with manager.session_scope() as session:
        blocked_service = EmailService(
            name="blocked@outlook.com",
            service_type="outlook",
            enabled=True,
            priority=0,
            config={"email": "blocked@outlook.com"},
        )
        available_service = EmailService(
            name="available@outlook.com",
            service_type="outlook",
            enabled=True,
            priority=1,
            config={"email": "available@outlook.com"},
        )
        session.add_all([blocked_service, available_service])
        session.flush()

        session.add(
            RegistrationTask(
                task_uuid="task-existing-openai-marker-1",
                status="failed",
                email_service_id=blocked_service.id,
                error_message="该邮箱已存在 OpenAI 账号",
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    result = asyncio.run(registration_routes.get_available_email_services())
    outlook_emails = [service["email"] for service in result["outlook"]["services"]]

    assert result["outlook"]["count"] == 1
    assert outlook_emails == ["available@outlook.com"]


def test_outlook_accounts_endpoint_hides_existing_openai_failed_outlook_accounts(monkeypatch):
    manager = _build_test_db("registration_outlook_accounts_existing_openai.db")

    with manager.session_scope() as session:
        blocked_service = EmailService(
            name="blocked2@outlook.com",
            service_type="outlook",
            enabled=True,
            priority=0,
            config={"email": "blocked2@outlook.com"},
        )
        available_service = EmailService(
            name="available2@outlook.com",
            service_type="outlook",
            enabled=True,
            priority=1,
            config={"email": "available2@outlook.com"},
        )
        session.add_all([blocked_service, available_service])
        session.flush()

        session.add(
            RegistrationTask(
                task_uuid="task-existing-openai-marker-2",
                status="failed",
                email_service_id=blocked_service.id,
                logs="register failed because account already exists",
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    response = asyncio.run(registration_routes.get_outlook_accounts_for_registration())

    assert response.total == 1
    assert response.registered_count == 1
    assert response.unregistered_count == 1
    assert [account.email for account in response.accounts] == ["available2@outlook.com"]


def test_outlook_batch_start_skips_existing_openai_failed_accounts(monkeypatch):
    manager = _build_test_db("registration_outlook_batch_skip_existing_openai.db")

    with manager.session_scope() as session:
        blocked_service = EmailService(
            name="blocked3@outlook.com",
            service_type="outlook",
            enabled=True,
            priority=0,
            config={"email": "blocked3@outlook.com"},
        )
        available_service = EmailService(
            name="available3@outlook.com",
            service_type="outlook",
            enabled=True,
            priority=1,
            config={"email": "available3@outlook.com"},
        )
        session.add_all([blocked_service, available_service])
        session.flush()

        session.add(
            RegistrationTask(
                task_uuid="task-existing-openai-marker-3",
                status="failed",
                email_service_id=blocked_service.id,
                error_message="该邮箱可能已在 OpenAI 注册",
            )
        )

        blocked_service_id = blocked_service.id
        available_service_id = available_service.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    request = registration_routes.OutlookBatchRegistrationRequest(
        service_ids=[blocked_service_id, available_service_id],
        skip_registered=True,
        interval_min=0,
        interval_max=0,
        concurrency=1,
        mode="pipeline",
    )
    response = asyncio.run(
        registration_routes.start_outlook_batch_registration(request, BackgroundTasks())
    )

    assert response.skipped == 1
    assert response.to_register == 1
    assert response.service_ids == [available_service_id]
