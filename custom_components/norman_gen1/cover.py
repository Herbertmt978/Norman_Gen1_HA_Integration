from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable
from typing import Any

from homeassistant.components.cover import ATTR_POSITION, CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NormanDataUpdateCoordinator
from .api import CannotConnect, CannotControl, InvalidAuth, NormanGen1Api, NormanRoom, NormanWindow
from .const import COMMAND_SETTLE_SECONDS, DATA_API, DATA_COORDINATOR, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _average_position(windows: Iterable[NormanWindow]) -> int | None:
    positions = [window.position for window in windows if window.position is not None]
    if not positions:
        return None
    return round(sum(positions) / len(positions))


def _sanitize_name(value: str) -> str:
    return " ".join(value.split()) or "Unnamed"


def _group_name(room: NormanRoom, level: int) -> str:
    if 0 <= level < len(room.group_names):
        return room.group_names[level]
    if 1 <= level <= len(room.group_names):
        return room.group_names[level - 1]
    return f"Group {level + 1}" if level >= 0 else "Group"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api: NormanGen1Api = data[DATA_API]
    coordinator: NormanDataUpdateCoordinator = data[DATA_COORDINATOR]

    entities: list[CoverEntity] = []
    rooms: list[NormanRoom] = coordinator.data["rooms"]
    levels_by_room: dict[int, list[int]] = coordinator.data["levels_by_room"]

    for room in rooms:
        entities.append(NormanRoomCover(api, coordinator, room))
        for level in levels_by_room.get(room.id, []):
            entities.append(NormanGroupCover(api, coordinator, room, level, _group_name(room, level)))

    async_add_entities(entities)


class NormanBaseCover(CoordinatorEntity[NormanDataUpdateCoordinator], CoverEntity):
    _attr_assumed_state = True
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_has_entity_name = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        api: NormanGen1Api,
        coordinator: NormanDataUpdateCoordinator,
        room: NormanRoom,
    ) -> None:
        super().__init__(coordinator)
        self.api = api
        self.room = room
        self._optimistic_position: int | None = None
        self._refresh_generation = 0

    @property
    def device_info(self) -> dict[str, Any]:
        hub_name = self.api.hub_info.get("hubName") or "Norman Gen 1 Hub"
        return {
            "identifiers": {(DOMAIN, self.api.hub_id)},
            "name": f"Norman Hub {hub_name}",
            "manufacturer": "Norman",
            "model": "Gen 1 Hub",
            "sw_version": self.api.hub_info.get("swVer"),
            "configuration_url": f"http://{self.api.host}/",
        }

    @property
    def available(self) -> bool:
        return super().available and self.room.id in self.coordinator.data.get("rooms_by_id", {})

    @property
    def current_cover_position(self) -> int | None:
        return self._optimistic_position if self._optimistic_position is not None else self._current_position()

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        if position is None:
            return None
        return position <= 0

    async def _refresh_after_command(self, optimistic_position: int | None = None) -> None:
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._optimistic_position = optimistic_position
        self.async_write_ha_state()
        self.hass.async_create_task(self._delayed_refresh(generation))

    async def _run_control_command(self, command: Awaitable[None], optimistic_position: int) -> None:
        try:
            await command
        except CannotConnect as err:
            message = f"Unable to reach Norman Gen 1 hub at {self.api.host}"
            _LOGGER.warning("%s while controlling %s: %s", message, self.entity_id, err)
            raise HomeAssistantError(f"{message}: {err}") from err
        except InvalidAuth as err:
            message = "Norman Gen 1 hub rejected the control request; check the hub password"
            _LOGGER.warning("%s while controlling %s: %s", message, self.entity_id, err)
            raise HomeAssistantError(message) from err
        except CannotControl as err:
            message = "Norman Gen 1 hub did not confirm the shutter command"
            _LOGGER.warning("%s for %s: %s", message, self.entity_id, err)
            raise HomeAssistantError(f"{message}: {err}") from err
        await self._refresh_after_command(optimistic_position)

    async def _delayed_refresh(self, generation: int) -> None:
        await asyncio.sleep(COMMAND_SETTLE_SECONDS)
        if generation != self._refresh_generation:
            return
        try:
            await self.coordinator.async_request_refresh()
        finally:
            if generation == self._refresh_generation:
                self._optimistic_position = None
                self.async_write_ha_state()

    def _current_position(self) -> int | None:
        raise NotImplementedError


class NormanRoomCover(NormanBaseCover):
    """Room-wide cover using full open/full close hub commands."""

    def __init__(self, api: NormanGen1Api, coordinator: NormanDataUpdateCoordinator, room: NormanRoom) -> None:
        super().__init__(api, coordinator, room)
        self._attr_unique_id = f"{api.hub_id}_room_{room.id}"
        self._attr_name = _sanitize_name(room.name)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        windows = self.coordinator.data.get("windows_by_room", {}).get(self.room.id, [])
        return {
            "room_id": self.room.id,
            "window_count": len(windows),
            "levels": self.coordinator.data.get("levels_by_room", {}).get(self.room.id, []),
        }

    def _current_position(self) -> int | None:
        return _average_position(self.coordinator.data.get("windows_by_room", {}).get(self.room.id, []))

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._run_control_command(self.api.full_open_room(self.room.id), 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._run_control_command(self.api.full_close_room(self.room.id), 0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = int(kwargs[ATTR_POSITION])
        levels = self.coordinator.data.get("levels_by_room", {}).get(self.room.id, [])
        await self._run_control_command(self.api.set_room_position(self.room.id, levels, position), position)


class NormanGroupCover(NormanBaseCover):
    """Cover for one Norman room group/level."""

    def __init__(
        self,
        api: NormanGen1Api,
        coordinator: NormanDataUpdateCoordinator,
        room: NormanRoom,
        level: int,
        group_name: str,
    ) -> None:
        super().__init__(api, coordinator, room)
        self.level = level
        self.group_name = _sanitize_name(group_name)
        self._attr_unique_id = f"{api.hub_id}_room_{room.id}_level_{level}"
        self._attr_name = f"{_sanitize_name(room.name)} {self.group_name}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        windows = self._windows
        return {
            "room_id": self.room.id,
            "level": self.level,
            "group_name": self.group_name,
            "window_ids": [window.id for window in windows],
            "window_names": [window.name for window in windows],
        }

    @property
    def _windows(self) -> list[NormanWindow]:
        return self.coordinator.data.get("windows_by_group", {}).get((self.room.id, self.level), [])

    def _current_position(self) -> int | None:
        return _average_position(self._windows)

    def _model(self) -> int:
        for window in self._windows:
            model = window.raw.get("model")
            if model is not None:
                return int(model)
        return 1

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._run_control_command(self.api.set_group_position(self.room.id, self.level, 100, self._model()), 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._run_control_command(self.api.set_group_position(self.room.id, self.level, 0, self._model()), 0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = int(kwargs[ATTR_POSITION])
        await self._run_control_command(
            self.api.set_group_position(self.room.id, self.level, position, self._model()),
            position,
        )
