"""Diagnostics: replaces the unauthenticated /api/haier_evo endpoint (upstream #100)."""
from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
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
}
# The entry title is the account email; inside the backend data "title" is
# only a UI label and is worth keeping.
TO_REDACT_ENTRY = TO_REDACT | {"title"}

# MACs also hide inside free-form strings, e.g. scenario deeplinks
# "haierevo://...?macAddress=aa:bb:...". Colon, dash and %3A separators.
MAC_RE = re.compile(r"[0-9a-f]{2}(?:(?::|-|%3a)[0-9a-f]{2}){5}", re.IGNORECASE)
# Icon links are presigned storage URLs: the query carries a credential and
# a signature that have nothing to do with debugging.
URL_QUERY_RE = re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']*")


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        value = URL_QUERY_RE.sub(r"\1", value)
        return MAC_RE.sub(REDACTED, value)
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    haier = entry.runtime_data
    return {
        "entry": _scrub(async_redact_data(entry.as_dict(), TO_REDACT_ENTRY)),
        "haier": _scrub(async_redact_data(haier.to_dict(), TO_REDACT)),
    }
