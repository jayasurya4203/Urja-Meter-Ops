"""Adapter tests using httpx.MockTransport: no network, real HTTP semantics."""

from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest

from app.client import (
    PortalAuthError,
    PortalNotFound,
    PortalProtocolError,
    PortalUnavailable,
    UrjaPortalClient,
    parse_ssr_meter_payload,
)

FIXED_NOW = 1_700_000_000.0
LOGIN_HEADERS = {
    "set-cookie": "__Secure-better-auth.session_token=session-token; Path=/; Max-Age=3600; HttpOnly; Secure; SameSite=Lax"
}


def login_ok() -> httpx.Response:
    return httpx.Response(200, json={"type": "redirect"}, headers=LOGIN_HEADERS)


def make_client(handler, **overrides) -> UrjaPortalClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    kwargs = {
        "http_client": http,
        "clock": lambda: FIXED_NOW,
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    return UrjaPortalClient("https://portal.test", "operator@example.test", "secret", **kwargs)


def test_login_sends_origin_and_stores_the_session_cookie():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return login_ok()

    client = make_client(handler)
    status = client.login()

    assert status.authenticated is True
    assert status.expires_at is not None
    assert int(status.expires_at.timestamp()) == int(FIXED_NOW) + 3600
    assert requests[0].headers["origin"] == "https://portal.test"
    assert requests[0].headers["referer"] == "https://portal.test/login"
    assert client.session_status().authenticated is True


def test_login_without_credentials_fails_fast():
    client = UrjaPortalClient("https://portal.test", "", "", http_client=httpx.Client(transport=httpx.MockTransport(lambda r: login_ok())))

    with pytest.raises(PortalAuthError, match="not configured"):
        client.login()


def test_login_csrf_rejection_is_reported_as_auth_error():
    client = make_client(lambda request: httpx.Response(403, text="Cross-site POST form submissions are forbidden"))

    with pytest.raises(PortalAuthError, match="CSRF/Origin"):
        client.login()


def test_login_failure_decodes_the_portal_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"type": "failure", "data": '["users","Invalid email or password"]'},
        )

    client = make_client(handler)

    with pytest.raises(PortalAuthError, match="Invalid email or password"):
        client.login()


def test_login_server_error_is_unavailable():
    client = make_client(lambda request: httpx.Response(503, text="down"))

    with pytest.raises(PortalUnavailable, match="503"):
        client.login()


def test_login_non_json_response_is_a_protocol_error():
    client = make_client(lambda request: httpx.Response(200, text="<html>login</html>"))

    with pytest.raises(PortalProtocolError, match="non-JSON"):
        client.login()


def test_401_triggers_one_reauthentication_and_retries():
    calls = {"login": 0, "search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            calls["login"] += 1
            return login_ok()
        if request.url.path == "/portal/meters/search":
            calls["search"] += 1
            if calls["search"] == 1:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"data": [{"meterId": "J100001"}], "total": 1})
        raise AssertionError(f"unexpected request {request.url}")

    client = make_client(handler)
    payload = client.search_meters("J100")

    assert calls == {"login": 2, "search": 2}
    assert payload["total"] == 1


def test_persistent_401_after_reauthentication_is_an_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return login_ok()
        return httpx.Response(401, json={"error": "unauthorized"})

    client = make_client(handler)

    with pytest.raises(PortalAuthError, match="after re-authentication"):
        client.search_meters("J100")


def test_5xx_is_retried_with_backoff():
    calls = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return login_ok()
        calls["search"] += 1
        if calls["search"] < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"data": [], "total": 0})

    client = make_client(handler, max_retries=2)
    assert client.search_meters()["total"] == 0
    assert calls["search"] == 3


def test_transport_errors_become_unavailable_after_retries():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return login_ok()
        calls["count"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler, max_retries=1)

    with pytest.raises(PortalUnavailable, match="connection refused"):
        client.search_meters()
    assert calls["count"] == 2


def test_404_is_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return login_ok()
        return httpx.Response(404, json={"error": "not_found"})

    client = make_client(handler)

    with pytest.raises(PortalNotFound):
        client.get_meter_energy("J999999")


def test_export_is_signed_with_the_documented_hmac_construction():
    secret = "top-secret-signing-key"
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return login_ok()
        if request.url.path == "/portal/keys":
            return httpx.Response(200, json={"data": {"signingSecret": secret}})
        if request.url.path == "/portal/export":
            seen.update(request.headers)
            return httpx.Response(200, json={"data": [], "total": 0})
        raise AssertionError(f"unexpected request {request.url}")

    client = make_client(handler)
    payload = client.get_export()

    assert payload["total"] == 0
    assert seen["x-timestamp"] == str(int(FIXED_NOW))
    expected = hmac.new(
        secret.encode(),
        f"GET\n/portal/export\npage=1\n{int(FIXED_NOW)}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert seen["x-signature"] == expected


def test_export_refreshes_the_signing_key_once_on_signature_invalid():
    keys = {"count": 0, "export": 0}
    signatures: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return login_ok()
        if request.url.path == "/portal/keys":
            keys["count"] += 1
            return httpx.Response(200, json={"data": {"signingSecret": f"secret-{keys['count']}"}})
        if request.url.path == "/portal/export":
            keys["export"] += 1
            signatures.append(request.headers["x-signature"])
            if keys["export"] == 1:
                return httpx.Response(401, json={"error": "signature_invalid"})
            expected = hmac.new(
                b"secret-2",
                f"GET\n/portal/export\npage=1\n{int(FIXED_NOW)}".encode(),
                hashlib.sha256,
            ).hexdigest()
            assert request.headers["x-signature"] == expected
            return httpx.Response(200, json={"data": [], "total": 0})
        raise AssertionError(f"unexpected request {request.url}")

    client = make_client(handler)
    client.get_export()

    assert keys == {"count": 2, "export": 2}
    assert signatures[0] != signatures[1]


def test_export_reauths_when_the_session_is_rejected():
    calls = {"login": 0, "export": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            calls["login"] += 1
            return login_ok()
        if request.url.path == "/portal/keys":
            return httpx.Response(200, json={"data": {"signingSecret": "secret"}})
        if request.url.path == "/portal/export":
            calls["export"] += 1
            if calls["export"] == 1:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"data": [], "total": 0})
        raise AssertionError(f"unexpected request {request.url}")

    client = make_client(handler)
    client.get_export()

    assert calls == {"login": 2, "export": 2}


def test_transformer_registry_is_paginated():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return login_ok()
        if request.url.path == "/portal/dts":
            page = request.url.params.get("page")
            if page == "1":
                return httpx.Response(200, json={"data": [{"code": "DT-001"}], "total": 2})
            return httpx.Response(200, json={"data": [{"code": "DT-002"}], "total": 2})
        raise AssertionError(f"unexpected request {request.url}")

    client = make_client(handler)

    assert [row["code"] for row in client.get_all_transformers()] == ["DT-001", "DT-002"]


def test_energy_forward_dates_and_return_rows():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return login_ok()
        if request.url.path == "/portal/meters/J100001/energy":
            seen.update(request.url.params)
            return httpx.Response(200, json={"data": [{"kwh": "1", "kvah": "2"}]})
        raise AssertionError(f"unexpected request {request.url}")

    client = make_client(handler)
    from datetime import date

    rows = client.get_meter_energy("J100001", date(2026, 6, 1), date(2026, 6, 2))

    assert seen == {"from": "2026-06-01", "to": "2026-06-02"}
    assert rows == [{"kwh": "1", "kvah": "2"}]


def test_ssr_payload_is_extracted_from_javascript_not_json():
    html = """
    <script>
      self.__sveltekit_abc = {
        node_ids: [0],
        data: [{
          data: {
            meterId: "J100001",
            detail: {data: []},
            hierarchy: {Zone: "Jaipur Zone 2", DT: "DT [7] ring"}
          }
        }]
      };
    </script>
    """

    payload = parse_ssr_meter_payload(html)

    assert payload["meter_id"] == "J100001"
    assert payload["hierarchy"]["DT"] == "DT [7] ring"


def test_ssr_payload_without_a_data_array_is_a_protocol_error():
    with pytest.raises(PortalProtocolError, match="SvelteKit payload"):
        parse_ssr_meter_payload("<html>plain page</html>")


def test_detail_redirect_to_login_triggers_reauthentication():
    calls = {"login": 0, "detail": 0}
    html = (
        '<script>self.__sveltekit_abc = {node_ids: [0], data: [{data: {meterId: "J100001",'
        ' detail: {data: []}, hierarchy: {Zone: "Z"}}}]};</script>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            calls["login"] += 1
            return login_ok()
        if request.url.path == "/meters/J100001":
            calls["detail"] += 1
            if calls["detail"] == 1:
                return httpx.Response(302, headers={"location": "/login"})
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        raise AssertionError(f"unexpected request {request.url}")

    client = make_client(handler)
    payload = client.get_meter_detail("J100001")

    assert calls == {"login": 2, "detail": 2}
    assert payload["meter_id"] == "J100001"
