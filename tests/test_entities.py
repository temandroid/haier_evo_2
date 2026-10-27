"""Entities built from live-unit findings: outdoor sensor, alarms, categories, modifiers."""
from __future__ import annotations

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo import const as C

from .test_websocket import CLIMATE_UID, climate_id, status_message


def entity_id(hass: HomeAssistant, domain: str, suffix: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, C.DOMAIN, f"{CLIMATE_UID}_{suffix}")


async def test_outdoor_temperature_sensor(hass, setup_entry, ws_factory):
    sensor = entity_id(hass, "sensor", "outdoor_temperature")
    assert hass.states.get(sensor).state == "11.0"
    # code 36 stays the room temperature of the climate entity
    assert hass.states.get(climate_id(hass)).attributes["current_temperature"] == 22.5
    ws_factory.current.feed(status_message({"38": "9"}))
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "9.0"


async def test_problem_sensor_follows_alarms(hass, setup_entry, ws_factory):
    problem = entity_id(hass, "binary_sensor", "problem")
    assert hass.states.get(problem).state == "off"
    assert er.async_get(hass).async_get(problem).entity_category is EntityCategory.DIAGNOSTIC
    ws = ws_factory.current
    ws.feed({
        "event": "status",
        "macAddress": setup_entry.runtime_data.devices[0].device_mac,
        "payload": {"statuses": [{"properties": {}, "alarms": ["indoorFanErr", "mysteryErr"]}]},
    })
    await hass.async_block_till_done()
    state = hass.states.get(problem)
    assert state.state == "on"
    assert state.attributes["alarms"] == ["indoorFanErr", "mysteryErr"]
    assert state.attributes["errors"] == [
        "[E14] Ошибка вентилятора внутреннего блока.",
        "Неизвестная ошибка (mysteryErr)",
    ]


async def test_duplicates_disabled_and_config_category(hass, setup_entry):
    registry = er.async_get(hass)
    quiet = registry.async_get(entity_id(hass, "switch", "quiet"))
    assert quiet.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    for suffix in ("light", "sound"):
        assert registry.async_get(entity_id(hass, "switch", suffix)).entity_category is EntityCategory.CONFIG


async def test_special_swing_mode_hidden(hass, setup_entry):
    modes = hass.states.get(climate_id(hass)).attributes["swing_modes"]
    assert "auto" in modes
    assert "special" not in modes


async def test_temperature_of_an_off_unit_is_sent_on_turn_on(hass, setup_entry, ws_factory):
    """Alice may set the temperature before turning the unit on ("включи на 23")."""
    ws = ws_factory.current
    ws.feed(status_message({"21": "0"}))
    await hass.async_block_till_done()
    ws.sent.clear()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    # the cloud locks the setpoint of an off unit: nothing goes out yet
    assert ws.sent == []
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 23
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "cool"}, blocking=True
    )
    assert {"commandName": "21", "value": "1"} in ws.sent[0]["commands"]
    assert ws.sent[-1]["commands"] == [{"commandName": "0", "value": "23"}]


async def test_stale_deferred_temperature_is_dropped(hass, setup_entry, ws_factory):
    """Turned on with the remote meanwhile: a later turn_on must not resend 23."""
    ws = ws_factory.current
    ws.feed(status_message({"21": "0"}))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    ws.feed(status_message({"21": "1"}))
    await hass.async_block_till_done()
    ws.sent.clear()
    await hass.services.async_call("climate", "turn_on", {"entity_id": climate_id(hass)}, blocking=True)
    assert all(c["commandName"] != "0" for m in ws.sent for c in m["commands"])


async def test_turn_off_with_temperature_does_not_fail(hass, setup_entry, ws_factory):
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": climate_id(hass), "temperature": 21, "hvac_mode": "off"}, blocking=True,
    )
    assert hass.states.get(climate_id(hass)).state == "off"


async def test_preset_shows_up_before_the_echo(hass, setup_entry, ws_factory):
    device = setup_entry.runtime_data.devices[0]
    await device.set_preset_mode("sleep")
    assert device.preset_mode == "sleep"
    await device.set_preset_mode("none")
    assert device.preset_mode == "none"


async def test_turn_on_running_unit_with_alarm(hass, setup_entry, ws_factory):
    """An alarm locks the mode, but turn_on without a mode change is not a change."""
    ws = ws_factory.current
    ws.feed({
        "event": "status",
        "macAddress": setup_entry.runtime_data.devices[0].device_mac,
        "payload": {"statuses": [{"properties": {}, "alarms": ["outdoorFanErr"]}]},
    })
    await hass.async_block_till_done()
    await hass.services.async_call("climate", "turn_on", {"entity_id": climate_id(hass)}, blocking=True)


async def test_our_own_turn_off_locks_without_echo(hass, setup_entry, ws_factory):
    await hass.services.async_call("climate", "turn_off", {"entity_id": climate_id(hass)}, blocking=True)
    with pytest.raises(HomeAssistantError) as err:
        await setup_entry.runtime_data.devices[0].set_light_on(False)
    assert err.value.translation_key == "locked_off"


async def test_quiet_locked_in_auto_mode(hass, setup_entry, ws_factory):
    ws_factory.current.feed(status_message({"2": "0"}))
    await hass.async_block_till_done()
    device = setup_entry.runtime_data.devices[0]
    with pytest.raises(HomeAssistantError) as err:
        await device.set_quiet_on(True)
    assert err.value.translation_key == "locked_mode"
    with pytest.raises(HomeAssistantError):
        await device.set_preset_mode("sleep")
    # light is not restricted in auto
    await device.set_light_on(False)


async def test_fan_only_restricts_fan_values(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.feed(status_message({"2": "6"}))
    await hass.async_block_till_done()
    device = setup_entry.runtime_data.devices[0]
    with pytest.raises(HomeAssistantError) as err:
        await device.set_fan_mode("auto")
    assert err.value.translation_key == "value_not_allowed"
    ws.sent.clear()
    await device.set_fan_mode("high")
    assert ws.sent[0]["commands"] == [{"commandName": "4", "value": "1"}]


async def test_alarm_locks_controls(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.feed({
        "event": "status",
        "macAddress": setup_entry.runtime_data.devices[0].device_mac,
        "payload": {"statuses": [{"properties": {}, "alarms": ["outdoorFanErr"]}]},
    })
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError) as err:
        await setup_entry.runtime_data.devices[0].set_temperature(22)
    assert err.value.translation_key == "locked_alarm"


async def test_alarms_from_rest_resync(hass, setup_entry):
    device = setup_entry.runtime_data.devices[0]
    device.update_from_backend({**device.status_data, "alarms": [{"name": "indoorFanErr"}]})
    assert device.alarms == ["indoorFanErr"]
    device.update_from_backend({**device.status_data, "alarms": []})
    assert device.alarms == []
