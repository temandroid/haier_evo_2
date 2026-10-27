"""Entry setup/unload, empty device list, 429, auth failure, diagnostics."""
from __future__ import annotations

import json
import os

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haier_evo.const import DOMAIN
from custom_components.haier_evo.diagnostics import async_get_config_entry_diagnostics

from .conftest import DEVICES_URL, EMAIL, LOGIN_URL, MAC_1, MAC_2, PASSWORD


async def test_setup_creates_entities(hass: HomeAssistant, setup_entry):
    assert setup_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(registry, setup_entry.entry_id)}
    assert f"{MAC_1}_AS25PHP3HRA" in unique_ids
    assert f"{MAC_2}_AS70S2SF2FA" in unique_ids
    # no more "Haier Evo HTTP GET/POST" switches (upstream #100)
    assert not [u for u in unique_ids if "http_switch" in u]

    entity_id = registry.async_get_entity_id("climate", DOMAIN, f"{MAC_1}_AS25PHP3HRA")
    state = hass.states.get(entity_id)
    assert state.state == "cool"
    assert state.attributes["current_temperature"] == 22.5
    assert state.attributes["temperature"] == 24
    assert state.attributes["fan_mode"] == "auto"
    assert state.attributes["min_temp"] == 16
    assert state.attributes["max_temp"] == 30
    assert set(state.attributes["hvac_modes"]) == {"auto", "cool", "dry", "heat", "fan_only", "off"}


async def test_tokens_not_written_to_config_dir(hass: HomeAssistant, setup_entry, hass_storage):
    assert f"{DOMAIN}.{setup_entry.entry_id}" in hass_storage
    # the old client kept tokens in /config/haier_evo next to configuration.yaml
    import os
    assert not os.path.exists(hass.config.path(DOMAIN))


async def test_empty_device_list_retries(hass: HomeAssistant, config_entry, mock_cloud, ws_factory):
    """Upstream #105: an empty deviceList must not load an empty integration."""
    mock_cloud(devices="devices_page_empty.json")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert ws_factory.urls == []


async def test_429_on_setup_retries(hass: HomeAssistant, config_entry, mock_cloud):
    mock_cloud(login_status=429)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_cloud_down_on_setup_retries(hass: HomeAssistant, config_entry, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=502, text="Bad gateway")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_wrong_password_starts_reauth(hass: HomeAssistant, config_entry, aioclient_mock):
    aioclient_mock.post(
        LOGIN_URL, status=400, json={"error": {"validation": {"password": "Неверный пароль"}}}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_old_entry_without_unique_id_is_migrated(hass, mock_cloud, ws_factory):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"email": EMAIL, "password": PASSWORD, "region": "ru"}
    )
    entry.add_to_hass(hass)
    mock_cloud()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.unique_id == f"{EMAIL}_ru"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unload(hass: HomeAssistant, setup_entry, ws_factory):
    ws = ws_factory.current
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert ws.closed


async def test_diagnostics_are_redacted(hass: HomeAssistant, setup_entry):
    # Real status payloads carry the MAC in free-form strings and presigned icon URLs.
    device = setup_entry.runtime_data.devices[0]
    device._status_data = {
        **(device.status_data or {}),
        "scenarios": {"default": {
            "title": "Сценарии",
            "deeplink": f"haierevo://hiHome/device/scenario?macAddress={MAC_1}",
            "icon": "https://storage.example/icon.png?X-Amz-Credential=TESTKEY&X-Amz-Signature=abc",
        }},
        "note": f"encoded {MAC_2.replace(':', '%3A').upper()}",
    }
    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    text = json.dumps(diag, ensure_ascii=False)
    for secret in (
        PASSWORD, EMAIL, MAC_1, MAC_2, MAC_2.replace(":", "%3A").upper(),
        "TESTSERIAL0001", "test-access-token", "TESTKEY",
    ):
        assert secret not in text
    assert diag["entry"]["title"] == "**REDACTED**"
    scenario = diag["haier"]["devices"][0]["backend_data"]["scenarios"]["default"]
    assert scenario["title"] == "Сценарии"
    assert scenario["icon"] == "https://storage.example/icon.png"
    assert diag["haier"]["devices"][0]["device_model"] == "AS25PHP3HRA"


async def test_diagnostics_of_a_failed_entry(hass: HomeAssistant, config_entry, mock_cloud):
    mock_cloud(login_status=429)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["haier"] is None
    assert EMAIL not in json.dumps(diag)


async def test_upstream_token_file_is_removed(hass: HomeAssistant, config_entry, mock_cloud, ws_factory):
    path = hass.config.path(DOMAIN)
    await hass.async_add_executor_job(
        lambda: open(path, "w", encoding="utf-8").write('{"token": "x", "refresh_token": "y"}')
    )
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert not await hass.async_add_executor_job(os.path.exists, path)
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_devices_request_has_no_token_in_log(hass, setup_entry, aioclient_mock, caplog):
    assert DEVICES_URL.split("?")[0] in str(aioclient_mock.mock_calls[1][1])
    assert "test-access-token" not in caplog.text
