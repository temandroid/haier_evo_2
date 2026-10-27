"""Async REST client for the Haier Evo cloud: auth, token storage, requests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, urljoin

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import const as C
from .logger import _LOGGER

TOKEN_MARGIN = timedelta(seconds=60)
# Used when the auth answer has no parsable "expire": without it every
# request would refresh the token.
TOKEN_FALLBACK_TTL = timedelta(hours=1)
STORAGE_VERSION = 1


class HaierError(Exception):
    """Base error of the Haier Evo client."""


class HaierAuthError(HaierError):
    """Credentials are rejected: user has to re-enter them."""


class HaierTokenError(HaierError):
    """Refresh token is rejected: a full login is needed."""


class HaierConnectionError(HaierError):
    """Network error, timeout or 5xx: retry later."""


class HaierRateLimitError(HaierError):
    """HTTP 429 or our own login throttling."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Too many requests, retry after {retry_after:.0f} s")
        self.retry_after = retry_after


class HaierApiError(HaierError):
    """Unexpected 4xx answer of the API."""


def parse_expire(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # The backend answers in Moscow time; keep the old assumption (UTC+3).
        parsed = parsed.replace(tzinfo=dt_util.get_time_zone("Europe/Moscow"))
    return parsed


def _retry_after(resp: ClientResponse, default: float) -> float:
    try:
        return max(float(resp.headers.get("Retry-After", default)), 1.0)
    except (TypeError, ValueError):
        return default


class HaierClient:
    """Talks to evo.haieronline.ru. Never logs passwords or tokens."""

    def __init__(
        self,
        session: ClientSession,
        email: str,
        password: str,
        region: str,
        store: Store | None = None,
        device_id: str | None = None,
    ) -> None:
        self._session = session
        self.email = email
        self._password = password
        self.region = region
        self._store = store
        self.device_id = device_id or "00000000-0000-4000-8000-000000000000"
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._token_expire: datetime | None = None
        self._refresh_token: str | None = None
        self._refresh_expire: datetime | None = None
        self._login_blocked_until: float = 0.0
        self._login_backoff: float = C.LOGIN_LIMIT_429
        # Set once the cloud rejects the password: logging in again with the same
        # one until reauth only risks a lockout of the account.
        self._auth_failed = False

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": "evo-mobile",
            "Platform": "android",
            "Accept": "*/*",
            "Device-Id": self.device_id,
        }
        if token:
            headers["X-Auth-Token"] = token
        return headers

    def _url(self, path: str) -> str:
        return urljoin(C.API_PATH, path.format(region=self.region))

    def ws_url(self, token: str) -> str:
        # Same as upstream: the token is the last path segment, sent as is.
        return urljoin(C.API_WS_PATH, token)

    # --- token storage (.storage/haier_evo.<entry_id>) ---

    async def async_load_tokens(self) -> None:
        if self._store is None:
            return
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        self._token = data.get("token")
        self._token_expire = parse_expire(data.get("token_expire"))
        self._refresh_token = data.get("refresh_token")
        self._refresh_expire = parse_expire(data.get("refresh_expire"))
        self.device_id = data.get("device_id") or self.device_id

    async def _async_save_tokens(self) -> None:
        if self._store is None:
            return
        await self._store.async_save({
            "token": self._token,
            "token_expire": self._token_expire.isoformat() if self._token_expire else None,
            "refresh_token": self._refresh_token,
            "refresh_expire": self._refresh_expire.isoformat() if self._refresh_expire else None,
            "device_id": self.device_id,
        })

    def invalidate_access_token(self) -> None:
        """The server rejected the access token: refresh it on next use."""
        self._token_expire = None

    def export_tokens(self) -> dict:
        """Tokens of a fresh login, handed from the config flow to the entry."""
        return {
            "token": self._token,
            "token_expire": self._token_expire.isoformat() if self._token_expire else None,
            "refresh_token": self._refresh_token,
            "refresh_expire": self._refresh_expire.isoformat() if self._refresh_expire else None,
        }

    async def async_import_tokens(self, data: dict) -> None:
        self._token = data.get("token")
        self._token_expire = parse_expire(data.get("token_expire"))
        self._refresh_token = data.get("refresh_token")
        self._refresh_expire = parse_expire(data.get("refresh_expire"))
        await self._async_save_tokens()

    def _set_tokens(self, token: dict) -> None:
        self._token = token["accessToken"]
        self._token_expire = parse_expire(token.get("expire"))
        if self._token_expire is None:
            _LOGGER.warning("Haier Evo sent no token expiry, assuming %s", TOKEN_FALLBACK_TTL)
            self._token_expire = dt_util.utcnow() + TOKEN_FALLBACK_TTL
        self._refresh_token = token.get("refreshToken") or None
        self._refresh_expire = parse_expire(token.get("refreshExpire"))

    def _clear_tokens(self) -> None:
        self._token = None
        self._token_expire = None
        self._refresh_token = None
        self._refresh_expire = None

    # --- auth ---

    async def _async_post_auth(self, path: str, data: dict[str, str]) -> dict:
        url = self._url(path)
        try:
            async with self._session.post(
                url,
                data=data,
                headers=self._headers(),
                timeout=ClientTimeout(total=C.API_TIMEOUT),
            ) as resp:
                status = resp.status
                if status == 429:
                    raise HaierRateLimitError(_retry_after(resp, self._login_backoff))
                if status >= 500:
                    raise HaierConnectionError(f"Auth endpoint answered HTTP {status}")
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
        except (ClientError, TimeoutError) as err:
            raise HaierConnectionError(f"Auth request failed: {type(err).__name__}") from None

        if not isinstance(body, dict):
            if status in (401, 403):
                raise HaierAuthError(f"HTTP {status}")
            if status >= 400:
                raise HaierApiError(f"Auth endpoint answered HTTP {status}")
            raise HaierConnectionError(f"Unexpected auth answer, HTTP {status}")
        error = body.get("error")
        if error:
            validation = (error.get("validation") or {}) if isinstance(error, dict) else {}
            if validation.get("refreshToken"):
                raise HaierTokenError("Refresh token rejected")
            if validation.get("email") or validation.get("password"):
                raise HaierAuthError("Email or password rejected")
            if status in (401, 403):
                raise HaierAuthError(f"HTTP {status}")
            message = error.get("message") if isinstance(error, dict) else error
            if status >= 400:
                raise HaierApiError(f"Auth error from server: {message}")
            raise HaierConnectionError(f"Auth error from server: {message}")
        if status in (401, 403):
            raise HaierAuthError(f"HTTP {status}")
        if status >= 400:
            raise HaierApiError(f"Auth endpoint answered HTTP {status}")
        token = (body.get("data") or {}).get("token") or {}
        if not isinstance(token.get("accessToken"), str) or not token["accessToken"]:
            raise HaierConnectionError("No access token in auth answer")
        return token

    async def async_login(self) -> None:
        if self._auth_failed:
            raise HaierAuthError("Credentials were rejected, waiting for re-authentication")
        loop_time = asyncio.get_running_loop().time()
        if loop_time < self._login_blocked_until:
            raise HaierRateLimitError(self._login_blocked_until - loop_time)
        _LOGGER.debug("Logging in to Haier Evo (%s)", self.region)
        try:
            token = await self._async_post_auth(
                C.API_LOGIN, {"email": self.email, "password": self._password}
            )
        except HaierRateLimitError as err:
            # Upstream #87: 429 is not a wrong password. Back off, double every time.
            self._login_blocked_until = loop_time + err.retry_after
            self._login_backoff = min(self._login_backoff * 2, C.LOGIN_LIMIT_MAX)
            raise
        except HaierAuthError:
            self._auth_failed = True
            raise
        except HaierConnectionError:
            self._login_blocked_until = loop_time + C.LOGIN_LIMIT_PERIOD
            raise
        except HaierApiError as err:
            # An unknown 4xx on login is not a verdict on the password: retry later.
            self._login_blocked_until = loop_time + C.LOGIN_LIMIT_PERIOD
            raise HaierConnectionError(str(err)) from None
        self._login_backoff = C.LOGIN_LIMIT_429
        self._login_blocked_until = loop_time + C.LOGIN_LIMIT_PERIOD
        self._set_tokens(token)
        await self._async_save_tokens()
        _LOGGER.debug("Login ok, token valid till %s", self._token_expire)

    async def _async_refresh(self) -> None:
        _LOGGER.debug("Refreshing Haier Evo token")
        token = await self._async_post_auth(
            C.API_TOKEN_REFRESH, {"refreshToken": self._refresh_token or ""}
        )
        self._set_tokens(token)
        await self._async_save_tokens()

    async def async_get_token(self) -> str:
        async with self._lock:
            now = dt_util.utcnow()
            if self._token and self._token_expire and self._token_expire - TOKEN_MARGIN > now:
                return self._token
            if self._refresh_token and (
                self._refresh_expire is None or self._refresh_expire - TOKEN_MARGIN > now
            ):
                try:
                    await self._async_refresh()
                except (HaierTokenError, HaierAuthError, HaierApiError) as err:
                    # Any refusal of the refresh token (not a network error or
                    # a 429) means the password has to be used again.
                    _LOGGER.info("Refresh token rejected (%s), logging in again", err)
                    self._clear_tokens()
                    await self._async_save_tokens()
                else:
                    return self._token  # type: ignore[return-value]
            await self.async_login()
            return self._token  # type: ignore[return-value]

    # --- data ---

    async def async_get(self, url: str) -> Any:
        for attempt in range(2):
            token = await self.async_get_token()
            try:
                async with self._session.get(
                    url,
                    headers={**self._headers(token), "Content-Type": "application/json"},
                    timeout=ClientTimeout(total=C.API_TIMEOUT),
                ) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        self.invalidate_access_token()
                        continue
                    if resp.status == 401:
                        # A just refreshed token is rejected too. 403 stays an
                        # API error: stale device-list entries answer it.
                        raise HaierAuthError("Access token rejected twice")
                    if resp.status == 429:
                        raise HaierRateLimitError(_retry_after(resp, C.LOGIN_LIMIT_429))
                    if resp.status >= 500:
                        raise HaierConnectionError(f"HTTP {resp.status}")
                    if resp.status >= 400:
                        raise HaierApiError(f"HTTP {resp.status}")
                    return await resp.json(content_type=None)
            except (ClientError, TimeoutError) as err:
                raise HaierConnectionError(f"Request failed: {type(err).__name__}") from None
            except ValueError:
                raise HaierConnectionError("Answer is not JSON") from None
        raise HaierApiError("Access to the resource is denied")

    async def async_get_devices_page(self) -> dict:
        data = await self.async_get(self._url(C.API_DEVICES))
        data = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(data, dict):
            raise HaierConnectionError("Devices page has no data")
        return data

    async def async_get_device_status(self, mac: str) -> dict:
        data = await self.async_get(C.API_STATUS.format(mac=quote(mac, safe=":")))
        if not isinstance(data, dict):
            raise HaierConnectionError("Device status is not an object")
        return data
