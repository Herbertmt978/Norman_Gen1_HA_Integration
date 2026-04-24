from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import DEFAULT_APP_VERSION

_LOGGER = logging.getLogger(__name__)


class NormanGen1Error(Exception):
    """Base error for Norman Gen 1 API failures."""


class CannotConnect(NormanGen1Error):
    """Raised when the hub cannot be reached."""


class InvalidAuth(NormanGen1Error):
    """Raised when the hub rejects the password."""


class NoDevicesFound(NormanGen1Error):
    """Raised when the hub responds but returns no controllable devices."""


class CannotControl(NormanGen1Error):
    """Raised when the hub rejects or fails to confirm a control command."""


@dataclass(slots=True)
class NormanRoom:
    id: int
    name: str
    group_names: list[str]
    raw: dict[str, Any]


@dataclass(slots=True)
class NormanWindow:
    id: int
    name: str
    room_id: int
    level: int
    group_id: int | None
    position: int | None
    battery: str | None
    raw: dict[str, Any]


class NormanGen1Api:
    """Minimal local API client for Norman Gen 1 hubs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        password: str,
        app_version: str = DEFAULT_APP_VERSION,
    ) -> None:
        self._session = session
        self.host = host.strip().removeprefix("http://").removeprefix("https://").strip("/")
        self.password = password
        self.app_version = app_version
        self._session_cookie: str | None = None
        self.hub_info: dict[str, Any] = {}

    @property
    def base_url(self) -> str:
        return f"http://{self.host}/cgi-bin/cgi"

    @property
    def hub_id(self) -> str:
        return str(self.hub_info.get("hubId") or self.host)

    async def login(self) -> dict[str, Any]:
        payload = {"password": self.password, "app_version": self.app_version}
        data, headers = await self._post("GatewayLogin", payload, require_session=False)
        if "errorCode" in data:
            if data.get("errorCode") == -13:
                raise InvalidAuth("Hub rejected the password")
            raise CannotConnect(f"Hub returned errorCode {data.get('errorCode')}")
        cookie = headers.get("Set-Cookie") or headers.get("set-cookie")
        if cookie:
            self._session_cookie = cookie.split(";", 1)[0]
        if not self._session_cookie:
            session_header = headers.get("session") or headers.get("Session")
            if session_header:
                self._session_cookie = session_header.split(";", 1)[0]
        self.hub_info = data
        return data

    async def logout(self) -> None:
        try:
            await self._post("AdminLogout", {})
            await self._post("GatewayLogout", {})
        except NormanGen1Error:
            _LOGGER.debug("Ignoring logout failure", exc_info=True)
        finally:
            self._session_cookie = None

    async def get_rooms(self) -> list[NormanRoom]:
        data, _ = await self._post("getRoomInfo", {})
        rooms = []
        for room in data.get("rooms", []):
            room_id = _as_int(room.get("Id", room.get("id")))
            if room_id is None:
                _LOGGER.warning("Skipping Norman room without a numeric Id: %s", room)
                continue
            rooms.append(
                NormanRoom(
                    id=room_id,
                    name=str(room.get("Name") or room_id),
                    group_names=[str(name) for name in room.get("groupname", [])],
                    raw=room,
                )
            )
        return rooms

    async def get_windows(self) -> list[NormanWindow]:
        data, _ = await self._post("getWindowInfo", {})
        return self._parse_windows(data)

    async def get_group_position(self, room_id: int, level: int) -> int | None:
        data, _ = await self._post(
            "getWindowInfo",
            {"action": "group_position", "Id": room_id, "Lid": level},
        )
        windows = self._parse_windows(data, default_room_id=room_id, default_level=level)
        positions = [window.position for window in windows if window.position is not None]
        if not positions:
            return None
        return round(sum(positions) / len(positions))

    async def full_open_room(self, room_id: int) -> None:
        await self._remote_control({"type": "fullopen", "action": 2, "id": room_id})

    async def full_close_room(self, room_id: int) -> None:
        await self._remote_control({"type": "fullclose", "action": 2, "id": room_id})

    async def set_group_position(self, room_id: int, level: int, position: int, model: int = 1) -> None:
        position = max(0, min(100, int(position)))
        await self._remote_control(
            {"type": "level", "Lid": int(level), "id": int(room_id), "action": position, "model": model}
        )

    async def set_room_position(self, room_id: int, levels: list[int], position: int) -> None:
        position = max(0, min(100, int(position)))
        if position >= 100:
            await self.full_open_room(room_id)
            return
        if position <= 0:
            await self.full_close_room(room_id)
            return
        for level in sorted(set(levels)):
            await self.set_group_position(room_id, level, position)
            await asyncio.sleep(0.15)

    async def _remote_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        data, _ = await self._post("RemoteControl", payload)
        if _as_int(data.get("errorCode")) not in (None, 0):
            message = f"RemoteControl returned errorCode {data.get('errorCode')}"
            _LOGGER.warning("%s for payload %s: %s", message, payload, data)
            raise CannotControl(message)
        confirmed = _as_int(data.get("errorCode")) == 0 or any(_is_success_value(value) for value in data.values())
        if not confirmed:
            message = f"RemoteControl did not confirm command: {data}"
            _LOGGER.warning("%s for payload %s", message, payload)
            raise CannotControl(message)
        return data

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        require_session: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": f"SmartShutterControl/103 HomeAssistant NormanGen1/{self.app_version}",
        }
        if require_session and self._session_cookie:
            headers["Cookie"] = self._session_cookie
        url = f"{self.base_url}/{endpoint}"
        try:
            async with self._session.post(url, json=payload, headers=headers, timeout=20) as response:
                text = await response.text()
                if response.status != 200:
                    raise CannotConnect(f"{endpoint} returned HTTP {response.status}: {text[:200]}")
                headers_out = {key: value for key, value in response.headers.items()}
                session_header = response.headers.get("session") or response.headers.get("Set-Cookie")
                if session_header and "Session=" in session_header:
                    self._session_cookie = session_header.split(";", 1)[0]
                try:
                    data = await response.json(content_type=None)
                except Exception as err:  # noqa: BLE001
                    raise CannotConnect(f"{endpoint} returned non-JSON: {text[:200]}") from err
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as err:
            raise CannotConnect(str(err)) from err
        if isinstance(data, dict) and data.get("errorCode") == -13:
            raise InvalidAuth("Hub rejected the request/session")
        if not isinstance(data, dict):
            raise CannotConnect(f"{endpoint} returned unexpected payload: {data!r}")
        return data, headers_out

    def _parse_windows(
        self,
        data: dict[str, Any],
        *,
        default_room_id: int | None = None,
        default_level: int | None = None,
    ) -> list[NormanWindow]:
        windows = []
        for window in data.get("windows", []):
            window_id = _as_int(window.get("Id", window.get("id")))
            if window_id is None:
                _LOGGER.warning("Skipping Norman window without a numeric Id: %s", window)
                continue
            room_id = window.get("roomId", window.get("RId", default_room_id))
            level = window.get("Level", window.get("Lid", default_level))
            position = window.get("position")
            parsed_room_id = _as_int(room_id)
            parsed_level = _as_int(level)
            windows.append(
                NormanWindow(
                    id=window_id,
                    name=str(window.get("Name") or window_id),
                    room_id=parsed_room_id if parsed_room_id is not None else -1,
                    level=parsed_level if parsed_level is not None else -1,
                    group_id=_as_int(window.get("groupId")),
                    position=_as_int(position),
                    battery=str(window["battery"]) if window.get("battery") is not None else None,
                    raw=window,
                )
            )
        return windows


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_success_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"ok", "success", "true"}
    return False
