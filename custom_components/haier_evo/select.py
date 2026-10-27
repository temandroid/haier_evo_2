from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant

from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    haier_object: api.Haier = config_entry.runtime_data
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_select())
    if entities:
        async_add_entities(entities)


class HaierSelect(SelectEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, device: api.HaierDevice) -> None:
        self._device = device
        self._device_attr_name = None
        self._attr_options = []

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.add_listener(self.async_write_ha_state))

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def current_option(self) -> str | None:
        value = getattr(self._device, self._device_attr_name, None)
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        method = getattr(self._device, f"set_{self._device_attr_name}", None)
        if method is not None:
            await method(option)
            self.async_write_ha_state()


class HaierACEcoSensorSelect(HaierSelect):
    _attr_translation_key = "conditioner_eco_sensor"

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "eco_sensor"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_eco_sensor"
        self._attr_name = f"{device.device_name} Эко-датчик"
        self._attr_options = device.get_eco_sensor_options()


class HaierREFFridgeModeSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "fridge_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_mode_select"
        self._attr_name = f"{device.device_name} Режим холодильной камеры"
        self._attr_options = device.get_fridge_mode_options()


class HaierREFFreezerModeSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "freezer_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_mode_select"
        self._attr_name = f"{device.device_name} Режим морозильной камеры"
        self._attr_options = device.get_freezer_mode_options()


class HaierREFMyZoneSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "my_zone"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_my_zone"
        self._attr_name = f"{device.device_name} My Zone"
        self._attr_options = device.get_my_zone_options()


class HaierWMProgramSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "program"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_program"
        self._attr_name = f"{device.device_name} Программа"
        self._attr_options = device.get_program_options()


class HaierWMTemperatureSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = f"{device.device_name} Температура"
        self._attr_options = device.get_temperature_options()


class HaierWMSpinSpeedSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "spin_speed"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_spin_speed"
        self._attr_name = f"{device.device_name} Скорость отжима"
        self._attr_options = device.get_spin_speed_options()
