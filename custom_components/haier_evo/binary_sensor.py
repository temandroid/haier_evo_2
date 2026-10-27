from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    haier_object: api.Haier = config_entry.runtime_data
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_binary_sensor())
    if entities:
        async_add_entities(entities)


class HaierBinarySensor(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, device: api.HaierDevice) -> None:
        self._device = device
        self._device_attr_name = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.add_listener(self.async_write_ha_state))

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def is_on(self) -> bool:
        return getattr(self._device, self._device_attr_name, False)


class HaierREFBinarySensor(HaierBinarySensor):
    _attr_icon = "mdi:fridge-outline"


class HaierREFDoorSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "door_open"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_door_open"
        self._attr_name = f"{device.device_name} Дверь"


class HaierREFVacationSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "vacation_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_vacation"
        self._attr_name = f"{device.device_name} Режим Отпуск"


class HaierREFSuperFreezeSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_freeze"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_freeze"
        self._attr_name = f"{device.device_name} Супер-заморозка"


class HaierREFSuperCoolingSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_cooling"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_cooling"
        self._attr_name = f"{device.device_name} Супер-охлаждение"


class HaierACProblemSensor(HaierBinarySensor):
    """On while the unit reports alarms; texts come from the cloud's error table."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_problem"
        self._attr_name = f"{device.device_name} Ошибка"

    @property
    def is_on(self) -> bool:
        return bool(self._device.alarms)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"alarms": self._device.alarms, "errors": self._device.alarm_texts}
