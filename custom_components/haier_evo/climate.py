from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import api
from .logger import _LOGGER


async def async_setup_entry(
    hass: HomeAssistant, config_entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    haier_object: api.Haier = config_entry.runtime_data
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_climate())
    if entities:
        async_add_entities(entities)


# https://developers.home-assistant.io/docs/core/entity/climate
class HaierACEntity(ClimateEntity):
    _attr_translation_key = "conditioner"
    _attr_should_poll = False
    _attr_icon = "mdi:air-conditioner"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, device: api.HaierAC) -> None:
        self._device = device
        self._attr_unique_id = f"{device.device_id}_{device.device_model}"
        self._attr_name = device.device_name
        self._attr_supported_features = device.get_supported_features()
        self._attr_hvac_modes = device.get_hvac_modes()
        self._attr_fan_modes = device.get_fan_modes()
        self._attr_swing_horizontal_modes = device.get_swing_horizontal_modes()
        self._attr_swing_modes = device.get_swing_modes()
        self._attr_preset_modes = device.get_preset_modes()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.add_listener(self.async_write_ha_state))

    @staticmethod
    def _known(value: str | None, options: list[str] | None) -> str | None:
        """Only report values HA can validate; anything else is 'unknown'."""
        if value is None or not options or value not in options:
            return None
        return value

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation ie. heat, cool mode."""
        if self._device.status is None:
            return None
        if self._device.status != 1:
            return HVACMode.OFF
        # Upstream #61/#73: a mode code missing from the profile produced the
        # string "None" here and HA raised "'None' is not a valid HVACMode".
        mode = self._device.mode
        if mode in self.hvac_modes:
            return HVACMode(mode)
        _LOGGER.debug("%s: unknown hvac mode %r", self._device.device_name, mode)
        return None

    @property
    def swing_horizontal_mode(self) -> str | None:
        return self._known(self._device.swing_horizontal_mode, self.swing_horizontal_modes)

    @property
    def swing_mode(self) -> str | None:
        return self._known(self._device.swing_mode, self.swing_modes)

    @property
    def preset_mode(self) -> str | None:
        return self._known(self._device.preset_mode, self.preset_modes)

    @property
    def fan_mode(self) -> str | None:
        return self._known(self._device.fan_mode, self.fan_modes)

    @property
    def max_temp(self) -> float:
        return self._device.max_temperature

    @property
    def min_temp(self) -> float:
        return self._device.min_temperature

    @property
    def current_temperature(self) -> float | None:
        return self._device.current_temperature

    @property
    def target_temperature(self) -> float | None:
        return self._device.target_temperature

    @property
    def target_temperature_step(self) -> float:
        return self._device.target_temperature_step

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def device_info(self):
        return self._device.device_info

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature (and mode, if given)."""
        if (hvac_mode := kwargs.get("hvac_mode")) is not None:
            await self.async_set_hvac_mode(hvac_mode)
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        _LOGGER.debug("Setting target temperature to %s", temp)
        await self._device.set_temperature(temp)
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        _LOGGER.debug("Setting HVAC mode to %s", hvac_mode)
        if hvac_mode == HVACMode.OFF:
            await self._device.switch_off()
        else:
            await self._device.switch_on(hvac_mode)
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        _LOGGER.debug("Setting fan mode to %s", fan_mode)
        await self._device.set_fan_mode(fan_mode)
        self.async_write_ha_state()

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        _LOGGER.debug("Setting swing horizontal mode to %s", swing_horizontal_mode)
        await self._device.set_swing_horizontal_mode(swing_horizontal_mode)
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        _LOGGER.debug("Setting swing mode to %s", swing_mode)
        await self._device.set_swing_mode(swing_mode)
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        _LOGGER.debug("Setting preset mode to %s", preset_mode)
        await self._device.set_preset_mode(preset_mode)
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self._device.switch_on()
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        await self._device.switch_off()
        self.async_write_ha_state()
