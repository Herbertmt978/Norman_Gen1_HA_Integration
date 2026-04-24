from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CannotConnect, InvalidAuth, NoDevicesFound, NormanGen1Api, NormanRoom, NormanWindow
from .const import CONF_APP_VERSION, DATA_API, DATA_COORDINATOR, DATA_HUB_INFO, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.COVER]
NormanConfigEntry = ConfigEntry


class NormanDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Norman Gen 1 hub room/window state."""

    def __init__(self, hass: HomeAssistant, api: NormanGen1Api) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._fetch_data()
        except InvalidAuth:
            try:
                await self.api.login()
                return await self._fetch_data()
            except (CannotConnect, InvalidAuth, NoDevicesFound) as err:
                raise UpdateFailed(str(err)) from err
        except CannotConnect as err:
            raise UpdateFailed(f"Unable to communicate with Norman Gen 1 hub: {err}") from err
        except NoDevicesFound as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch_data(self) -> dict[str, Any]:
        rooms = await self.api.get_rooms()
        windows = await self.api.get_windows()
        rooms_by_id = {room.id: room for room in rooms}
        windows_by_room: dict[int, list[NormanWindow]] = defaultdict(list)
        windows_by_group: dict[tuple[int, int], list[NormanWindow]] = defaultdict(list)
        levels_by_room: dict[int, set[int]] = defaultdict(set)
        for window in windows:
            if window.room_id < 0:
                _LOGGER.warning("Ignoring Norman window without a room id: %s", window.raw)
                continue
            windows_by_room[window.room_id].append(window)
            if window.level >= 0:
                windows_by_group[(window.room_id, window.level)].append(window)
                levels_by_room[window.room_id].add(window.level)
        discovered_rooms: list[NormanRoom] = list(rooms)
        for room_id in sorted(windows_by_room):
            if room_id not in rooms_by_id:
                fallback_room = NormanRoom(
                    id=room_id,
                    name=f"Room {room_id}",
                    group_names=[],
                    raw={"generated_from_window_scan": True},
                )
                discovered_rooms.append(fallback_room)
                rooms_by_id[room_id] = fallback_room
        if not discovered_rooms and not windows:
            raise NoDevicesFound("Hub responded, but no Norman rooms or shutter devices were discovered")
        return {
            "rooms": discovered_rooms,
            "windows": windows,
            "rooms_by_id": rooms_by_id,
            "windows_by_room": dict(windows_by_room),
            "windows_by_group": dict(windows_by_group),
            "levels_by_room": {room_id: sorted(levels) for room_id, levels in levels_by_room.items()},
        }


async def async_setup_entry(hass: HomeAssistant, entry: NormanConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = NormanGen1Api(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_APP_VERSION) or "2.11.21",
    )
    await api.login()

    coordinator = NormanDataUpdateCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()
    room_count = len(coordinator.data["rooms"])
    window_count = len(coordinator.data["windows"])
    group_count = sum(len(levels) for levels in coordinator.data["levels_by_room"].values())
    _LOGGER.info(
        "Discovered Norman Gen 1 hub with %s room(s), %s shutter device(s), and %s group(s)",
        room_count,
        window_count,
        group_count,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_API: api,
        DATA_COORDINATOR: coordinator,
        DATA_HUB_INFO: dict(api.hub_info),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NormanConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data is not None:
        await data[DATA_API].logout()
    return unload_ok
