"""Button entities — PTZ direction jog controls, alarm trigger/stop, and OpenAPI actions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import VIGIEntity
from .onvif_ptz import BUTTON_MOVE_S, DEFAULT_SPEED


# ── PTZ jog buttons ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PTZButtonDesc:
    key: str
    name: str
    icon: str
    pan: float
    tilt: float
    zoom: float


_PTZ_BUTTONS: tuple[PTZButtonDesc, ...] = (
    PTZButtonDesc("ptz_left",      "PTZ Pan Left",    "mdi:pan-left",      -DEFAULT_SPEED, 0.0,           0.0),
    PTZButtonDesc("ptz_right",     "PTZ Pan Right",   "mdi:pan-right",      DEFAULT_SPEED, 0.0,           0.0),
    PTZButtonDesc("ptz_up",        "PTZ Tilt Up",     "mdi:pan-up",         0.0,           DEFAULT_SPEED, 0.0),
    PTZButtonDesc("ptz_down",      "PTZ Tilt Down",   "mdi:pan-down",       0.0,          -DEFAULT_SPEED, 0.0),
    PTZButtonDesc("ptz_zoom_in",   "PTZ Zoom In",     "mdi:magnify-plus",   0.0,           0.0,           DEFAULT_SPEED),
    PTZButtonDesc("ptz_zoom_out",  "PTZ Zoom Out",    "mdi:magnify-minus",  0.0,           0.0,          -DEFAULT_SPEED),
)


# ── Alarm trigger/stop buttons ────────────────────────────────────────────────

@dataclass(frozen=True)
class AlarmButtonDesc:
    key: str
    name: str
    icon: str
    action: str  # "start" or "stop"
    supported_fn: Callable[[dict], bool] = field(default=lambda _: True)


_ALARM_BUTTONS: tuple[AlarmButtonDesc, ...] = (
    AlarmButtonDesc(
        "alarm_trigger", "Alarm Trigger", "mdi:alarm-light", "start",
        supported_fn=lambda d: bool(d.get("alarm")),
    ),
    AlarmButtonDesc(
        "alarm_stop", "Alarm Stop", "mdi:alarm-off", "stop",
        supported_fn=lambda d: bool(d.get("alarm")),
    ),
)


# ── Platform setup ────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    coord_data = coordinator.data or {}
    has_openapi = data.get("has_openapi", False)

    entities: list[ButtonEntity] = []

    if data.get("has_ptz") and data.get("onvif_ptz"):
        entities.extend(VIGIPTZButton(coordinator, data, desc) for desc in _PTZ_BUTTONS)

    entities.extend(
        VIGIAlarmButton(coordinator, data, desc)
        for desc in _ALARM_BUTTONS
        if desc.supported_fn(coord_data)
    )

    if has_openapi:
        entities.append(VIGISoftResetButton(coordinator, data))
        entities.append(VIGIFormatSDButton(coordinator, data))
        if data.get("has_ptz"):
            entities.append(VIGIPTZCruiseStartButton(coordinator, data))
            entities.append(VIGIPTZCruiseStopButton(coordinator, data))
            entities.append(VIGIPTZSavePresetButton(coordinator, data))
            entities.append(VIGIPTZDeletePresetButton(coordinator, data))

    if entities:
        async_add_entities(entities)


# ── Entity classes ────────────────────────────────────────────────────────────

class VIGIPTZButton(VIGIEntity, ButtonEntity):
    """Jogs the camera in one direction for BUTTON_MOVE_S seconds then stops."""

    def __init__(self, coordinator, entry_data, desc: PTZButtonDesc) -> None:
        super().__init__(coordinator, entry_data)
        self._desc = desc
        self._attr_name = desc.name
        self._attr_icon = desc.icon

    @property
    def _unique_id_suffix(self) -> str:
        return self._desc.key

    async def async_press(self) -> None:
        ptz = self._entry_data["onvif_ptz"]
        await ptz.continuous_move(self._desc.pan, self._desc.tilt, self._desc.zoom)
        await asyncio.sleep(BUTTON_MOVE_S)
        await ptz.stop()


class VIGIAlarmButton(VIGIEntity, ButtonEntity):
    """Triggers or cancels the manual alarm sound (10 s countdown, camera auto-stops)."""

    def __init__(self, coordinator, entry_data, desc: AlarmButtonDesc) -> None:
        super().__init__(coordinator, entry_data)
        self._desc = desc
        self._attr_name = desc.name
        self._attr_icon = desc.icon

    @property
    def _unique_id_suffix(self) -> str:
        return self._desc.key

    async def async_press(self) -> None:
        api = self._entry_data["api"]
        if self._desc.action == "start":
            await api.trigger_alarm()
        else:
            await api.stop_alarm()


# ── OpenAPI buttons ───────────────────────────────────────────────────────────

class VIGISoftResetButton(VIGIEntity, ButtonEntity):
    """Reboot the camera (soft reset — config is preserved)."""

    _attr_name = "Soft Reset"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def _unique_id_suffix(self) -> str:
        return "soft_reset"

    async def async_press(self) -> None:
        openapi = self._entry_data.get("openapi")
        if openapi:
            await openapi.do_soft_reset()


class VIGIFormatSDButton(VIGIEntity, ButtonEntity):
    """Format the SD card — DESTRUCTIVE, all recordings will be deleted."""

    _attr_name = "Format SD Card"
    _attr_icon = "mdi:sd"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def _unique_id_suffix(self) -> str:
        return "format_sd_card"

    @property
    def available(self) -> bool:
        return self.coordinator.has_sd_card

    async def async_press(self) -> None:
        openapi = self._entry_data.get("openapi")
        if openapi:
            await openapi.format_sd_card()
            await self.coordinator.async_request_refresh()


class VIGIPTZCruiseStartButton(VIGIEntity, ButtonEntity):
    """Start the PTZ cruise (auto-patrol) route."""

    _attr_name = "PTZ Cruise Start"
    _attr_icon = "mdi:ship-wheel"

    @property
    def _unique_id_suffix(self) -> str:
        return "ptz_cruise_start"

    async def async_press(self) -> None:
        openapi = self._entry_data.get("openapi")
        if openapi:
            await openapi.cruise_move(action="start")


class VIGIPTZCruiseStopButton(VIGIEntity, ButtonEntity):
    """Stop the PTZ cruise route."""

    _attr_name = "PTZ Cruise Stop"
    _attr_icon = "mdi:stop-circle"

    @property
    def _unique_id_suffix(self) -> str:
        return "ptz_cruise_stop"

    async def async_press(self) -> None:
        openapi = self._entry_data.get("openapi")
        if openapi:
            await openapi.cruise_move(action="stop")


class VIGIPTZSavePresetButton(VIGIEntity, ButtonEntity):
    """Save current PTZ position as the currently-selected preset slot."""

    _attr_name = "PTZ Save Preset"
    _attr_icon = "mdi:content-save"

    @property
    def _unique_id_suffix(self) -> str:
        return "ptz_save_preset"

    async def async_press(self) -> None:
        openapi = self._entry_data.get("openapi")
        coordinator = self.coordinator
        if openapi and coordinator.last_preset:
            # Find the id for the currently-selected preset name
            preset = next(
                (p for p in coordinator.presets if p["name"] == coordinator.last_preset),
                None,
            )
            if preset:
                await openapi.set_preset_point(preset["id"], preset["name"])


class VIGIPTZDeletePresetButton(VIGIEntity, ButtonEntity):
    """Delete the currently-selected PTZ preset slot."""

    _attr_name = "PTZ Delete Preset"
    _attr_icon = "mdi:delete"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def _unique_id_suffix(self) -> str:
        return "ptz_delete_preset"

    async def async_press(self) -> None:
        openapi = self._entry_data.get("openapi")
        coordinator = self.coordinator
        if openapi and coordinator.last_preset:
            preset = next(
                (p for p in coordinator.presets if p["name"] == coordinator.last_preset),
                None,
            )
            if preset:
                await openapi.remove_preset_point(preset["id"])
                coordinator.presets = []  # force refresh
                await coordinator.async_request_refresh()
