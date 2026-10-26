"""HTTP adapter for the legacy Urja Meter Ops portal.

This module knows only about the portal: cookies, the signed bulk export, the
JSON endpoints, and the devalue payload embedded in server-rendered meter pages.
It returns raw parsed payloads; turning them into the public contract is the
job of :mod:`app.normalize`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import json5

logger = logging.getLogger("flock.portal")

SESSION_COOKIE = "__Secure-better-auth.session_token"
EXPORT_QUERY = "page=1"
SIGNING_KEY_TTL_SECONDS = 600


class PortalError(Exception):
    """Base class for adapter failures."""


class PortalAuthError(PortalError):
    """The portal rejected our credentials or the session cannot be established."""


class PortalUnavailable(PortalError):
    """The portal could not be reached or kept returning server errors."""


class PortalNotFound(PortalError):
    """The portal says the requested meter does not exist."""


class PortalProtocolError(PortalError):
    """The portal returned something we could not understand."""


@dataclass(frozen=True)
class SessionStatus:
    authenticated: bool
    expires_at: datetime | None


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        value = payload.get("error")
        if isinstance(value, str):
            return value
    return None


def _balanced_slice(text: str, start: int, open_char: str, close_char: str) -> str:
    """Return a balanced slice starting at ``start``, ignoring brackets in strings."""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise PortalProtocolError("Unbalanced payload while parsing the server-rendered page")


def parse_ssr_meter_payload(html: str) -> dict[str, Any]:
    """Extract ``{meterId, detail, hierarchy}`` from a SvelteKit meter page.

    SvelteKit embeds a devalue array in an inline script. It is JavaScript, not
    JSON (unquoted keys), so json5 is used after isolating the balanced array.
    """
    marker = html.find("node_ids:")
    if marker < 0:
        raise PortalProtocolError("Meter page did not contain a SvelteKit payload")
    data_marker = html.find("data:", marker)
    array_start = html.find("[", data_marker)
    if data_marker < 0 or array_start < 0:
        raise PortalProtocolError("Meter page payload is missing its data array")

    array_text = _balanced_slice(html, array_start, "[", "]")
    try:
        nodes = json5.loads(array_text)
    except ValueError as exc:  # json5 raises ValueError subclasses
        raise PortalProtocolError(f"Could not decode the meter page payload: {exc}") from exc

    for node in nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data")
        if isinstance(data, dict) and data.get("meterId"):
            detail = data.get("detail")
            hierarchy = data.get("hierarchy")
            if not isinstance(detail, dict) or not isinstance(hierarchy, dict):
                raise PortalProtocolError("Meter page payload is missing detail or hierarchy")
            return {"meter_id": data["meterId"], "detail": detail, "hierarchy": hierarchy}
    raise PortalProtocolError("Meter payload was not present in the server-rendered page")


class UrjaPortalClient:
    """Cookie-session client with automatic re-authentication and signed export."""

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        *,
        timeout_seconds: float = 20.0,
        connect_timeout_seconds: float = 5.0,
        max_retries: int = 2,
        refresh_skew_seconds: int = 90,
        user_agent: str = "flock-energy-api/1.0 (read-only adapter)",
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._origin = self._origin_of(self._base_url)
        self._email = email
        self._password = password
        self._max_retries = max_retries
        self._refresh_skew = timedelta(seconds=refresh_skew_seconds)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()

        self._session_expires_at: datetime | None = None
        self._signing_secret: str | None = None
        self._signing_secret_at = 0.0

        if http_client is None:
            self._http = httpx.Client(
                timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
                follow_redirects=False,
                headers={"User-Agent": user_agent, "Accept": "application/json"},
            )
            self._owns_http = True
        else:
            self._http = http_client
            self._owns_http = False

    @staticmethod
    def _origin_of(url: str) -> str:
        match = re.match(r"^(https?://[^/]+)", url)
        return match.group(1) if match else url

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------
    def login(self) -> SessionStatus:
        """Authenticate and store the session cookie in the HTTP client."""
        if not self._email or not self._password:
            raise PortalAuthError(
                "Portal credentials are not configured. Set PORTAL_EMAIL and PORTAL_PASSWORD (see .env.example)."
            )

        headers = {
            "Origin": self._origin,
            "Referer": f"{self._origin}/login",
            "Accept": "application/json",
        }
        payload = {"email": self._email, "password": self._password}
        try:
            response = self._http.post(
                f"{self._base_url}/login",
                data=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise PortalUnavailable(f"Could not reach the portal login endpoint: {exc}") from exc

        if response.status_code == 403:
            raise PortalAuthError(
                "The portal rejected the login request (CSRF/Origin check). "
                "Check PORTAL_BASE_URL and that the portal accepts the Origin header."
            )
        if response.status_code >= 500:
            raise PortalUnavailable(f"Portal login failed with HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise PortalProtocolError("Portal login returned a non-JSON response") from exc

        if response.status_code != 200 or body.get("type") != "redirect":
            message = self._login_failure_message(body)
            raise PortalAuthError(message)

        expires_at = self._cookie_expiry(response)
        self._session_expires_at = expires_at
        logger.info("Authenticated with the portal; session expires at %s", expires_at.isoformat())
        return SessionStatus(authenticated=True, expires_at=expires_at)

    @staticmethod
    def _login_failure_message(body: Any) -> str:
        fallback = "The portal rejected the supplied credentials."
        if not isinstance(body, dict):
            return fallback
        raw = body.get("data")
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, list) and decoded and isinstance(decoded[-1], str):
                    return decoded[-1]
            except ValueError:
                pass
        if isinstance(raw, list) and raw and isinstance(raw[-1], str):
            return raw[-1]
        return fallback

    def _cookie_expiry(self, response: httpx.Response) -> datetime:
        now = self._utcnow()
        for header in response.headers.get_list("set-cookie"):
            if SESSION_COOKIE not in header:
                continue
            match = re.search(r"Max-Age=(\d+)", header, re.IGNORECASE)
            if match:
                return now + timedelta(seconds=int(match.group(1)))
        return now + timedelta(hours=1)

    def _utcnow(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    def _session_cookie_present(self) -> bool:
        try:
            return self._http.cookies.get(SESSION_COOKIE) is not None
        except Exception:  # cookie jar conflict or unusual cookie implementation
            return False

    def _has_valid_session(self) -> bool:
        if not self._session_cookie_present():
            return False
        if self._session_expires_at is None:
            return True
        return self._utcnow() < self._session_expires_at - self._refresh_skew

    def ensure_session(self) -> SessionStatus:
        with self._lock:
            if not self._has_valid_session():
                return self.login()
            return self.session_status()

    def session_status(self) -> SessionStatus:
        """Report the local view of the session without contacting the portal."""
        authenticated = self._session_cookie_present()
        if authenticated and self._session_expires_at is not None:
            authenticated = self._utcnow() < self._session_expires_at
        return SessionStatus(authenticated=authenticated, expires_at=self._session_expires_at)

    def _force_login(self) -> SessionStatus:
        with self._lock:
            self._session_expires_at = None
            return self.login()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _backoff(self, attempt: int) -> None:
        delay = min(4.0, 0.35 * (2**attempt)) + random.uniform(0, 0.15)
        self._sleep(delay)

    def _execute(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a request, retrying transport errors and 5xx responses."""
        attempts = self._max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._http.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("Portal request %s %s failed (%s); attempt %s/%s", method, path, exc, attempt + 1, attempts)
                if attempt + 1 < attempts:
                    self._backoff(attempt)
                    continue
                raise PortalUnavailable(f"Portal request failed: {exc}") from exc

            if response.status_code >= 500 and attempt + 1 < attempts:
                logger.warning(
                    "Portal request %s %s returned HTTP %s; retrying",
                    method,
                    path,
                    response.status_code,
                )
                self._backoff(attempt)
                continue
            if response.status_code == 429 and attempt + 1 < attempts:
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.0
                logger.warning("Portal rate-limited %s %s; sleeping %.1fs", method, path, delay)
                self._sleep(delay)
                continue
            return response

        raise PortalUnavailable(f"Portal request failed after {attempts} attempts: {last_error}")

    def _json(self, response: httpx.Response, context: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise PortalProtocolError(f"{context} returned a non-JSON response") from exc

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        reauth_on_redirect: bool = False,
    ) -> httpx.Response:
        """Authenticated request with one re-login attempt on session expiry."""
        self.ensure_session()
        response = self._execute(method, path, params=params, headers=headers)

        unauthorised = response.status_code == 401
        login_redirect = (
            reauth_on_redirect
            and response.status_code in (301, 302, 303, 307, 308)
            and "/login" in response.headers.get("location", "")
        )
        if unauthorised or login_redirect:
            logger.info("Portal session expired or rejected; re-authenticating")
            self._force_login()
            response = self._execute(method, path, params=params, headers=headers)

        if response.status_code == 404:
            raise PortalNotFound("Meter not found")
        if response.status_code == 401:
            raise PortalAuthError("The portal still rejected the session after re-authentication")
        if response.status_code >= 400:
            code = _error_code(response) or "http_error"
            raise PortalError(f"Portal returned HTTP {response.status_code} ({code})")
        return response

    # ------------------------------------------------------------------
    # Endpoint wrappers
    # ------------------------------------------------------------------
    def search_meters(self, query: str = "", page: int = 1) -> dict[str, Any]:
        response = self._request("/portal/meters/search", params={"q": query, "page": page})
        payload = self._json(response, "Meter search")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PortalProtocolError("Meter search returned an unexpected shape")
        return payload

    def get_transformer_page(self, page: int = 1) -> dict[str, Any]:
        response = self._request("/portal/dts", params={"page": page})
        payload = self._json(response, "Transformer list")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PortalProtocolError("Transformer list returned an unexpected shape")
        return payload

    def get_all_transformers(self, max_pages: int = 100) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(1, max_pages + 1):
            payload = self.get_transformer_page(page)
            records.extend(item for item in payload["data"] if isinstance(item, dict))
            total = int(payload.get("total") or len(records))
            if not payload["data"] or len(records) >= total:
                break
        return records

    def get_meter_geo(self, meter_id: str) -> dict[str, Any]:
        response = self._request(f"/portal/meters/{meter_id}/geo")
        payload = self._json(response, "Meter location")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise PortalProtocolError("Meter location returned an unexpected shape")
        return data

    def get_meter_energy(
        self,
        meter_id: str,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if from_date is not None:
            params["from"] = from_date.isoformat()
        if to_date is not None:
            params["to"] = to_date.isoformat()
        response = self._request(f"/portal/meters/{meter_id}/energy", params=params or None)
        payload = self._json(response, "Meter consumption")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise PortalProtocolError("Meter consumption returned an unexpected shape")
        return [item for item in data if isinstance(item, dict)]

    def get_meter_detail(self, meter_id: str) -> dict[str, Any]:
        """Fetch and parse the SSR meter page. Raises PortalNotFound/ProtocolError."""
        response = self._request(
            f"/meters/{meter_id}",
            headers={"Accept": "text/html"},
            reauth_on_redirect=True,
        )
        if "text/html" not in response.headers.get("content-type", ""):
            raise PortalProtocolError("Meter detail page was not HTML")
        return parse_ssr_meter_payload(response.text)

    # ------------------------------------------------------------------
    # Signed bulk export
    # ------------------------------------------------------------------
    def get_signing_secret(self, *, force: bool = False) -> str:
        with self._lock:
            if (
                not force
                and self._signing_secret is not None
                and self._clock() - self._signing_secret_at < SIGNING_KEY_TTL_SECONDS
            ):
                return self._signing_secret
            response = self._request("/portal/keys")
            payload = self._json(response, "Signing key")
            secret = payload.get("data", {}).get("signingSecret") if isinstance(payload, dict) else None
            if not isinstance(secret, str) or not secret:
                raise PortalProtocolError("Signing key endpoint returned an unexpected shape")
            self._signing_secret = secret
            self._signing_secret_at = self._clock()
            return secret

    def _signed_export_headers(self, secret: str) -> dict[str, str]:
        timestamp = str(int(self._clock()))
        payload = "\n".join(["GET", "/portal/export", EXPORT_QUERY, timestamp])
        signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"x-timestamp": timestamp, "x-signature": signature}

    def get_export(self) -> dict[str, Any]:
        """Fetch the full meter export, signing the request as the portal's UI does."""
        self.ensure_session()
        last_error: str | None = None

        for attempt in range(2):
            secret = self.get_signing_secret(force=attempt > 0)
            headers = self._signed_export_headers(secret)
            response = self._execute("GET", f"/portal/export?{EXPORT_QUERY}", headers=headers)
            if response.status_code == 200:
                payload = self._json(response, "Bulk export")
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise PortalProtocolError("Bulk export returned an unexpected shape")
                return payload

            code = _error_code(response)
            last_error = f"HTTP {response.status_code} ({code or 'unknown'})"
            if response.status_code == 401 and code == "unauthorized":
                logger.info("Bulk export session expired; re-authenticating")
                self._force_login()
                continue
            if response.status_code == 401 and code == "signature_invalid":
                logger.warning("Bulk export signature rejected; refreshing signing key")
                self._signing_secret = None
                continue
            break

        raise PortalError(f"Bulk export failed: {last_error}")

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
