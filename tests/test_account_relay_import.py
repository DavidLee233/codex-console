import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.database.models import Account, Base, CpaService, Sub2ApiService
from src.database.session import DatabaseSessionManager
from src.web.routes import accounts as accounts_routes


def _build_test_db(name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_batch_import_relays_requires_enabled_targets(monkeypatch):
    manager = _build_test_db("account_relay_import_no_targets.db")

    with manager.session_scope() as session:
        account = Account(
            email="alpha@example.com",
            password="pwd",
            email_service="tempmail",
            access_token="token-alpha",
            status="active",
        )
        session.add(account)
        session.flush()
        account_id = account.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr(accounts_routes, "_get_proxy", lambda value=None: None)
    monkeypatch.setattr(accounts_routes, "_resolve_relay_import_targets", lambda db: {"cpa": None, "sub2api": None})

    request = accounts_routes.BatchRelayImportRequest(ids=[account_id])

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(accounts_routes.batch_import_accounts_to_relays(request))

    assert exc_info.value.status_code == 400
    assert "均未启用" in str(exc_info.value.detail)


def test_batch_import_relays_uploads_to_cpa_and_sub2api(monkeypatch):
    manager = _build_test_db("account_relay_import_success.db")

    with manager.session_scope() as session:
        account = Account(
            email="relay@example.com",
            password="pwd",
            email_service="tempmail",
            access_token="token-relay",
            refresh_token="refresh-relay",
            client_id="client-1",
            account_id="acct-1",
            workspace_id="ws-1",
            status="active",
        )
        session.add(account)
        session.flush()
        account_id = account.id
        session.add(CpaService(name="CPA Main", api_url="http://cpa.local", api_token="cpa-token", enabled=True, priority=0))
        session.add(Sub2ApiService(name="Sub2API Main", api_url="http://sub2api.local", api_key="sub2api-key", enabled=True, priority=0))

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    cpa_calls = []
    sub2api_calls = []

    def fake_overview(db, account, force_refresh=False, proxy=None, allow_network=True):
        return (
            {
                "plan_type": "Team",
                "hourly_quota": {"status": "ok", "percentage": 10},
                "weekly_quota": {"status": "ok", "percentage": 20},
            },
            False,
        )

    def fake_upload_to_cpa(token_data, proxy=None, api_url=None, api_token=None):
        cpa_calls.append({"email": token_data["email"], "api_url": api_url, "api_token": api_token})
        return True, "ok"

    def fake_upload_to_sub2api(accounts, api_url, api_key, concurrency=3, priority=50):
        sub2api_calls.append(
            {
                "emails": [account.email for account in accounts],
                "api_url": api_url,
                "api_key": api_key,
                "concurrency": concurrency,
                "priority": priority,
            }
        )
        return True, "ok"

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr(accounts_routes, "_get_proxy", lambda value=None: "http://proxy.local:7890")
    monkeypatch.setattr(accounts_routes, "_get_account_overview_data", fake_overview)
    monkeypatch.setattr(accounts_routes, "upload_to_cpa", fake_upload_to_cpa)
    monkeypatch.setattr(accounts_routes, "upload_to_sub2api", fake_upload_to_sub2api)

    request = accounts_routes.BatchRelayImportRequest(ids=[account_id], concurrency=6, priority=77)
    result = asyncio.run(accounts_routes.batch_import_accounts_to_relays(request))

    assert result["account_count"] == 1
    assert result["success_count"] == 1
    assert result["partial_count"] == 0
    assert result["failed_count"] == 0
    assert result["targets"] == {"cpa": True, "sub2api": True}
    assert cpa_calls == [{"email": "relay@example.com", "api_url": "http://cpa.local", "api_token": "cpa-token"}]
    assert sub2api_calls == [
        {
            "emails": ["relay@example.com"],
            "api_url": "http://sub2api.local",
            "api_key": "sub2api-key",
            "concurrency": 6,
            "priority": 77,
        }
    ]

    with manager.session_scope() as session:
        updated = session.query(Account).filter(Account.id == account_id).first()
        assert updated is not None
        assert updated.cpa_uploaded is True
        assert updated.cpa_uploaded_at is not None


def test_batch_import_relays_stops_when_quota_refresh_fails(monkeypatch):
    manager = _build_test_db("account_relay_import_quota_fail.db")

    with manager.session_scope() as session:
        account = Account(
            email="blocked@example.com",
            password="pwd",
            email_service="tempmail",
            access_token="token-blocked",
            status="active",
        )
        session.add(account)
        session.flush()
        account_id = account.id
        session.add(CpaService(name="CPA Main", api_url="http://cpa.local", api_token="cpa-token", enabled=True, priority=0))
        session.add(Sub2ApiService(name="Sub2API Main", api_url="http://sub2api.local", api_key="sub2api-key", enabled=True, priority=0))

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    cpa_calls = []
    sub2api_calls = []

    def fake_overview(db, account, force_refresh=False, proxy=None, allow_network=True):
        return (
            {
                "error": "quota refresh failed",
                "hourly_quota": {"status": "unknown"},
                "weekly_quota": {"status": "unknown"},
            },
            False,
        )

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr(accounts_routes, "_get_proxy", lambda value=None: None)
    monkeypatch.setattr(accounts_routes, "_get_account_overview_data", fake_overview)
    monkeypatch.setattr(accounts_routes, "upload_to_cpa", lambda *args, **kwargs: cpa_calls.append(True) or (True, "ok"))
    monkeypatch.setattr(
        accounts_routes,
        "upload_to_sub2api",
        lambda *args, **kwargs: sub2api_calls.append(True) or (True, "ok"),
    )

    request = accounts_routes.BatchRelayImportRequest(ids=[account_id])
    result = asyncio.run(accounts_routes.batch_import_accounts_to_relays(request))

    assert result["success_count"] == 0
    assert result["partial_count"] == 0
    assert result["failed_count"] == 1
    assert result["details"][0]["quota_check"]["success"] is False
    assert "quota refresh failed" in str(result["details"][0]["error"])
    assert cpa_calls == []
    assert sub2api_calls == []
