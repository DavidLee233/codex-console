from types import SimpleNamespace

import pytest

from src.services.base import EmailServiceError
from src.services.freemail import FreemailService


class FakeResponse:
    def __init__(self, *, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise ValueError("no json payload")
        return self._json_data


def test_freemail_headers_include_cf_access_service_token():
    service = FreemailService(
        config={
            "base_url": "https://freemail.example.com",
            "admin_token": "jwt-token",
            "cf_access_client_id": "client-id",
            "cf_access_client_secret": "client-secret",
        }
    )

    headers = service._get_headers()

    assert headers["Authorization"] == "Bearer jwt-token"
    assert headers["CF-Access-Client-Id"] == "client-id"
    assert headers["CF-Access-Client-Secret"] == "client-secret"


def test_freemail_create_email_uses_cf_access_headers(monkeypatch):
    service = FreemailService(
        config={
            "base_url": "https://freemail.example.com",
            "admin_token": "jwt-token",
            "cf_access_client_id": "client-id",
            "cf_access_client_secret": "client-secret",
            "domain": "kd7.icu",
        }
    )

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/api/domains"):
            return FakeResponse(json_data=["foo.test", "kd7.icu"])
        if "/api/generate" in url:
            return FakeResponse(json_data={"email": "abc123@kd7.icu", "expires": 1700000000000})
        raise AssertionError(f"unexpected url: {url}")

    service.http_client = SimpleNamespace(request=fake_request)

    result = service.create_email()

    assert result["email"] == "abc123@kd7.icu"
    assert len(calls) == 2
    _, _, domain_kwargs = calls[0]
    _, _, generate_kwargs = calls[1]
    assert domain_kwargs["headers"]["CF-Access-Client-Id"] == "client-id"
    assert domain_kwargs["headers"]["CF-Access-Client-Secret"] == "client-secret"
    assert generate_kwargs["headers"]["CF-Access-Client-Id"] == "client-id"
    assert generate_kwargs["params"]["domainIndex"] == 1


def test_freemail_get_verification_code_reads_recent_mail(monkeypatch):
    service = FreemailService(
        config={
            "base_url": "https://freemail.example.com",
            "admin_token": "jwt-token",
            "cf_access_client_id": "client-id",
            "cf_access_client_secret": "client-secret",
        }
    )

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/api/emails"):
            return FakeResponse(
                json_data=[
                    {
                        "id": "mail-1",
                        "sender": "noreply@tm.openai.com",
                        "subject": "Your verification code",
                        "preview": "Code: 654321",
                    }
                ]
            )
        raise AssertionError(f"unexpected url: {url}")

    service.http_client = SimpleNamespace(request=fake_request)

    code = service.get_verification_code("abc123@kd7.icu", timeout=1)

    assert code == "654321"
    assert calls[0][2]["headers"]["CF-Access-Client-Id"] == "client-id"


def test_freemail_cloudflare_access_html_raises_helpful_error():
    service = FreemailService(
        config={
            "base_url": "https://freemail.example.com",
            "admin_token": "jwt-token",
        }
    )

    service.http_client = SimpleNamespace(
        request=lambda *args, **kwargs: FakeResponse(
            text="<!DOCTYPE html><html><head><title>Sign in ・ Cloudflare Access</title></head></html>",
            headers={"Content-Type": "text/html; charset=UTF-8"},
        )
    )

    with pytest.raises(EmailServiceError) as exc_info:
        service._make_request("GET", "/api/domains")

    assert "Cloudflare Access" in str(exc_info.value)


def test_freemail_falls_back_to_requests_when_primary_client_fails(monkeypatch):
    service = FreemailService(
        config={
            "base_url": "https://freemail.example.com",
            "admin_token": "jwt-token",
            "cf_access_client_id": "client-id",
            "cf_access_client_secret": "client-secret",
        }
    )

    service.http_client = SimpleNamespace(
        request=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("TLS connect error"))
    )

    fallback_calls = []

    def fake_stdlib_request(method, url, **kwargs):
        fallback_calls.append((method, url, kwargs))
        return FakeResponse(json_data=["kd7.icu"])

    monkeypatch.setattr(service, "_request_via_stdlib", fake_stdlib_request)

    result = service._make_request("GET", "/api/domains")

    assert result == ["kd7.icu"]
    assert len(fallback_calls) == 1
    _, _, request_kwargs = fallback_calls[0]
    assert request_kwargs["headers"]["CF-Access-Client-Id"] == "client-id"
    assert request_kwargs["headers"]["CF-Access-Client-Secret"] == "client-secret"
