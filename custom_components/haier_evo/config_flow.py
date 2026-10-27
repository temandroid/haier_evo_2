from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import flow_tokens
from .client import (
    HaierAuthError,
    HaierClient,
    HaierConnectionError,
    HaierError,
    HaierRateLimitError,
)
from .const import CONF_REGION, DOMAIN, REGIONS
from .logger import _LOGGER


def _schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_EMAIL, default=defaults.get(CONF_EMAIL, "")): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=defaults.get(CONF_REGION, "ru")): vol.In(REGIONS),
    })


class HaierEvoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Login is checked against the cloud before the entry is saved.

    There is no options flow any more: the old one assigned
    `self.config_entry`, which HA 2025.12+ forbids, and the settings page
    answered 500 (upstream #96, #98). Credentials are changed with
    "Reconfigure", and a rejected password starts the reauth flow.
    """

    VERSION = 1

    async def _async_check(self, data: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        email = str(data[CONF_EMAIL]).strip()
        if len(email) < 3 or "@" not in email:
            return {CONF_EMAIL: "invalid_email"}
        client = HaierClient(
            async_get_clientsession(self.hass), email, data[CONF_PASSWORD], data[CONF_REGION]
        )
        try:
            await client.async_login()
        except HaierAuthError:
            errors["base"] = "invalid_auth"
        except HaierRateLimitError:
            errors["base"] = "too_many_requests"
        except HaierConnectionError:
            errors["base"] = "cannot_connect"
        except HaierError:
            errors["base"] = "unknown"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error while checking Haier Evo login")
            errors["base"] = "unknown"
        if not errors:
            flow_tokens(self.hass)[self._unique_id(data)] = client.export_tokens()
        return errors

    @staticmethod
    def _unique_id(data: Mapping[str, Any]) -> str:
        return f"{str(data[CONF_EMAIL]).strip().lower()}_{data[CONF_REGION]}"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_EMAIL] = str(user_input[CONF_EMAIL]).strip()
            await self.async_set_unique_id(self._unique_id(user_input))
            self._abort_if_unique_id_configured()
            errors = await self._async_check(user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"{user_input[CONF_EMAIL]} ({user_input[CONF_REGION]})",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors = await self._async_check(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": entry.data.get(CONF_EMAIL, "")},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_EMAIL] = str(user_input[CONF_EMAIL]).strip()
            await self.async_set_unique_id(self._unique_id(user_input))
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            errors = await self._async_check(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    title=f"{user_input[CONF_EMAIL]} ({user_input[CONF_REGION]})",
                    data={**entry.data, **user_input},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or entry.data),
            errors=errors,
        )
