import asyncio
import time
from contextlib import contextmanager
from pathlib import Path

from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.outlook.base import EmailMessage, ProviderType
from src.services.outlook.email_parser import EmailParser
from src.services.outlook.service import (
    OUTLOOK_POLL_MAX_ROUNDS,
    OUTLOOK_POLL_PREFER_UNSEEN_ROUNDS,
    OutlookService,
)
from src.web.routes import accounts as accounts_routes
from src.web.routes import email as email_routes


class DummyProvider:
    def __init__(self, provider_type: ProviderType, calls: list):
        self.provider_type = provider_type
        self.calls = calls
        self.config = type("Cfg", (), {"timeout": 0})()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def get_recent_emails(self, count=20, only_unseen=True, mailbox="INBOX"):
        self.calls.append((self.provider_type.value, mailbox))
        if self.provider_type == ProviderType.GRAPH_API and mailbox == "JUNK":
            return [
                EmailMessage(
                    id="msg-1",
                    subject="OpenAI verification code",
                    sender="noreply@tm.openai.com",
                    recipients=["tester@outlook.com"],
                    body="Your verification code is 123456",
                    received_timestamp=int(time.time()),
                )
            ]
        return []


class DummyInjectProvider(DummyProvider):
    def inject_test_code_email(self, code, mailbox="INBOX"):
        self.calls.append(("inject", mailbox, code))
        return True


class DummyGraphInjectProvider(DummyProvider):
    def inject_test_code_email(self, code, mailbox="INBOX"):
        self.calls.append(("graph_inject", mailbox, code))
        return True


def _build_test_service():
    service = OutlookService(
        config={
            "email": "tester@outlook.com",
            "password": "pwd",
            "client_id": "cid",
            "refresh_token": "rtk",
        }
    )
    service.provider_priority = [
        ProviderType.IMAP_OLD,
        ProviderType.IMAP_NEW,
        ProviderType.GRAPH_API,
    ]
    return service


def test_outlook_round_robin_provider_and_mailbox(monkeypatch):
    service = _build_test_service()
    account = service.accounts[0]
    calls = []
    providers = {
        ProviderType.IMAP_OLD: DummyProvider(ProviderType.IMAP_OLD, calls),
        ProviderType.IMAP_NEW: DummyProvider(ProviderType.IMAP_NEW, calls),
        ProviderType.GRAPH_API: DummyProvider(ProviderType.GRAPH_API, calls),
    }

    monkeypatch.setattr(service, "_get_provider", lambda acc, p: providers[p])
    monkeypatch.setattr(service.health_checker, "is_available", lambda p: True)
    monkeypatch.setattr(service.health_checker, "record_success", lambda p: None)
    monkeypatch.setattr(service.health_checker, "record_failure", lambda p, e: None)

    emails = service._try_providers_for_emails(
        account=account,
        count=10,
        only_unseen=True,
        mailboxes=["INBOX", "JUNK"],
    )

    assert calls == [
        ("imap_old", "INBOX"),
        ("imap_old", "JUNK"),
        ("imap_new", "INBOX"),
        ("imap_new", "JUNK"),
        ("graph_api", "INBOX"),
        ("graph_api", "JUNK"),
    ]
    assert len(emails) == 1


def test_outlook_trigger_test_verification_email(monkeypatch):
    service = _build_test_service()
    calls = []
    providers = {
        ProviderType.IMAP_OLD: DummyInjectProvider(ProviderType.IMAP_OLD, calls),
        ProviderType.IMAP_NEW: DummyInjectProvider(ProviderType.IMAP_NEW, calls),
        ProviderType.GRAPH_API: DummyProvider(ProviderType.GRAPH_API, calls),
    }

    monkeypatch.setattr(service, "_get_provider", lambda acc, p: providers[p])
    code = service.trigger_test_verification_email("tester@outlook.com", code="445566")

    assert code == "445566"
    assert ("inject", "INBOX", "445566") in calls


def test_outlook_trigger_test_verification_email_via_graph(monkeypatch):
    service = _build_test_service()
    calls = []
    providers = {
        ProviderType.IMAP_OLD: DummyProvider(ProviderType.IMAP_OLD, calls),
        ProviderType.IMAP_NEW: DummyProvider(ProviderType.IMAP_NEW, calls),
        ProviderType.GRAPH_API: DummyGraphInjectProvider(ProviderType.GRAPH_API, calls),
    }

    monkeypatch.setattr(service, "_get_provider", lambda acc, p: providers[p])
    code = service.trigger_test_verification_email("tester@outlook.com", code="990011")

    assert code == "990011"
    assert ("graph_inject", "INBOX", "990011") in calls


def test_outlook_get_verification_code_found_in_junk_graph(monkeypatch):
    service = _build_test_service()
    calls = []
    providers = {
        ProviderType.IMAP_OLD: DummyProvider(ProviderType.IMAP_OLD, calls),
        ProviderType.IMAP_NEW: DummyProvider(ProviderType.IMAP_NEW, calls),
        ProviderType.GRAPH_API: DummyProvider(ProviderType.GRAPH_API, calls),
    }

    monkeypatch.setattr(service, "_get_provider", lambda acc, p: providers[p])
    monkeypatch.setattr(service.health_checker, "is_available", lambda p: True)
    monkeypatch.setattr(service.health_checker, "record_success", lambda p: None)
    monkeypatch.setattr(service.health_checker, "record_failure", lambda p, e: None)
    monkeypatch.setattr(
        service.email_parser,
        "find_verification_code_in_emails",
        lambda emails, **kwargs: "123456" if emails else None,
    )
    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 2, "poll_interval": 0},
    )

    code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=2,
        otp_sent_at=time.time(),
    )

    assert code == "123456"


def test_outlook_get_verification_code_uses_five_rounds_and_relaxes_after_three(monkeypatch):
    service = _build_test_service()
    observed_rounds = []

    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 30, "poll_interval": 0},
    )
    monkeypatch.setattr(
        service,
        "_try_providers_for_emails",
        lambda account, count, only_unseen, mailboxes, deadline_ts: observed_rounds.append(
            only_unseen
        )
        or [],
    )
    monkeypatch.setattr(
        service.email_parser,
        "find_verification_code_in_emails",
        lambda emails, **kwargs: None,
    )
    monkeypatch.setattr("src.services.outlook.service.time.sleep", lambda *_args, **_kwargs: None)

    code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=30,
        otp_sent_at=time.time(),
        max_poll_rounds=OUTLOOK_POLL_MAX_ROUNDS,
        prefer_unseen_rounds=OUTLOOK_POLL_PREFER_UNSEEN_ROUNDS,
    )

    assert code is None
    assert observed_rounds == [True, True, True, False, False]


def test_email_parser_preview_and_any_sender_fallback():
    parser = EmailParser()
    now_ts = int(time.time())
    emails = [
        EmailMessage(
            id="old-openai",
            subject="Your ChatGPT code is 111111",
            sender="noreply@tm.openai.com",
            body="",
            body_preview="",
            received_timestamp=now_ts - 200,
        ),
        EmailMessage(
            id="new-non-openai",
            subject="Security check",
            sender="security@service.example.com",
            body="",
            body_preview="Your verification code is 222222",
            received_timestamp=now_ts - 10,
        ),
    ]

    # Strict OpenAI mode should not return fallback sender.
    assert (
        parser.find_verification_code_in_emails(
            emails,
            target_email="tester@outlook.com",
            min_timestamp=now_ts - 60,
            used_codes=set(),
        )
        is None
    )

    # Fallback mode should extract from body_preview and non-OpenAI sender.
    assert (
        parser.find_verification_code_in_emails(
            emails,
            target_email="tester@outlook.com",
            min_timestamp=now_ts - 60,
            used_codes=set(),
            allow_any_sender=True,
        )
        == "222222"
    )


def test_outlook_inbox_route_returns_code(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "outlook_inbox_route.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        svc = EmailService(
            service_type="outlook",
            name="tester@outlook.com",
            config={"email": "tester@outlook.com", "password": "pwd"},
            enabled=True,
            priority=0,
        )
        session.add(svc)
        session.flush()
        service_id = svc.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    captured = {}

    class FakeOutlookService:
        def get_verification_code(self, email, email_id=None, timeout=None, pattern=None, otp_sent_at=None, **kwargs):
            captured["email"] = email
            captured["email_id"] = email_id
            captured["timeout"] = timeout
            captured["pattern"] = pattern
            captured["otp_sent_at"] = otp_sent_at
            captured.update(kwargs)
            return "654321"

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)
    monkeypatch.setattr(email_routes.EmailServiceFactory, "create", lambda *args, **kwargs: FakeOutlookService())

    result = asyncio.run(email_routes.get_outlook_latest_inbox_code(service_id))
    assert result.success is True
    assert result.code == "654321"
    assert captured["prefer_unseen_rounds"] == OUTLOOK_POLL_PREFER_UNSEEN_ROUNDS
    assert captured["max_poll_rounds"] == OUTLOOK_POLL_MAX_ROUNDS


def test_outlook_test_route_triggers_and_receives_code(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "outlook_test_route.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        svc = EmailService(
            service_type="outlook",
            name="tester@outlook.com",
            config={"email": "tester@outlook.com", "password": "pwd"},
            enabled=True,
            priority=0,
        )
        session.add(svc)
        session.flush()
        service_id = svc.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    captured = {}

    class FakeOutlookService:
        def trigger_test_verification_email(self, email, code=None):
            captured["trigger_email"] = email
            return "778899"

        def get_verification_code(self, email, email_id=None, timeout=None, pattern=None, otp_sent_at=None, **kwargs):
            captured["email"] = email
            captured["timeout"] = timeout
            captured["pattern"] = pattern
            captured["otp_sent_at"] = otp_sent_at
            captured.update(kwargs)
            return "123123"

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)
    monkeypatch.setattr(email_routes.EmailServiceFactory, "create", lambda *args, **kwargs: FakeOutlookService())

    result = asyncio.run(email_routes.test_outlook_verification_code(service_id))
    assert result.success is True
    assert result.code == "123123"
    assert captured["trigger_email"] == "tester@outlook.com"
    assert captured["allow_any_sender"] is True
    assert captured["lookback_seconds"] == 120
    assert captured["prefer_unseen_rounds"] == OUTLOOK_POLL_PREFER_UNSEEN_ROUNDS
    assert captured["fetch_count"] == 120
    assert captured["strict_unseen_only"] is False
    assert captured["max_poll_rounds"] == OUTLOOK_POLL_MAX_ROUNDS
    assert captured["timeout"] == 75


def test_account_outlook_inbox_route_uses_outlook_poll_profile(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "account_outlook_inbox_route.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        svc = EmailService(
            service_type="outlook",
            name="tester@outlook.com",
            config={"email": "tester@outlook.com", "password": "pwd"},
            enabled=True,
            priority=0,
        )
        session.add(svc)
        session.flush()

        from src.database.models import Account

        account = Account(
            email="tester@outlook.com",
            password="pwd",
            email_service="outlook",
            email_service_id=str(svc.id),
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

    captured = {}

    class FakeOutlookService:
        def get_verification_code(self, email, email_id=None, timeout=None, pattern=None, otp_sent_at=None, **kwargs):
            captured["email"] = email
            captured["email_id"] = email_id
            captured["timeout"] = timeout
            captured["pattern"] = pattern
            captured["otp_sent_at"] = otp_sent_at
            captured.update(kwargs)
            return "888999"

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr("src.services.EmailServiceFactory.create", lambda *args, **kwargs: FakeOutlookService())

    result = asyncio.run(accounts_routes.get_account_inbox_code(account_id))
    assert result["success"] is True
    assert result["code"] == "888999"
    assert captured["email"] == "tester@outlook.com"
    assert captured["lookback_seconds"] == 60
    assert captured["prefer_unseen_rounds"] == OUTLOOK_POLL_PREFER_UNSEEN_ROUNDS
    assert captured["strict_unseen_only"] is False
    assert captured["max_poll_rounds"] == OUTLOOK_POLL_MAX_ROUNDS
    assert captured["timeout"] == 60


def test_account_tempmail_inbox_route_keeps_legacy_timeout(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "account_tempmail_inbox_route.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        from src.database.models import Account

        account = Account(
            email="tester@example.com",
            password="pwd",
            email_service="tempmail",
            email_service_id="token-1",
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

    captured = {}

    class FakeTempMailService:
        def get_verification_code(self, email, email_id=None, timeout=None, pattern=None, otp_sent_at=None, **kwargs):
            captured["email"] = email
            captured["email_id"] = email_id
            captured["timeout"] = timeout
            captured["kwargs"] = kwargs
            return "112233"

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr("src.services.EmailServiceFactory.create", lambda *args, **kwargs: FakeTempMailService())

    result = asyncio.run(accounts_routes.get_account_inbox_code(account_id))
    assert result["success"] is True
    assert result["code"] == "112233"
    assert captured["timeout"] == 12
    assert captured["kwargs"] == {}

def test_outlook_batch_import_persists_password_and_oauth_fields(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "outlook_batch_import.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)

    request = email_routes.OutlookBatchImportRequest(
        data="tester@outlook.com----pwd123----client-abc----refresh-xyz",
        enabled=True,
        priority=7,
    )

    result = asyncio.run(email_routes.batch_import_outlook(request))
    assert result.success == 1
    assert result.failed == 0

    with manager.session_scope() as session:
        service = session.query(EmailService).filter(EmailService.name == "tester@outlook.com").first()
        assert service is not None
        assert service.priority == 7
        assert service.enabled is True
        assert service.config["email"] == "tester@outlook.com"
        assert service.config["password"] == "pwd123"
        assert service.config["client_id"] == "client-abc"
        assert service.config["refresh_token"] == "refresh-xyz"
