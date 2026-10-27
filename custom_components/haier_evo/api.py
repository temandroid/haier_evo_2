from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from aiohttp import (
    ClientError,
    ClientSession,
    ClientWebSocketResponse,
    WSMsgType,
    WSServerHandshakeError,
)
from homeassistant.components.climate.const import (
    PRESET_NONE,
    SWING_OFF,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import config as CFG  # noqa: N812
from . import const as C  # noqa: N812
from .client import (
    HaierApiError,
    HaierAuthError,
    HaierClient,
    HaierConnectionError,
    HaierError,
    HaierRateLimitError,
)
from .logger import _LOGGER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DEVICE_LIST_COMPONENT_ID = "72a6d224-cb66-4e6d-b427-2e4609252684"


class HaierCommandError(HomeAssistantError):
    """Command could not be delivered to the Haier Evo cloud."""


class HaierCommandLocked(HomeAssistantError):
    """The cloud marks the attribute read-only in the current state (modifiers)."""


class HaierEmptyDeviceList(HaierError):
    """The cloud answered with no devices (upstream #105)."""


def parse_device_list(data: dict) -> list[dict[str, str]]:
    """Extract devices from the smartHome sduiRawPaginated page."""
    layout = ((data or {}).get("presentation") or {}).get("layout") or {}
    candidates = []
    for item in layout.get("scrollContainer") or []:
        if not isinstance(item, dict):
            continue
        component = (item.get("trackingData") or {}).get("component") or {}
        if component.get("componentName") != "deviceList":
            continue
        # Prefer the known container, but do not depend on its id.
        candidates.append((component.get("componentId") != DEVICE_LIST_COMPONENT_ID, item))
    candidates.sort(key=lambda c: c[0])
    devices: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, item in candidates:
        state = item.get("state") or "{}"
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except ValueError:
                continue
        for entry in (state or {}).get("items") or []:
            if not isinstance(entry, dict):
                continue
            link = (entry.get("action") or {}).get("link") or ""
            query = parse_qs(urlparse(link).query)
            mac = (query.get("deviceId") or [""])[0].replace("%3A", ":")
            if not mac or mac.lower() in seen:
                continue
            seen.add(mac.lower())
            devices.append({
                "mac": mac,
                "type": (query.get("type") or ["UNKNOWN"])[0],
                "serial": (query.get("serialNum") or [""])[0],
                "title": entry.get("title") or "",
            })
    return devices


class Haier:
    """One Evo account: device list, WebSocket and command delivery."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: HaierClient,
        session: ClientSession,
    ) -> None:
        self.hass = hass
        self.client = client
        self._session = session
        self.devices: list[HaierDevice] = []
        self.backend_data: dict | None = None
        self._entry: ConfigEntry | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._connected = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._ever_connected = False
        self._disconnected_since: float | None = self._now()
        self._pending: dict[str, dict] = {}  # trace -> command waiting for command_response
        self._unavailable_timer: CALLBACK_TYPE | None = None
        self.reconnects = 0
        self.lost_commands = 0

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "lost_commands": self.lost_commands,
            # The raw page embeds MACs inside JSON strings: keep only the summary.
            "device_list": [
                {"type": d["type"], "title": d["title"]}
                for d in parse_device_list(self.backend_data or {})
            ],
            "devices": [device.to_dict() for device in self.devices],
        }

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and self._ws is not None and not self._ws.closed

    @property
    def cloud_available(self) -> bool:
        """False once the WebSocket has been down for too long (upstream #103)."""
        if self.connected:
            return True
        since = self._disconnected_since
        return since is None or self._now() - since < C.WS_UNAVAILABLE_AFTER

    def get_device_by_id(self, id_: str) -> HaierDevice | None:
        id_ = str(id_).lower()
        return next((d for d in self.devices if str(d.device_id).lower() == id_), None)

    # --- setup / teardown ---

    async def async_setup(self) -> None:
        """Load tokens and devices. Raises HaierError subclasses."""
        await self.client.async_load_tokens()
        self.backend_data = await self.client.async_get_devices_page()
        found = parse_device_list(self.backend_data)
        if not found:
            raise HaierEmptyDeviceList("Device list is empty")
        for info in found:
            try:
                status = await self.client.async_get_device_status(info["mac"])
            except HaierApiError as err:
                _LOGGER.warning("Skipping device %s: %s", info["title"] or info["mac"], err)
                continue
            # Profiles are YAML files: build the device off the event loop.
            device = await self.hass.async_add_executor_job(partial(
                HaierDevice.create,
                haier=self,
                device_type=info["type"],
                device_mac=info["mac"],
                device_serial=info["serial"],
                device_title=info["title"],
                backend_data=status,
            ))
            self.devices.append(device)
            _LOGGER.info("Added device: %s", device)
        if not self.devices:
            raise HaierEmptyDeviceList("No device status could be read")

    def start(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._task = entry.async_create_background_task(
            self.hass, self._async_ws_loop(), f"{C.DOMAIN} websocket"
        )
        # Push is the main source of state; this only catches a silent session
        # that stays open but stops delivering statuses.
        entry.async_on_unload(async_track_time_interval(
            self.hass, self._async_periodic_refresh, C.REST_REFRESH_INTERVAL,
            name=f"{C.DOMAIN} refresh", cancel_on_shutdown=True,
        ))

    def _create_task(self, coro: Any, name: str) -> None:
        # Entry background tasks are cancelled on unload.
        if self._entry is not None:
            self._entry.async_create_background_task(self.hass, coro, f"{C.DOMAIN} {name}")
        else:
            self.hass.async_create_task(coro, eager_start=False)

    async def async_stop(self) -> None:
        self._stopping = True
        self._cancel_unavailable_timer()
        self._wakeup.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # --- websocket ---

    def _mark_disconnected(self) -> None:
        self._connected.clear()
        if self._disconnected_since is None:
            self._disconnected_since = self._now()
        self._notify_all()
        # cloud_available is only evaluated when an entity writes its state:
        # without this nudge nothing would turn the entities unavailable.
        if self._unavailable_timer is None and not self._stopping:
            self._unavailable_timer = async_call_later(
                self.hass, C.WS_UNAVAILABLE_AFTER + 1, self._on_unavailable
            )

    @callback
    def _on_unavailable(self, _now: Any) -> None:
        self._unavailable_timer = None
        self._notify_all()

    def _cancel_unavailable_timer(self) -> None:
        if self._unavailable_timer is not None:
            self._unavailable_timer()
            self._unavailable_timer = None

    def _notify_all(self) -> None:
        for device in self.devices:
            device.notify()

    async def _async_ws_loop(self) -> None:
        backoff = C.WS_BACKOFF_MIN
        while not self._stopping:
            started = self._now()
            delay = 0.0
            try:
                await self._async_ws_session()
            except asyncio.CancelledError:
                raise
            except HaierAuthError:
                _LOGGER.error("Haier Evo rejected the credentials, re-authentication is required")
                self._mark_disconnected()
                if self._entry is not None:
                    self._entry.async_start_reauth(self.hass)
                return
            except HaierRateLimitError as err:
                _LOGGER.warning("Haier Evo: %s", err)
                delay = err.retry_after
            except HaierError as err:
                _LOGGER.warning("Haier Evo WebSocket: %s", err)
            except Exception:  # noqa: BLE001 - keep the loop alive whatever happens
                _LOGGER.exception("Unexpected Haier Evo WebSocket error")
            self._mark_disconnected()
            if self._stopping:
                break
            if self._now() - started > C.WS_STABLE_AFTER:
                backoff = C.WS_BACKOFF_MIN
            delay = max(delay, backoff)
            backoff = min(backoff * 2, C.WS_BACKOFF_MAX)
            # A pending command wakes the loop up early: it is waiting for us.
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), delay)
            self._wakeup.clear()

    async def _async_ws_session(self) -> None:
        token = await self.client.async_get_token()
        try:
            ws = await self._session.ws_connect(
                self.client.ws_url(token), heartbeat=C.WS_HEARTBEAT
            )
        except WSServerHandshakeError as err:
            # The exception text contains the URL, i.e. the token: never log it.
            if err.status in (401, 403):
                self.client.invalidate_access_token()
            raise HaierConnectionError(f"WebSocket handshake failed: HTTP {err.status}") from None
        except (ClientError, TimeoutError) as err:
            raise HaierConnectionError(f"WebSocket connect failed: {type(err).__name__}") from None

        outage = None if self._disconnected_since is None else self._now() - self._disconnected_since
        self._ws = ws
        self._disconnected_since = None
        self._cancel_unavailable_timer()
        self._connected.set()
        if self._ever_connected:
            self.reconnects += 1
        _LOGGER.debug("WebSocket connected")
        if self._ever_connected and outage is not None and outage > C.WS_RESYNC_AFTER:
            self._create_task(self._async_resync(), "resync")
        self._ever_connected = True
        self._notify_all()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self._handle_message(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._connected.clear()
            if self._ws is ws:
                self._ws = None
            if not ws.closed:
                await ws.close()
            _LOGGER.debug("WebSocket closed, code %s", ws.close_code)

    async def _async_periodic_refresh(self, _now: Any = None) -> None:
        await self._async_resync()

    async def _async_resync(self, devices: list[HaierDevice] | None = None) -> None:
        """Re-read device statuses via REST (after an outage, a lost command, or periodically)."""
        for device in devices if devices is not None else self.devices:
            started = self._now()
            try:
                status = await self.client.async_get_device_status(device.device_id)
            except HaierError as err:
                _LOGGER.debug("Resync of %s failed: %s", device.device_name, err)
                continue
            if devices is None and device.changed_at > started:
                # A push or a command came in while the request was in flight:
                # the REST snapshot is older than what the entity already shows.
                # A targeted resync (lost or rejected command) applies anyway:
                # it exists to undo an optimistic value.
                continue
            device.update_from_backend(status)
            device.notify()

    def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Ignoring non-JSON WebSocket message")
            return
        if not isinstance(message, dict):
            return
        _LOGGER.debug("Received WSS message: %s", message)
        try:
            if message.get("event") == "command_response":
                self._handle_ack(message)
            device = self.get_device_by_id(message.get("macAddress") or "")
            if device is None:
                _LOGGER.debug("Message for an unknown device: %s", message.get("event"))
                return
            device.on_message(message)
        except Exception:  # noqa: BLE001 - one bad message must not kill the socket
            _LOGGER.exception("Failed to handle %s message", message.get("event"))

    def _handle_ack(self, message: dict) -> None:
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        trace = message.get("trace") or payload.get("trace")
        trace = trace if isinstance(trace, str) else None
        err = message.get("errNo", payload.get("errNo"))
        command = None
        if trace in self._pending:
            command = self._pending.pop(trace)
        elif trace:
            # The cloud also echoes responses to commands from the Evo app or
            # Alice. Counting one for our pending command would hide a lost one.
            _LOGGER.debug("command_response for a foreign command, errNo=%s", err)
            return
        else:
            # No trace in the answer: count it for the oldest command to that unit.
            mac = str(message.get("macAddress") or "").lower()
            for pending_trace, pending in self._pending.items():
                if str(pending.get("macAddress") or "").lower() == mac:
                    command = self._pending.pop(pending_trace)
                    break
        if err not in (None, 0, "0"):
            _LOGGER.warning("Haier Evo rejected a command, errNo=%s", err)
            # The entity already shows the requested value: read the real one back.
            mac = message.get("macAddress") or (command or {}).get("macAddress") or ""
            if (device := self.get_device_by_id(mac)) is not None:
                self._create_task(self._async_resync([device]), "resync")

    async def _async_ack_watchdog(
        self, trace: str, payload: dict, sent_ws: ClientWebSocketResponse
    ) -> None:
        """No command_response in time: re-send once on a fresh connection.

        Catches the session that accepts writes but no longer delivers them,
        which a heartbeat cannot see (upstream #89, idea from the JampireX fork).
        Commands are absolute values, so a duplicate is harmless.
        """
        await asyncio.sleep(C.WS_ACK_TIMEOUT)
        if trace not in self._pending:
            return
        _LOGGER.warning("Haier Evo did not confirm a command in %s s, re-sending it", C.WS_ACK_TIMEOUT)
        # Only the socket the command went out on is suspect: commands sent
        # together must not tear down the fresh connection one after another.
        if sent_ws is self._ws and not sent_ws.closed:
            self._connected.clear()
            await sent_ws.close()
        try:
            await self.async_send(payload, retry=True)
        except HomeAssistantError:
            pass
        else:
            await asyncio.sleep(C.WS_ACK_TIMEOUT)
        if self._pending.pop(trace, None) is None:
            return
        self.lost_commands += 1
        device = self.get_device_by_id(payload.get("macAddress") or "")
        _LOGGER.error(
            "Haier Evo did not confirm a command for %s, reading the real state back",
            device.device_name if device else "unknown device",
        )
        if device is not None:
            await self._async_resync([device])

    async def _async_wait_connected(self, wait: float) -> ClientWebSocketResponse | None:
        if self.connected:
            return self._ws
        self._wakeup.set()
        try:
            await asyncio.wait_for(self._connected.wait(), wait)
        except TimeoutError:
            return None
        return self._ws if self.connected else None

    async def async_send(self, payload: dict, *, retry: bool = False) -> None:
        """Send a command, reconnecting first if needed (upstream #89, #102).

        The old client wrote into a socket that the server had already closed
        and silently dropped the command. Here a closed socket is detected by
        the heartbeat, the command waits for the reconnect, and a failed write
        is retried once on a fresh connection. If it still fails, the service
        call fails loudly instead of pretending it worked.
        """
        data = json.dumps(payload)
        trace = payload.get("trace") if not retry and self._entry is not None else None
        for _attempt in range(2):
            ws = await self._async_wait_connected(C.WS_SEND_TIMEOUT)
            if ws is None:
                break
            if trace:
                # Registered before the write: the answer may beat send_str.
                self._pending[trace] = payload
            try:
                await ws.send_str(data)
            except (ClientError, ConnectionError, RuntimeError) as err:
                _LOGGER.warning("Sending failed (%s), reconnecting", type(err).__name__)
                if trace:
                    self._pending.pop(trace, None)
                self._connected.clear()
                with suppress(Exception):
                    await ws.close()
                continue
            except BaseException:
                # Cancelled mid-write: no watchdog will ever clear the entry.
                if trace:
                    self._pending.pop(trace, None)
                raise
            _LOGGER.debug("Sent message: %s", data)
            if trace:
                self._create_task(self._async_ack_watchdog(trace, payload, ws), "ack")
            return
        raise HaierCommandError(translation_domain=C.DOMAIN, translation_key="command_failed")


class HaierDevice:

    def __init__(
        self,
        haier: Haier,
        device_mac: str,
        device_serial: str | None = None,
        device_title: str | None = None,
        backend_data: dict | None = None,
    ) -> None:
        self._haier = haier
        self.device_id = device_mac
        self.device_serial = device_serial
        self.device_name = device_title
        self.device_model = "UNKNOWN"
        self.sw_version = None
        self._listeners: list[Callable[[], None]] = []
        self._online = True
        self._config = None
        self._status_data = backend_data
        self.alarms: list[str] = []
        # Last push or command (monotonic): a REST snapshot older than this is stale.
        self.changed_at = 0.0

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self.device_name!r},"
            f"model={self.device_model!r},"
            f"config={self.config!r}"
            f")"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(C.DOMAIN, self.device_id)},
            name=self.device_name,
            sw_version=self.sw_version,
            model=self.device_model,
            manufacturer="Haier",
        )

    @property
    def device_mac(self) -> str:
        return self.device_id

    @property
    def available(self) -> bool:
        """Device reported ONLINE and the cloud connection is alive."""
        return self._online and self._haier.cloud_available

    @available.setter
    def available(self, value: bool | str) -> None:
        if isinstance(value, bool):
            self._online = value
        else:
            self._online = str(value).upper() != "OFFLINE"

    @property
    def status_data(self) -> dict:
        return self._status_data

    @property
    def hass(self) -> HomeAssistant:
        return self._haier.hass

    @property
    def config(self) -> CFG.HaierDeviceConfig:
        return self._config

    @property
    def constraint(self) -> CFG.Constraint:
        return self.config.constraint

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "device_id": self.device_id,
            "device_mac": self.device_mac,
            "device_name": self.device_name,
            "device_serial": self.device_serial,
            "device_model": self.device_model,
            "sw_version": self.sw_version,
            "config": self.config.to_dict() if self.config else None,
            "alarms": self.alarms,
            "backend_data": self.status_data,
        }

    def _set_alarms(self, alarms: Any) -> None:
        # Live units send an empty list; entries are taken as alarm names
        # ("indoorFanErr") or dicts carrying one, since no live alarm was seen yet.
        names = []
        for alarm in alarms if isinstance(alarms, list) else []:
            if isinstance(alarm, dict):
                alarm = alarm.get("name") or alarm.get("alarmName") or alarm.get("code")
            if alarm:
                names.append(str(alarm))
        self.alarms = names

    @property
    def alarm_texts(self) -> list[str]:
        """Human-readable alarms from the cloud's own table (errors.alarmToResult)."""
        errors = (self.status_data or {}).get("errors") or {}
        table = errors.get("alarmToResult") or {}
        unknown = (errors.get("undefinedAlarm") or {}).get("text") or "Unknown error"
        return [
            (table.get(alarm) or {}).get("text") or f"{unknown} ({alarm})"
            for alarm in self.alarms
        ]

    def _get_status(self, data: dict) -> dict:
        self._status_data = data = (data or {})
        info = data.setdefault("info", {})
        self.device_serial = info.setdefault("serialNumber", self.device_serial)
        device_model = info.get("model") or "AC"
        device_model = device_model.replace('-', '').replace('/', '')[:11]
        self.device_model = device_model
        self.available = data.setdefault("status", "ONLINE")
        settings = data.setdefault("settings", {})
        self.device_name = (settings.get("name") or {}).get("name") or self.device_name
        self.sw_version = (settings.get("firmware") or {}).get("value")
        self._set_alarms(data.get("alarms"))
        # read config and current values
        self._load_config_from_attributes(data)
        return data

    def update_from_backend(self, data: dict) -> None:
        """Apply a fresh REST status without rebuilding the config."""
        if not isinstance(data, dict) or self.config is None:
            return
        self._status_data = data
        self.available = data.get("status") or "ONLINE"
        self._set_alarms(data.get("alarms"))
        for attr in data.get("attributes") or []:
            if isinstance(attr, dict) and attr.get("currentValue") is not None:
                self._set_attribute_value(str(attr.get("name")), attr.get("currentValue"))

    def _load_config_from_attributes(self, data: dict) -> None:
        pass

    def _set_attribute_value(self, code: str, value: str) -> None:
        pass

    def _handle_status_update(self, received_message: dict) -> None:
        statuses = (received_message.get("payload") or {}).get("statuses") or []
        self.changed_at = time.monotonic()
        try:
            for status in statuses:
                if not isinstance(status, dict):
                    continue
                properties = status.get("properties")
                for key, value in (properties if isinstance(properties, dict) else {}).items():
                    self._set_attribute_value(key, value)
                if "alarms" in status:
                    self._set_alarms(status["alarms"])
            self.available = True
        finally:
            # Whatever was applied before a malformed entry still reaches the UI.
            self.notify()

    def _handle_device_status_update(self, received_message: dict) -> None:
        status = (received_message.get("payload") or {}).get("status")
        if status is not None:
            self.available = status
        self.notify()

    def _handle_info(self, received_message: dict) -> None:
        payload = received_message.get("payload") or {}
        self.sw_version = payload.get("swVersion") or self.sw_version

    async def _send_message(self, message: dict) -> None:
        await self._haier.async_send(message)
        self.changed_at = time.monotonic()

    async def _send_commands(self, commands: list[dict]) -> None:
        await self._send_group_command(commands)

    async def _send_group_command(self, commands: list[dict]) -> None:
        if not commands:
            return
        if not self.config.command_name:
            for command in commands:
                await self._send_single_command(command)
            return
        await self._send_message({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self.config.command_name,
            "commands": commands,
            "trace": str(uuid.uuid4()),
        })

    async def _send_single_command(self, command: dict) -> None:
        await self._send_message({
            "action": "command",
            "macAddress": self.device_id,
            "command": command,
            "trace": str(uuid.uuid4()),
        })

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        value = str({True: "on", False: "off", None: "off"}.get(value, value))
        if custom := self.config.get_command_by_name(f"{name}_{value}"):
            return custom
        attr = self.config.get_attr_by_name(name)
        code = attr.get_item_code(value) if attr else None
        if code is None:
            # Used to go out as the string "None".
            _LOGGER.warning("%s: no command for %s=%s", self.device_name, name, value)
            return []
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": code,
        }])

    def on_message(self, message_dict: dict) -> None:
        message_type = message_dict.get("event", "")
        if message_type == "status":
            self._handle_status_update(message_dict)
        elif message_type == "command_response":
            pass
        elif message_type == "info":
            self._handle_info(message_dict)
        elif message_type == "deviceStatusEvent":
            self._handle_device_status_update(message_dict)
        else:
            _LOGGER.debug("Unknown message type %r", message_type)

    def notify(self) -> None:
        """Push the current state to entities. Event loop only."""
        for listener in list(self._listeners):
            listener()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # noinspection PyMethodMayBeStatic
    def create_entities_climate(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_switch(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_select(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_sensor(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_binary_sensor(self) -> list:
        return []

    @classmethod
    def create(
        cls,
        haier: Haier,
        device_type: str,
        device_mac: str,
        device_serial: str | None = None,
        device_title: str | None = None,
        backend_data: dict | None = None,
    ) -> HaierDevice:
        device_cls = {
            "AC": HaierAC,
            "REF": HaierREF,
            "WM": HaierWM,
        }.get(device_type, cls)
        if device_cls is cls:
            _LOGGER.warning("Unknown device type: %s", device_type)
        return device_cls(
            haier=haier,
            device_mac=device_mac,
            device_serial=device_serial,
            device_title=device_title,
            backend_data=backend_data,
        )


class HaierAC(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_temperature: float | None = None
        self.outdoor_temperature: float | None = None
        self.target_temperature: float | None = None
        self.target_temperature_step: float = 1.0
        self.status = None
        self.mode = None
        self.fan_mode = None
        self.swing_horizontal_mode = None
        self.swing_mode = None
        self._preset_mode = None
        self.min_temperature = 7
        self.max_temperature = 35
        self.light_on = True
        self.sound_on = True
        self.quiet_on = False
        self.turbo_on = False
        self.health_on = False
        self.comfort_on = False
        self.cleaning_on = False
        self.antifreeze_on = False
        self.autohumidity_on = False
        self.eco_sensor = None
        # Raw attribute values by code, as the cloud sends them: modifiers are
        # written against these, not against our parsed state.
        self._raw: dict[str, str] = {}
        # Set on an off unit: the cloud refuses it until the unit runs.
        self._deferred_temperature: float | None = None
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierACConfig:
        return self._config

    @property
    def preset_mode(self) -> str:
        if self._preset_mode not in ("none", "sleep", "boost"):
            return self._preset_mode
        elif self.quiet_on:
            return "sleep"
        elif self.turbo_on:
            return "boost"
        return "none"

    @preset_mode.setter
    def preset_mode(self, preset_mode: str) -> None:
        self._preset_mode = preset_mode

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_temperature": self.current_temperature,
            "outdoor_temperature": self.outdoor_temperature,
            "target_temperature": self.target_temperature,
            "max_temperature": self.max_temperature,
            "min_temperature": self.min_temperature,
            "status": self.status,
            "mode": self.mode,
            "fan_mode": self.fan_mode,
            "swing_horizontal_mode": self.swing_horizontal_mode,
            "swing_mode": self.swing_mode,
            "preset_mode": self.preset_mode,
            "light_on": self.light_on,
            "sound_on": self.sound_on,
            "quiet_on": self.quiet_on,
            "turbo_on": self.turbo_on,
            "health_on": self.health_on,
            "comfort_on": self.comfort_on,
            "cleaning_on": self.cleaning_on,
            "antifreeze_on": self.antifreeze_on,
            "autohumidity_on": self.autohumidity_on,
            "eco_sensor": self.eco_sensor,
        })
        return data

    def _set_attribute_value(self, code: str, value: str) -> None:
        # "Тихий"/"Турбо" exist twice with one code (preset_mode_sleep + quiet,
        # preset_mode_boost + turbo). Only the first one used to be applied, so
        # the switches never followed the unit (upstream #67).
        if value is None:
            return
        self._raw[str(code)] = str(value)
        if self._deferred_temperature is not None and (
            (str(code) == str(self.config["status"]) and _norm(value) == "1")
            or str(code) == str(self.config["target_temperature"])
        ):
            # Turned on elsewhere, or the cloud reports its own setpoint: the
            # value kept for our next turn-on is stale and must not pop up later.
            self._deferred_temperature = None
        for attr in self.config.get_attrs_by_code(code):
            self._apply_attribute(attr, value)

    def _apply_attribute(self, attr: CFG.Attribute, value: str) -> None:
        if attr.name == "current_temperature":
            self.current_temperature = parsefloat(value, self.current_temperature)
        elif attr.name == "outdoor_temperature":
            self.outdoor_temperature = parsefloat(value, self.outdoor_temperature)
        elif attr.name == "status":
            self.status = 1 if parsebool(str(value)) else 0
        elif attr.name == "target_temperature":
            self.target_temperature = parsefloat(value, self.target_temperature)
        elif attr.name == "mode":
            self.mode = attr.get_item_name(value)
        elif attr.name == "fan_mode":
            self.fan_mode = attr.get_item_name(value)
        elif attr.name == "swing_horizontal_mode":
            self.swing_horizontal_mode = attr.get_item_name(value)
        elif attr.name == "swing_mode":
            self.swing_mode = attr.get_item_name(value)
        elif attr.name == "light":
            self.light_on = parsebool(attr.get_item_name(value))
        elif attr.name == "sound":
            self.sound_on = parsebool(attr.get_item_name(value))
        elif attr.name in ("quiet", "preset_mode_sleep"):
            self.quiet_on = parsebool(attr.get_item_name(value))
        elif attr.name in ("turbo", "preset_mode_boost"):
            self.turbo_on = parsebool(attr.get_item_name(value))
        elif attr.name == "health":
            self.health_on = parsebool(attr.get_item_name(value))
        elif attr.name == "comfort":
            self.comfort_on = parsebool(attr.get_item_name(value))
        elif attr.name == "cleaning":
            self.cleaning_on = parsebool(attr.get_item_name(value))
        elif attr.name == "antifreeze":
            self.antifreeze_on = parsebool(attr.get_item_name(value))
        elif attr.name == "autohumidity":
            self.autohumidity_on = parsebool(attr.get_item_name(value))
        elif attr.name == "eco_sensor":
            self.eco_sensor = attr.get_item_name(value)

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierACConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        sensors = data.setdefault("sensors", {}).get("items", [])
        sensor_curr_temp = next(filter(lambda i: (
            isinstance(i, dict)
            and isinstance(i.get("value"), dict)
            and i.get("value", {}).get("description") == "indoorTemperature"
        ), sensors), {}).get("value", {}).get("name")
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            if attr.name == "current_temperature" and str(attr.code) != sensor_curr_temp:
                if sensor_curr_temp is None:
                    continue
                # The second "room temperature" (code 38 on S2SF and PHP3) follows
                # the outdoor unit: 11-13 °C on idle units at 8-10 °C outside,
                # warmer while the unit runs. The indoor one is named in sensors.
                attr = CFG.Attribute({**attr, "attrname": "outdoor_temperature"})
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            if attr.name == "target_temperature" and attr.range:
                self.min_temperature = parsefloat(attr.range.min_value, self.min_temperature)
                self.max_temperature = parsefloat(attr.range.max_value, self.max_temperature)
                self.target_temperature_step = parsefloat(attr.range.step, 1.0) or 1.0
            _LOGGER.debug("%s: %s", self.device_name, attr)
        self.constraint.extend(data.setdefault("constraint", []))

    def _get_status(self, data: dict) -> dict:
        data = super()._get_status(data)
        if self.swing_horizontal_mode is None:
            self.swing_horizontal_mode = SWING_OFF
        if self.swing_mode is None:
            self.swing_mode = SWING_OFF
        if self.preset_mode is None:
            self.preset_mode = PRESET_NONE
        return data

    def _modifier_locks(self) -> tuple[dict[str, str], dict[str, set[str]]]:
        """Read-only codes (code -> reason) and value whitelists right now.

        The cloud ships "modifiers": rules that make attributes read-only while
        the unit is off, cleaning, in a given mode or reporting an alarm. The
        unit silently ignores such commands, so they are refused up front.
        """
        locked: dict[str, str] = {}
        allowed: dict[str, set[str]] = {}
        # Rules go by priority, and the first firing rule that rewrites a field
        # decides it, "writable": true included (live units use priority 0 for
        # the "unit is off" rule, which has to win).
        decided: set[str] = set()
        modifiers = (self.status_data or {}).get("modifiers") or []
        for rule in sorted(
            (r for r in modifiers if isinstance(r, dict)),
            key=lambda r: r.get("priority") or 0,
        ):
            reason = self._modifier_reason(rule.get("trigger") or {})
            if reason is None:
                continue
            for action in rule.get("actions") or []:
                code = str(action.get("name"))
                fields = str(action.get("rewriteFields") or "")
                if "W" in fields and code not in decided:
                    decided.add(code)
                    if action.get("writable") is False:
                        locked[code] = reason
                if "V" in fields and code not in allowed:
                    data = ((action.get("valueRange") or {}).get("list") or {}).get("data") or []
                    if values := {_norm(i.get("data")) for i in data if isinstance(i, dict)}:
                        allowed[code] = values
        return locked, allowed

    def _modifier_reason(self, trigger: dict) -> str | None:
        """Why a rule fires ("off", "mode", "alarm"), or None if it does not."""
        status_code = str(self.config["status"])
        results = []
        for cond in (trigger.get("conditions") or {}).get("condition") or []:
            code = str(cond.get("mode"))
            values = {_norm(v) for v in cond.get("values") or []}
            matched = code in self._raw and _norm(self._raw[code]) in values
            results.append((matched, "off" if code == status_code else "mode"))
        if alarms := set(trigger.get("alarms") or []):
            results.append((bool(alarms & set(self.alarms)), "alarm"))
        if not results:
            return None
        if str(trigger.get("operator") or "AND").upper() == "OR":
            return next((reason for matched, reason in results if matched), None)
        if all(matched for matched, _ in results):
            return results[0][1]
        return None

    def _check_writable(self, name: str, value: str | None = None) -> None:
        attr = self.config.get_attr_by_name(name)
        if attr is None:
            return
        code = str(attr.code)
        locked, allowed = self._modifier_locks()
        placeholders = {
            "device": str(self.device_name),
            "attribute": attr.description or name,
        }
        if code in locked:
            raise HaierCommandLocked(
                translation_domain=C.DOMAIN,
                translation_key=f"locked_{locked[code]}",
                translation_placeholders=placeholders,
            )
        if value is not None and code in allowed:
            item = attr.get_item_code(str(value))
            if item is not None and _norm(item) not in allowed[code]:
                raise HaierCommandLocked(
                    translation_domain=C.DOMAIN,
                    translation_key="value_not_allowed",
                    translation_placeholders={**placeholders, "value": str(value)},
                )

    async def _send_commands(self, commands: list[dict]) -> None:
        await super()._send_commands(commands)
        # Keep modifiers honest until the unit echoes the new state.
        for command in commands:
            if command.get("commandName") is not None:
                self._raw[str(command["commandName"])] = str(command.get("value"))

    def _status_commands(self, value: str) -> list[dict]:
        if custom := self.config.get_command_by_name(f"status_{value}"):
            return custom
        attr = self.config.get_attr_by_name("status")
        if attr is None:
            return []
        # Profiles without a status mapping: on/off is 1/0 on every known unit.
        code = attr.get_item_code(value) or ("1" if value == "on" else "0")
        return self.constraint.apply([{"commandName": str(attr.code), "value": code}])

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        if name != "preset_mode":
            return super().get_commands(name, value)
        func = getattr(self, f"get_preset_mode_{value}", None)
        if func is not None:
            return func()
        return self.get_preset_mode_command(value)

    def get_preset_mode_none(self) -> list[dict]:
        if custom := self.config.get_command_by_name('preset_mode_none'):
            return custom
        return [{
            "commandName": str(attr.code),
            "value": attr.get_item_code("off", "0"),
        } for attr in filter(
            lambda a: a.name.startswith("preset_mode"),
            self.config.attrs
        )]

    def get_preset_mode_command(self, mode: str) -> list[dict]:
        if custom := self.config.get_command_by_name(f'preset_mode_{mode}'):
            return custom
        attr = self.config.get_attr_by_name(f"preset_mode_{mode}")
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": attr.get_item_code("on", "1")
        }] if attr else [])

    def get_supported_features(self) -> ClimateEntityFeature:
        value = (
            ClimateEntityFeature.TARGET_TEMPERATURE |
            ClimateEntityFeature.TURN_OFF |
            ClimateEntityFeature.TURN_ON |
            ClimateEntityFeature.FAN_MODE
        )
        if self.config['swing_horizontal_mode'] is not None:
            value = value | ClimateEntityFeature.SWING_HORIZONTAL_MODE
        if self.config['swing_mode'] is not None:
            value = value | ClimateEntityFeature.SWING_MODE
        if self.config.preset_mode is True:
            value = value | ClimateEntityFeature.PRESET_MODE
        return ClimateEntityFeature(value)

    def get_hvac_modes(self) -> list[HVACMode]:
        modes = []
        for mode in self.config.get_values('mode'):
            try:
                modes.append(HVACMode(mode))
            except ValueError:
                pass
        return modes + [HVACMode.OFF]

    def get_fan_modes(self) -> list[str]:
        return self.config.get_values('fan_mode')

    def get_swing_horizontal_modes(self) -> list[str]:
        return self.config.get_values('swing_horizontal_mode')

    def get_swing_modes(self) -> list[str]:
        # "special" (code 9) is "только для специальной модели" per the cloud.
        return [m for m in self.config.get_values('swing_mode') if m != "special"]

    def get_preset_modes(self) -> list[str]:
        return ["none"] + self.config.get_preset_modes()

    def get_eco_sensor_options(self) -> list[str]:
        return self.config.get_values('eco_sensor')

    async def set_temperature(self, value: float) -> None:
        code = self.config['target_temperature']
        if code is None:
            raise HomeAssistantError(f"{self.device_name}: target temperature is not supported")
        try:
            self._check_writable("target_temperature")
        except HaierCommandLocked as err:
            if err.translation_key != "locked_off":
                raise
            # Alice and thermostat cards set the temperature of an off unit
            # ("включи на 24" may come temperature first). Keep it for switch_on.
            self._deferred_temperature = float(value)
            self.target_temperature = float(value)
            return
        await self._send_commands([
            {
                "commandName": code,
                "value": format_number(value),
            }
        ])
        self._deferred_temperature = None
        self.target_temperature = float(value)

    async def switch_on(self, value: str = None) -> None:
        value = str(value or self.mode or HVACMode.AUTO)
        if self.status and value != self.mode:
            # Turning on is never locked; a mode change on a running unit may be.
            self._check_writable("mode", value)
        # Always send status=on: our idea of "already on" may be stale, and
        # then a mode change alone does not start the unit (MaxSezin fork).
        await self._send_commands([
            *self._status_commands("on"),
            *self.get_commands("mode", value),
        ])
        self.status = 1
        self.mode = value
        if (deferred := self._deferred_temperature) is not None:
            self._deferred_temperature = None
            try:
                await self.set_temperature(deferred)
            except HaierCommandLocked as err:
                # The new mode does not take a setpoint (e.g. fan only).
                _LOGGER.debug("Dropping the deferred temperature: %s", err.translation_key)
            except HaierCommandError:
                # The unit is on already: report the setpoint, do not fail the turn-on.
                _LOGGER.warning("%s: turned on, but the temperature could not be sent", self.device_name)

    async def switch_off(self) -> None:
        await self._send_commands([
            *self._status_commands("off"),
        ])
        self.status = 0

    async def set_fan_mode(self, value: str) -> None:
        self._check_writable("fan_mode", value)
        if commands := self.get_commands("fan_mode", value):
            await self._send_commands(commands)
            self.fan_mode = value

    async def set_swing_horizontal_mode(self, value: str) -> None:
        self._check_writable("swing_horizontal_mode", value)
        if commands := self.get_commands("swing_horizontal_mode", value):
            await self._send_commands(commands)
            self.swing_horizontal_mode = value

    async def set_swing_mode(self, value: str) -> None:
        self._check_writable("swing_mode", value)
        if commands := self.get_commands("swing_mode", value):
            await self._send_commands(commands)
            self.swing_mode = value

    async def set_preset_mode(self, value: str) -> None:
        if value != PRESET_NONE:
            self._check_writable(f"preset_mode_{value}")
        if commands := self.get_commands("preset_mode", value):
            await self._send_commands(commands)
            self.preset_mode = value
            if value in (PRESET_NONE, "sleep", "boost"):
                # The getter reads these two until the unit echoes them.
                self.quiet_on = value == "sleep"
                self.turbo_on = value == "boost"

    async def set_light_on(self, value: bool) -> None:
        self._check_writable("light")
        if commands := self.get_commands("light", value):
            await self._send_commands(commands)
            self.light_on = value

    async def set_sound_on(self, value: bool) -> None:
        self._check_writable("sound")
        if commands := self.get_commands("sound", value):
            await self._send_commands(commands)
            self.sound_on = value

    async def set_quiet_on(self, value: bool) -> None:
        self._check_writable("quiet")
        if commands := self.get_commands("quiet", value):
            await self._send_commands(commands)
            self.quiet_on = value

    async def set_health_on(self, value: bool) -> None:
        self._check_writable("health")
        if commands := self.get_commands("health", value):
            await self._send_commands(commands)
            self.health_on = value

    async def set_turbo_on(self, value: bool) -> None:
        self._check_writable("turbo")
        if commands := self.get_commands("turbo", value):
            await self._send_commands(commands)
            self.turbo_on = value

    async def set_comfort_on(self, value: bool) -> None:
        self._check_writable("comfort")
        if commands := self.get_commands("comfort", value):
            await self._send_commands(commands)
            self.comfort_on = value

    async def set_cleaning_on(self, value: bool) -> None:
        self._check_writable("cleaning")
        if commands := self.get_commands("cleaning", value):
            await self._send_commands(commands)
            self.cleaning_on = value

    async def set_antifreeze_on(self, value: bool) -> None:
        self._check_writable("antifreeze")
        if commands := self.get_commands("antifreeze", value):
            await self._send_commands(commands)
            self.antifreeze_on = value

    async def set_autohumidity_on(self, value: bool) -> None:
        self._check_writable("autohumidity")
        if commands := self.get_commands("autohumidity", value):
            await self._send_commands(commands)
            self.autohumidity_on = value

    async def set_eco_sensor(self, value: str) -> None:
        self._check_writable("eco_sensor", value)
        if commands := self.get_commands("eco_sensor", value):
            await self._send_commands(commands)
            self.eco_sensor = value

    def create_entities_climate(self) -> list:
        from . import climate
        return [climate.HaierACEntity(self)]

    def create_entities_switch(self) -> list:
        from . import switch
        entities = []
        if self.config['light'] is not None:
            entities.append(switch.HaierACLightSwitch(self))
        if self.config['sound'] is not None:
            entities.append(switch.HaierACSoundSwitch(self))
        if self.config['quiet'] is not None:
            entities.append(switch.HaierACQuietSwitch(self))
        if self.config['turbo'] is not None:
            entities.append(switch.HaierACTurboSwitch(self))
        if self.config['health'] is not None:
            entities.append(switch.HaierACHealthSwitch(self))
        if self.config['comfort'] is not None:
            entities.append(switch.HaierACComfortSwitch(self))
        if self.config['cleaning'] is not None:
            entities.append(switch.HaierACCleaningSwitch(self))
        if self.config['antifreeze'] is not None:
            entities.append(switch.HaierACAntiFreezeSwitch(self))
        if self.config['autohumidity'] is not None:
            entities.append(switch.HaierACAutoHumiditySwitch(self))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['eco_sensor'] is not None:
            entities.append(select.HaierACEcoSensorSelect(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['outdoor_temperature'] is not None:
            entities.append(sensor.HaierACOutdoorTemperatureSensor(self))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        return [binary_sensor.HaierACProblemSensor(self)]


class HaierREF(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_fridge_temperature: float | None = None
        self.current_freezer_temperature: float | None = None
        self.current_temperature: float | None = None
        self.fridge_mode = None
        self.freezer_mode = None
        self.my_zone = None
        self.super_cooling = False
        self.super_freeze = False
        self.vacation_mode = False
        self.door_open = False
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierREFConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_fridge_temperature": self.current_fridge_temperature,
            "current_freezer_temperature": self.current_freezer_temperature,
            "current_temperature": self.current_temperature,
            "fridge_mode": self.fridge_mode,
            "freezer_mode": self.freezer_mode,
            "my_zone": self.my_zone,
            "super_cooling": self.super_cooling,
            "super_freeze": self.super_freeze,
            "vacation_mode": self.vacation_mode,
            "door_open": self.door_open,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierREFConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            _LOGGER.debug("%s: %s", self.device_name, attr)

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "current_fridge_temperature":
            self.current_fridge_temperature = parsefloat(value)
        elif attr.name == "current_freezer_temperature":
            self.current_freezer_temperature = parsefloat(value)
        elif attr.name == "current_temperature":
            self.current_temperature = parsefloat(value)
        elif attr.name == "fridge_mode":
            self.fridge_mode = attr.get_item_name(value)
        elif attr.name == "freezer_mode":
            self.freezer_mode = attr.get_item_name(value)
        elif attr.name == "my_zone":
            self.my_zone = attr.get_item_name(value)
        elif attr.name == "super_cooling":
            self.super_cooling = parsebool(attr.get_item_name(value))
        elif attr.name == "super_freeze":
            self.super_freeze = parsebool(attr.get_item_name(value))
        elif attr.name == "vacation_mode":
            self.vacation_mode = parsebool(attr.get_item_name(value))
        elif attr.name == "door_open":
            self.door_open = parsebool(attr.get_item_name(value))

    def get_fridge_mode_options(self) -> list[str]:
        return self.config.get_values('fridge_mode')

    def get_freezer_mode_options(self) -> list[str]:
        return self.config.get_values('freezer_mode')

    def get_my_zone_options(self) -> list[str]:
        return self.config.get_values('my_zone')

    async def set_super_cooling(self, value: bool) -> None:
        if commands := self.get_commands("super_cooling", value):
            await self._send_single_command(commands[0])
            self.super_cooling = value

    async def set_super_freeze(self, value: bool) -> None:
        if commands := self.get_commands("super_freeze", value):
            await self._send_single_command(commands[0])
            self.super_freeze = value

    async def set_vacation_mode(self, value: bool) -> None:
        if commands := self.get_commands("vacation_mode", value):
            await self._send_single_command(commands[0])
            self.vacation_mode = value

    async def set_fridge_mode(self, value: str) -> None:
        if commands := self.get_commands("fridge_mode", value):
            await self._send_single_command(commands[0])
            self.fridge_mode = value

    async def set_freezer_mode(self, value: str) -> None:
        if commands := self.get_commands("freezer_mode", value):
            await self._send_single_command(commands[0])
            self.freezer_mode = value

    async def set_my_zone(self, value: str) -> None:
        if commands := self.get_commands("my_zone", value):
            await self._send_single_command(commands[0])
            self.my_zone = value

    def create_entities_switch(self) -> list:
        from . import switch
        entities = []
        if self.config['super_cooling'] is not None:
            entities.append(switch.HaierREFSuperCoolingSwitch(self))
        if self.config['super_freeze'] is not None:
            entities.append(switch.HaierREFSuperFreezeSwitch(self))
        if self.config['vacation_mode'] is not None:
            entities.append(switch.HaierREFVacationSwitch(self))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['fridge_mode'] is not None:
            entities.append(select.HaierREFFridgeModeSelect(self))
        if self.config['freezer_mode'] is not None:
            entities.append(select.HaierREFFreezerModeSelect(self))
        if self.config['my_zone'] is not None:
            entities.append(select.HaierREFMyZoneSelect(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['current_temperature'] is not None:
            entities.append(sensor.HaierREFTemperatureSensor(self))
        if self.config['current_fridge_temperature'] is not None:
            entities.append(sensor.HaierREFFridgeTemperatureSensor(self))
        if self.config['current_freezer_temperature'] is not None:
            entities.append(sensor.HaierREFFreezerTemperatureSensor(self))
        if self.config['fridge_mode'] is not None:
            entities.append(sensor.HaierREFFridgeModeSensor(self))
        if self.config['freezer_mode'] is not None:
            entities.append(sensor.HaierREFFreezerModeSensor(self))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = []
        if self.config['super_cooling'] is not None:
            entities.append(binary_sensor.HaierREFSuperCoolingSensor(self))
        if self.config['super_freeze'] is not None:
            entities.append(binary_sensor.HaierREFSuperFreezeSensor(self))
        if self.config['vacation_mode'] is not None:
            entities.append(binary_sensor.HaierREFVacationSensor(self))
        if self.config['door_open'] is not None:
            entities.append(binary_sensor.HaierREFDoorSensor(self))
        return entities


class HaierWM(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.status = None
        self.program = None
        self.temperature = None
        self.spin_speed = None
        self.remaining_time = None
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierWMConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "status": self.status,
            "program": self.program,
            "temperature": self.temperature,
            "spin_speed": self.spin_speed,
            "remaining_time": self.remaining_time,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierWMConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            _LOGGER.debug("%s: %s", self.device_name, attr)

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "status":
            self.status = attr.get_item_name(value)
        elif attr.name == "program":
            self.program = attr.get_item_name(value)
        elif attr.name == "temperature":
            self.temperature = attr.get_item_name(value)
        elif attr.name == "spin_speed":
            self.spin_speed = attr.get_item_name(value)
        elif attr.name == "remaining_time":
            self.remaining_time = parsefloat(value)

    def get_program_options(self) -> list[str]:
        return self.config.get_values('program')

    def get_temperature_options(self) -> list[str]:
        return self.config.get_values('temperature')

    def get_spin_speed_options(self) -> list[str]:
        return self.config.get_values('spin_speed')

    async def set_program(self, value: str) -> None:
        if commands := self.get_commands("program", value):
            await self._send_single_command(commands[0])
            self.program = value

    async def set_temperature(self, value: str) -> None:
        if commands := self.get_commands("temperature", value):
            await self._send_single_command(commands[0])
            self.temperature = value

    async def set_spin_speed(self, value: str) -> None:
        if commands := self.get_commands("spin_speed", value):
            await self._send_single_command(commands[0])
            self.spin_speed = value

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['program'] is not None:
            entities.append(select.HaierWMProgramSelect(self))
        if self.config['temperature'] is not None:
            entities.append(select.HaierWMTemperatureSelect(self))
        if self.config['spin_speed'] is not None:
            entities.append(select.HaierWMSpinSpeedSelect(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['remaining_time'] is not None:
            entities.append(sensor.HaierWMRemainingTimeSensor(self))
        if self.config['status'] is not None:
            entities.append(sensor.HaierWMStatusSensor(self))
        return entities


def parsebool(value) -> bool:
    if value in ("on", 1, True, "true", "enable", "1"):
        return True
    return False


def _norm(value: Any) -> str:
    """Compare raw values: modifiers say "true", the unit reports "1"."""
    text = str(value).strip().lower()
    return {"true": "1", "on": "1", "false": "0", "off": "0"}.get(text, text)


def parsefloat(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_number(value: float | int | str) -> str:
    """24.0 -> "24", 24.5 -> "24.5" (the app sends integers as such)."""
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)
