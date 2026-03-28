import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.database.models import Account, Base, EmailService
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
