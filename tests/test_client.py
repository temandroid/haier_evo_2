"""Auth, tokens, 401/429 handling of the REST client (upstream #87)."""
from __future__ import annotations

import logging

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.haier_evo.client import (
    HaierApiError,
    HaierAuthError,
    HaierClient,
    HaierConnectionError,
    HaierRateLimitError,
)

from .conftest import DEVICES_URL, EMAIL, LOGIN_URL, PASSWORD, REFRESH_URL, load_fixture

STORE_KEY = "haier_evo.test"


def make_client(hass: HomeAssistant, store: bool = True) -> HaierClient:
    return HaierClient(
        async_get_clientsession(hass),
        EMAIL,
        PASSWORD,
        "ru",
        store=Store(hass, 1, STORE_KEY) if store else None,
    )


async def test_login_saves_tokens_in_storage(hass, aioclient_mock, hass_storage):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    assert await client.async_get_token() == "test-access-token"
    saved = hass_storage[STORE_KEY]["data"]
    assert saved["token"] == "test-access-token"
    assert saved["refresh_token"] == "test-refresh-token"
    # login form, not JSON, exactly like the mobile app
    assert aioclient_mock.mock_calls[0][2] == {"email": EMAIL, "password": PASSWORD}


async def test_tokens_are_reused_from_storage(hass, aioclient_mock, hass_storage):
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {
            "token": "stored-token",
            "token_expire": "2099-01-01T00:00:00+03:00",
            "refresh_token": "stored-refresh",
            "refresh_expire": "2099-02-01T00:00:00+03:00",
        },
    }
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "stored-token"
    assert aioclient_mock.call_count == 0


async def test_expired_token_is_refreshed(hass, aioclient_mock, hass_storage):
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {
            "token": "old",
            "token_expire": "2000-01-01T00:00:00+03:00",
            "refresh_token": "stored-refresh",
            "refresh_expire": "2099-02-01T00:00:00+03:00",
        },
    }
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "test-access-token"
    assert aioclient_mock.mock_calls[0][1].path.endswith("/auth/refresh")
    assert aioclient_mock.mock_calls[0][2] == {"refreshToken": "stored-refresh"}


async def test_rejected_refresh_falls_back_to_login(hass, aioclient_mock, hass_storage):
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {"token": "old", "token_expire": "2000-01-01T00:00:00+03:00", "refresh_token": "bad"},
    }
    aioclient_mock.post(
        REFRESH_URL, status=400, json={"error": {"validation": {"refreshToken": "invalid"}}}
    )
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "test-access-token"
    assert aioclient_mock.call_count == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, {"error": {"message": "Unauthorized"}}), (400, {"error": "token expired"}), (403, None)],
)
async def test_any_refresh_refusal_falls_back_to_login(hass, aioclient_mock, hass_storage, status, body):
    """Otherwise a dead refresh token survives reauth and loops it forever."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {"token": "old", "token_expire": "2000-01-01T00:00:00+03:00", "refresh_token": "dead"},
    }
    if body is None:
        aioclient_mock.post(REFRESH_URL, status=status, text="")
    else:
        aioclient_mock.post(REFRESH_URL, status=status, json=body)
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "test-access-token"
    assert aioclient_mock.call_count == 2


async def test_wrong_password_is_auth_error(hass, aioclient_mock):
    aioclient_mock.post(
        LOGIN_URL, status=400, json={"error": {"validation": {"password": "Неверный пароль"}}}
    )
    client = make_client(hass, store=False)
    with pytest.raises(HaierAuthError):
        await client.async_login()
    # no more guesses with the same password until reauth (account lockout)
    with pytest.raises(HaierAuthError):
        await client.async_login()
    assert aioclient_mock.call_count == 1


async def test_missing_expiry_does_not_refresh_every_time(hass, aioclient_mock):
    token = load_fixture("login.json")
    token["data"]["token"].pop("expire", None)
    aioclient_mock.post(LOGIN_URL, json=token)
    client = make_client(hass, store=False)
    await client.async_get_token()
    await client.async_get_token()
    assert aioclient_mock.call_count == 1


async def test_429_is_not_a_wrong_password_and_backs_off(hass, aioclient_mock):
    """Upstream #87: 429 was reported as invalid credentials and retried in a loop."""
    aioclient_mock.post(LOGIN_URL, status=429, headers={"Retry-After": "120"}, text="")
    client = make_client(hass, store=False)
    with pytest.raises(HaierRateLimitError) as err:
        await client.async_login()
    assert err.value.retry_after == 120
    # the second attempt does not even hit the server
    with pytest.raises(HaierRateLimitError):
        await client.async_login()
    assert aioclient_mock.call_count == 1


async def test_server_error_is_connection_error(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=503, text="maintenance")
    with pytest.raises(HaierConnectionError):
        await make_client(hass, store=False).async_login()


async def test_network_error_is_connection_error(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, exc=ClientError("boom"))
    with pytest.raises(HaierConnectionError):
        await make_client(hass, store=False).async_login()


async def test_401_on_request_refreshes_and_retries(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    client = make_client(hass, store=False)
    await client.async_login()
    answers = [
        AiohttpClientMockResponse("GET", DEVICES_URL, status=401, text=""),
        AiohttpClientMockResponse("GET", DEVICES_URL, json=load_fixture("devices_page.json")),
    ]

    async def next_answer(*args):
        return answers.pop(0)

    aioclient_mock.get(DEVICES_URL, side_effect=next_answer)
    data = await client.async_get_devices_page()
    assert "presentation" in data
    paths = [call[1].path for call in aioclient_mock.mock_calls]
    assert paths[-1].endswith("/smartHome")
    assert any(p.endswith("/auth/refresh") for p in paths)


async def test_secrets_never_logged(hass, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.haier_evo")
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page.json"))
    client = make_client(hass, store=False)
    await client.async_get_devices_page()
    assert PASSWORD not in caplog.text
    assert "test-access-token" not in caplog.text
    assert "test-refresh-token" not in caplog.text


def _answers(url: str, aioclient_mock, *answers):
    queue = [AiohttpClientMockResponse("GET", url, **a) for a in answers]

    async def next_answer(*args):
        return queue.pop(0)

    aioclient_mock.get(url, side_effect=next_answer)


def _paths(aioclient_mock) -> list[str]:
    return [call[1].path for call in aioclient_mock.mock_calls]


async def test_403_is_final_and_keeps_the_token(hass, aioclient_mock):
    """CR-T-050: stale device-list entries answer 403; that used to rotate the token."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, status=403, text="")
    client = make_client(hass, store=False)
    await client.async_login()
    with pytest.raises(HaierApiError):
        await client.async_get_devices_page()
    paths = _paths(aioclient_mock)
    assert not any(p.endswith("/auth/refresh") for p in paths)
    assert sum(p.endswith("/smartHome") for p in paths) == 1


async def test_refreshed_token_rejected_logs_in_again(hass, aioclient_mock):
    """CR-T-050: 401 after a refresh is no verdict on the password: log in, do not reauth."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    _answers(
        DEVICES_URL, aioclient_mock,
        {"status": 401, "text": ""},
        {"status": 401, "text": ""},
        {"json": load_fixture("devices_page.json")},
    )
    client = make_client(hass, store=False)
    await client.async_login()
    client._login_blocked_until = 0  # the test does not wait out the login throttle
    assert "presentation" in await client.async_get_devices_page()
    paths = _paths(aioclient_mock)
    assert [p.rsplit("/", 1)[-1] for p in paths] == [
        "sign-in", "smartHome", "refresh", "smartHome", "sign-in", "smartHome"
    ]


async def test_token_rejected_after_login_is_a_connection_error(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, status=401, text="")
    client = make_client(hass, store=False)
    await client.async_login()
    client._login_blocked_until = 0  # the test does not wait out the login throttle
    with pytest.raises(HaierConnectionError):
        await client.async_get_devices_page()
    assert not client._auth_failed


async def test_stale_rejection_keeps_a_newer_token(hass, aioclient_mock):
    """A request in flight with the old token must not drop a just refreshed one."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass, store=False)
    await client.async_login()
    client.invalidate_access_token("an-older-token")
    assert await client.async_get_token() == "test-access-token"
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_bare_401_403_on_sign_in_is_not_a_wrong_password(hass, aioclient_mock, status):
    """CR-T-051: a proxy or maintenance page is no reason for reauth."""
    aioclient_mock.post(LOGIN_URL, status=status, text="<html>Forbidden</html>")
    client = make_client(hass, store=False)
    with pytest.raises(HaierConnectionError):
        await client.async_login()
    assert not client._auth_failed


async def test_json_401_on_sign_in_is_a_wrong_password(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=401, json={"error": {"message": "Unauthorized"}})
    with pytest.raises(HaierAuthError):
        await make_client(hass, store=False).async_login()
