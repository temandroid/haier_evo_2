from __future__ import annotations

import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from . import api
from .client import (
    STORAGE_VERSION,
    HaierAuthError,
    HaierClient,
    HaierError,
    HaierRateLimitError,
)
from .const import CONF_REGION, DOMAIN
from .logger import _LOGGER

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

type HaierConfigEntry = ConfigEntry[api.Haier]


def token_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Tokens live in .storage, not in /config (they used to be /config/haier_evo)."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True)


async def async_setup_entry(hass: HomeAssistant, entry: HaierConfigEntry) -> bool:
    if entry.unique_id is None:
        # Entries made by upstream versions have no unique_id.
        hass.config_entries.async_update_entry(
            entry,
            unique_id=f"{str(entry.data[CONF_EMAIL]).strip().lower()}_{entry.data.get(CONF_REGION) or 'ru'}",
        )
    session = async_get_clientsession(hass)
    client = HaierClient(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_REGION) or "ru",
        store=token_store(hass, entry.entry_id),
        # Stable per entry; upstream sent a new random Device-Id on every start.
        device_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{DOMAIN}:{entry.entry_id}")),
    )
    haier = api.Haier(hass, client, session)
    try:
        await haier.async_setup()
    except HaierAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except api.HaierEmptyDeviceList as err:
        # Upstream #105: the cloud sometimes answers with an empty list right
        # after a restart. Let HA retry instead of loading with no entities.
        raise ConfigEntryNotReady("Haier Evo returned no devices, will retry") from err
    except HaierRateLimitError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except HaierError as err:
        raise ConfigEntryNotReady(f"Haier Evo cloud is unavailable: {err}") from err

    entry.runtime_data = haier
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    haier.start(entry)
    _LOGGER.debug("Haier Evo set up with %d device(s)", len(haier.devices))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaierConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await token_store(hass, entry.entry_id).async_remove()
