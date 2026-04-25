from __future__ import annotations

import logging
from typing import Any, Iterable

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CannotConnect,
    InvalidAuth,
    NoDevicesFound,
    NormanGen1Api,
    NormanRoom,
    group_target_id,
    room_close_position,
    room_open_position,
    room_target_id,
)
from .const import (
    CONF_APP_VERSION,
    CONF_REVERSED_CLOSE_TARGETS,
    CONF_TILT_OPEN_TARGETS,
    DATA_COORDINATOR,
    DEFAULT_APP_VERSION,
    DEFAULT_PASSWORD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    session = async_get_clientsession(hass)
    api = NormanGen1Api(
        session,
        data[CONF_HOST],
        data[CONF_PASSWORD],
        data.get(CONF_APP_VERSION, DEFAULT_APP_VERSION),
    )
    try:
        info = await api.login()
        rooms = await api.get_rooms()
        windows = await api.get_windows()
    finally:
        await api.logout()
    if not rooms and not windows:
        raise NoDevicesFound("Hub responded, but no rooms or shutter devices were discovered")
    return info


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Norman Gen 1 Hub."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_HOST] = (
                user_input[CONF_HOST].strip().removeprefix("http://").removeprefix("https://").strip("/")
            )
            try:
                info = await _validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except NoDevicesFound:
                errors["base"] = "no_devices"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception validating Norman Gen 1 hub")
                errors["base"] = "unknown"
            else:
                hub_id = str(info.get("hubId") or user_input[CONF_HOST])
                await self.async_set_unique_id(hub_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                return self.async_create_entry(title=info.get("hubName") or "Norman Gen 1 Hub", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                vol.Optional(CONF_APP_VERSION, default=DEFAULT_APP_VERSION): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Norman Gen 1 options."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        choices, tilt_defaults, reversed_defaults = _target_choices(self.hass, self.entry)

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_TILT_OPEN_TARGETS: list(user_input.get(CONF_TILT_OPEN_TARGETS, [])),
                    CONF_REVERSED_CLOSE_TARGETS: list(user_input.get(CONF_REVERSED_CLOSE_TARGETS, [])),
                },
            )

        tilt_targets = self.entry.options.get(CONF_TILT_OPEN_TARGETS, tilt_defaults)
        reversed_targets = self.entry.options.get(CONF_REVERSED_CLOSE_TARGETS, reversed_defaults)
        _add_unknown_targets(choices, tilt_targets)
        _add_unknown_targets(choices, reversed_targets)

        schema = vol.Schema(
            {
                vol.Optional(CONF_TILT_OPEN_TARGETS, default=list(tilt_targets)): cv.multi_select(choices),
                vol.Optional(CONF_REVERSED_CLOSE_TARGETS, default=list(reversed_targets)): cv.multi_select(choices),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def _target_choices(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
) -> tuple[dict[str, str], list[str], list[str]]:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get(DATA_COORDINATOR)
    if coordinator is None or coordinator.data is None:
        return {}, [], []

    rooms: list[NormanRoom] = coordinator.data.get("rooms", [])
    levels_by_room: dict[int, list[int]] = coordinator.data.get("levels_by_room", {})
    choices: dict[str, str] = {}
    tilt_defaults: list[str] = []
    reversed_defaults: list[str] = []

    for room in sorted(rooms, key=lambda item: _clean_label(item.name)):
        room_label = _clean_label(room.name)
        room_key = room_target_id(room.id)
        choices[room_key] = f"{room_label} (room)"
        if room_open_position(room.raw, None) == 37:
            tilt_defaults.append(room_key)
        if room_close_position(room.raw) == 100:
            reversed_defaults.append(room_key)

        for level in levels_by_room.get(room.id, []):
            group_label = _clean_label(_group_label(room, level))
            choices[group_target_id(room.id, level)] = f"{room_label} - {group_label}"

    return choices, tilt_defaults, reversed_defaults


def _clean_label(value: str) -> str:
    return " ".join(value.split()) or "Unnamed"


def _group_label(room: NormanRoom, level: int) -> str:
    if 0 <= level < len(room.group_names):
        return room.group_names[level]
    if 1 <= level <= len(room.group_names):
        return room.group_names[level - 1]
    return f"Group {level + 1}" if level >= 0 else "Group"


def _add_unknown_targets(choices: dict[str, str], targets: Iterable[str]) -> None:
    for target in targets:
        choices.setdefault(str(target), f"{target} (not currently discovered)")
