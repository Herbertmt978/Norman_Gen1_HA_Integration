from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CannotConnect, InvalidAuth, NoDevicesFound, NormanGen1Api
from .const import CONF_APP_VERSION, DEFAULT_APP_VERSION, DEFAULT_PASSWORD, DOMAIN

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
