from __future__ import annotations

import json
import os
import re
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
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
from .const import (
    CACHE_SAVE_DELAY,
    CACHE_VERSION,
    CONF_REGION,
    DOMAIN,
    SETUP_INCOMPLETE_RETRIES,
)
from .logger import _LOGGER, mask_macs

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

type HaierConfigEntry = ConfigEntry[api.Haier]

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
# entry_id -> setups in a row that got fewer devices than the registry knows
_INCOMPLETE_SETUPS: dict[str, int] = {}


def token_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Tokens live in .storage, not in /config (they used to be /config/haier_evo)."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True)


def devices_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Device descriptions of the last good start (MACs, serials: private too)."""
    return Store(hass, CACHE_VERSION, f"{DOMAIN}.{entry_id}.devices", private=True)


def _known_macs(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """MACs of the Haier devices this entry created before."""
    registry = dr.async_get(hass)
    return {
        identifier.lower()
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN and _MAC.match(identifier.lower())
    }


def entry_device_id(entry_id: str) -> str:
    """Stable Device-Id per entry; upstream sent a new random one on every start."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{DOMAIN}:{entry_id}"))


def flow_tokens(hass: HomeAssistant) -> dict[str, dict]:
    """Tokens of the login the config flow just made, by unique_id.

    Without them the entry would log in a second time right after the flow,
    and the sign-in endpoint is quick to answer 429 (upstream #87).
    """
    return hass.data.setdefault(DOMAIN, {}).setdefault("flow_tokens", {})


def _remove_upstream_token_file(path: str) -> None:
    """Upstream kept live tokens as plain JSON in /config/haier_evo."""
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and {"token", "refresh_token"} & data.keys():
        os.remove(path)
        _LOGGER.info("Removed the token file of the upstream integration")


async def async_setup_entry(hass: HomeAssistant, entry: HaierConfigEntry) -> None:
    if entry.unique_id is None:
        # Entries made by upstream versions have no unique_id.
        unique_id = f"{str(entry.data[CONF_EMAIL]).strip().lower()}_{entry.data.get(CONF_REGION) or 'ru'}"
        if hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id) is None:
            hass.config_entries.async_update_entry(entry, unique_id=unique_id)
        else:
            _LOGGER.warning("Another Haier Evo entry already uses this account, leaving this one as is")
    await hass.async_add_executor_job(_remove_upstream_token_file, hass.config.path(DOMAIN))
    session = async_get_clientsession(hass)
    client = HaierClient(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_REGION) or "ru",
        store=token_store(hass, entry.entry_id),
        device_id=entry_device_id(entry.entry_id),
    )
    if entry.unique_id and (tokens := flow_tokens(hass).pop(entry.unique_id, None)):
        await client.async_import_tokens(tokens)
    haier = api.Haier(hass, client, session)
    cache = devices_store(hass, entry.entry_id)
    try:
        await haier.async_setup()
    except HaierAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except HaierError as err:
        # Upstream #105 (an empty list right after a restart), 429, network,
        # a token the cloud keeps refusing: the cloud is not usable right now.
        if not await _async_setup_from_cache(haier, cache, err):
            if isinstance(err, api.HaierEmptyDeviceList):
                raise ConfigEntryNotReady("Haier Evo returned no devices, will retry") from err
            if isinstance(err, HaierRateLimitError):
                raise ConfigEntryNotReady(str(err)) from err
            raise ConfigEntryNotReady(f"Haier Evo cloud is unavailable: {err}") from err
    else:
        _check_complete(hass, entry, haier)
        await cache.async_save(haier.cache_dict())

    haier.cache_store = cache
    haier.on_refreshed = lambda: cache.async_delay_save(haier.cache_dict, CACHE_SAVE_DELAY)
    entry.runtime_data = haier
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    haier.start(entry)
    _LOGGER.debug("Haier Evo set up with %d device(s)", len(haier.devices))


async def _async_setup_from_cache(haier: api.Haier, cache: Store, err: Exception) -> bool:
    """The cloud is not usable at start: build the devices from the last good start.

    The entities come up at once instead of waiting in "setup retry"; the
    WebSocket and a background check bring the real state as soon as the
    cloud answers (the power cut of 2026-11-04: the cloud came back later than HA).
    """
    try:
        cached = await cache.async_load()
    except Exception as load_err:  # noqa: BLE001 - a broken file is no reason to fail
        _LOGGER.warning("Saved Haier Evo device descriptions are not readable: %s", load_err)
        return False
    if cached is None:
        return False
    try:
        await haier.async_setup_from_cache(cached)
    except (ValueError, TypeError, KeyError, AttributeError) as build_err:
        _LOGGER.warning("Saved Haier Evo device descriptions are not usable: %s", build_err)
        await cache.async_remove()
        return False
    _LOGGER.warning(
        "Haier Evo cloud is not available (%s): started %d device(s) from the last "
        "good start, the real state follows once the cloud answers",
        err, len(haier.devices),
    )
    return True


def _check_complete(hass: HomeAssistant, entry: ConfigEntry, haier: api.Haier) -> None:
    """Fewer devices than the registry knows: retry the setup a few times.

    The cloud sometimes leaves a unit out of the list or does not describe it
    right after an outage; starting without it would make its entities
    disappear until the next restart.
    """
    found = {str(device.device_id).lower() for device in haier.devices}
    missing = _known_macs(hass, entry) - found
    if not missing:
        _INCOMPLETE_SETUPS.pop(entry.entry_id, None)
        return
    attempts = _INCOMPLETE_SETUPS.get(entry.entry_id, 0) + 1
    if attempts <= SETUP_INCOMPLETE_RETRIES:
        _INCOMPLETE_SETUPS[entry.entry_id] = attempts
        raise ConfigEntryNotReady(mask_macs(
            f"Haier Evo did not return {len(missing)} known device(s): {', '.join(sorted(missing))} "
            f"(attempt {attempts}/{SETUP_INCOMPLETE_RETRIES})"
        ))
    _INCOMPLETE_SETUPS.pop(entry.entry_id, None)
    _LOGGER.warning(
        "Haier Evo still does not return %s, starting with the %d device(s) it returned; "
        "remove a device in Home Assistant if it is gone from the Evo app",
        ", ".join(sorted(missing)), len(found),
    )


async def async_unload_entry(hass: HomeAssistant, entry: HaierConfigEntry) -> None:
    # HA 2026.11: entry functions return None, a failed unload raises.
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
    haier = entry.runtime_data
    await haier.async_stop()
    if haier.cache_store is not None:
        # Written now: a delayed write left pending would outlive the entry
        # and could bring the file back after the integration is deleted.
        haier.on_refreshed = None
        await haier.cache_store.async_save(haier.cache_dict())


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await token_store(hass, entry.entry_id).async_remove()
    await devices_store(hass, entry.entry_id).async_remove()
    _INCOMPLETE_SETUPS.pop(entry.entry_id, None)
