from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
import unittest
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT_PATH = ROOT / "custom_components" / "norman_gen1"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(ROOT / "custom_components")]
sys.modules.setdefault("custom_components", custom_components)

norman_package = types.ModuleType("custom_components.norman_gen1")
norman_package.__path__ = [str(COMPONENT_PATH)]
sys.modules.setdefault("custom_components.norman_gen1", norman_package)

api_module = importlib.import_module("custom_components.norman_gen1.api")
CannotControl = api_module.CannotControl
NormanGen1Api = api_module.NormanGen1Api


class RecordingApi(NormanGen1Api):
    def __init__(self) -> None:
        super().__init__(object(), "192.0.2.10", "password")
        self.calls: list[dict[str, Any]] = []

    async def _remote_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        return {"errorCode": 0}


class TestRoomPositionControl(unittest.TestCase):
    def test_room_close_uses_discovered_group_levels(self) -> None:
        api = RecordingApi()

        asyncio.run(api.set_room_position(56548, [3, 1, 1, 0], 0, {1: 2}))

        self.assertEqual(
            api.calls,
            [
                {"type": "level", "Lid": 0, "id": 56548, "action": 0, "model": 1},
                {"type": "level", "Lid": 1, "id": 56548, "action": 0, "model": 2},
                {"type": "level", "Lid": 3, "id": 56548, "action": 0, "model": 1},
            ],
        )

    def test_room_open_uses_discovered_group_levels(self) -> None:
        api = RecordingApi()

        asyncio.run(api.set_room_position(56548, [0, 1], 100))

        self.assertEqual(
            api.calls,
            [
                {"type": "level", "Lid": 0, "id": 56548, "action": 100, "model": 1},
                {"type": "level", "Lid": 1, "id": 56548, "action": 100, "model": 1},
            ],
        )

    def test_room_close_falls_back_to_full_close_without_levels(self) -> None:
        api = RecordingApi()

        asyncio.run(api.set_room_position(56548, [], 0))

        self.assertEqual(api.calls, [{"type": "fullclose", "action": 2, "id": 56548}])

    def test_intermediate_position_needs_discovered_levels(self) -> None:
        api = RecordingApi()

        with self.assertRaises(CannotControl):
            asyncio.run(api.set_room_position(56548, [], 50))


if __name__ == "__main__":
    unittest.main()
