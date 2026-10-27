"""Config flow: login check, errors, reauth, reconfigure (upstream #96, #98, #87)."""
from __future__ import annotations

from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.haier_evo.const import DOMAIN

from .conftest import EMAIL, LOGIN_URL, PASSWORD, load_fixture

USER_INPUT = {"email": f"  {EMAIL}  ", "password": PASSWORD, "region": "ru"}


async def _start(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return result


async def test_user_flow_creates_entry(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"{EMAIL} (ru)"
    assert result["data"] == {"email": EMAIL, "password": PASSWORD, "region": "ru"}
    assert result["result"].unique_id == f"{EMAIL}_ru"


async def test_user_flow_invalid_auth(hass, aioclient_mock):
    aioclient_mock.post(
        LOGIN_URL, status=400, json={"error": {"validation": {"password": "Неверный пароль"}}}
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_too_many_requests(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=429, text="")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "too_many_requests"}


async def test_user_flow_cannot_connect(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, exc=ClientError())
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_invalid_email(hass, aioclient_mock):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "email": "abc"}
    )
    assert result["errors"] == {"email": "invalid_email"}
    assert aioclient_mock.call_count == 0


async def test_user_flow_duplicate(hass, aioclient_mock, config_entry):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_options_flow(hass, config_entry):
    """The broken OptionsFlow is gone, so the gear cannot answer 500 any more."""
    assert config_entry.supports_options is False
    assert config_entry.supports_reconfigure is True


async def test_reauth(hass, aioclient_mock, config_entry):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["password"] == "new-password"
    assert config_entry.data["email"] == EMAIL


async def test_reconfigure(hass, aioclient_mock, config_entry):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": "other-password", "region": "ru"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data["password"] == "other-password"


async def test_reconfigure_other_account_is_refused(hass, aioclient_mock, config_entry):
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "other@example.com", "password": "x" * 8, "region": "ru"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
