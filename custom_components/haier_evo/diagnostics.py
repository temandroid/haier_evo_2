"""Diagnostics: replaces the unauthenticated /api/haier_evo endpoint (upstream #100)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    "token",
    "accessToken",
    "refreshToken",
    "device_id",
    "device_mac",
    "macAddress",
    "mac",
    "deviceId",
    "device_serial",
    "serialNumber",
    "serialNum",
    "link",
    "unique_id",
    "title",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    haier = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "haier": async_redact_data(haier.to_dict(), TO_REDACT),
    }
