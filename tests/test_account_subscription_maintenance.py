from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.core.openai import payment as payment_core
from src.database.models import Account, Base
from src.database.session import DatabaseSessionManager
from src.web.routes import accounts as accounts_routes


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"{}"
        self.headers = {}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_run_scheduled_account_token_maintenance_aggregates_refresh_and_validate(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "account_token_maintenance.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        first = Account(email="alpha@example.com", password="pwd", email_service="tempmail", status="active")
        second = Account(email="beta@example.com", password="pwd", email_service="outlook", status="active")
        session.add_all([first, second])
        session.flush()
        account_ids = [first.id, second.id]

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    refresh_calls = []
    validate_calls = []

    def fake_refresh(account_id, proxy):
        refresh_calls.append((account_id, proxy))
        if account_id == account_ids[0]:
            return SimpleNamespace(success=True, error_message=None)
        return SimpleNamespace(success=False, error_message="refresh failed")

    def fake_validate(account_id, proxy):
        validate_calls.append((account_id, proxy))
        if account_id == account_ids[0]:
            return True, None
        return False, "token expired"

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr(accounts_routes, "_get_proxy", lambda value: "http://proxy.local:7890")
    monkeypatch.setattr(accounts_routes, "do_refresh", fake_refresh)
    monkeypatch.setattr(accounts_routes, "do_validate", fake_validate)

    result = accounts_routes.run_scheduled_account_token_maintenance()

    assert result["account_count"] == 2
    assert result["proxy"] == "http://proxy.local:7890"
    assert refresh_calls == [(account_ids[0], "http://proxy.local:7890"), (account_ids[1], "http://proxy.local:7890")]
    assert validate_calls == [(account_ids[0], "http://proxy.local:7890"), (account_ids[1], "http://proxy.local:7890")]
    assert result["refresh"]["success_count"] == 1
    assert result["refresh"]["failed_count"] == 1
    assert result["validate"]["valid_count"] == 1
    assert result["validate"]["invalid_count"] == 1


def test_check_subscription_status_detail_detects_trial_offer_as_team(monkeypatch):
    account = Account(
        email="promo@example.com",
        password="pwd",
        email_service="outlook",
        access_token="access-token",
        subscription_type=None,
    )

    def fake_get(url, headers=None, proxies=None, timeout=None, impersonate=None):
        assert url == "https://chatgpt.com/backend-api/me"
        return FakeResponse(
            {
                "plan_type": "free",
                "offers": [
                    {
                        "target_plan": "chatgptteamplan",
                        "promo_campaign": {"promo_campaign_id": "team-1-month-free"},
                        "amount": 0,
                    }
                ],
            }
        )

    monkeypatch.setattr(payment_core.cffi_requests, "get", fake_get)

    detail = payment_core.check_subscription_status_detail(account)

    assert detail["status"] == "team"
    assert detail["note"] == "promo_offer_detected"
    assert "promo" in str(detail["source"])
