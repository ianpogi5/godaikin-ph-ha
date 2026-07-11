"""
API client for the GO DAIKIN Philippine cloud service.

Device data is fetched from the "international" gateway with two calls that are
merged by ThingName:
  - gethomepage            -> per-unit metadata (ACName, ACGroup, plan, ...)
  - gethomepageshadowstate -> per-unit shadow state (Set_*/Sta_*/Ena_* fields)
Control commands use publishdevicestate (type 3), same desired-state shape as
other regions. Every request carries `authorization: <AccessToken>`.
"""

import logging

import aiohttp

from .auth import AuthClient
from .const import DEVICE_BASE_URL
from .types import *

_LOGGER = logging.getLogger(__name__)


class ApiClient:
    def __init__(self, auth: AuthClient):
        self.auth = auth
        # Reuse the auth client's session so there is a single session to close.
        self.session = auth.session

        self.airconds_by_unique_id: dict[UniqueID, Aircond] = {}
        # ThingName -> metadata dict from gethomepage (names rarely change, so we
        # fetch it once and refresh only when an unknown unit shows up).
        self._metadata_by_thing_name: dict[str, dict] = {}

    async def _api_request(self, endpoint: str, payload: dict) -> dict | list:
        access_token = await self.auth.async_get_token()
        async with self.session.post(
            f"{DEVICE_BASE_URL}{endpoint}",
            json=payload,
            headers={"authorization": access_token},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_airconds(self) -> list[Aircond]:
        _LOGGER.debug("Getting airconds")

        user_id = self.auth.user_id

        shadow_list = await self._api_request(
            "gethomepageshadowstate",
            {"requestData": {"userID": user_id}},
        )
        shadow_by_thing_name = {
            item["ThingName"]: item.get("shadowState", {})
            for item in shadow_list
            if "ThingName" in item
        }

        # (Re)load metadata if empty or a unit we don't know about appeared.
        if not self._metadata_by_thing_name or any(
            tn not in self._metadata_by_thing_name for tn in shadow_by_thing_name
        ):
            await self._refresh_metadata(user_id)

        airconds: list[Aircond] = []
        for thing_name, shadow_state in shadow_by_thing_name.items():
            metadata = self._metadata_by_thing_name.get(thing_name, {})
            aircond_data = {**metadata, "shadowState": shadow_state}
            airconds.append(Aircond.from_api(aircond_data))

        self.airconds_by_unique_id = {a.unique_id: a for a in airconds}
        return airconds

    async def _refresh_metadata(self, user_id: str | None) -> None:
        response_data = await self._api_request(
            "gethomepage",
            {"requestData": {"type": 1, "userID": user_id}},
        )
        self._metadata_by_thing_name = {
            item["ThingName"]: item
            for item in response_data.get("data", [])
            if "ThingName" in item
        }

    async def set_mode(self, unique_id: UniqueID, mode: AircondMode):
        _LOGGER.info("Setting mode %s for %s", mode.value, unique_id)

        await self._set_desired_state(
            unique_id,
            Set_OnOff=1,
            Set_Mode=mode.value,
        )

    async def set_preset(self, unique_id: UniqueID, preset: AircondPreset):
        _LOGGER.info("Setting preset %s for %s", preset.value, unique_id)

        default_settings = dict(
            Set_Breeze=0,
            Set_Ecoplus=0,
            Set_Silent=0,
            Set_Sleep=0,
            Set_SmEcomax=0,
            Set_SmSleepplus=0,
            Set_SmPwrfulplus=0,
            Set_Turbo=0,
        )

        match preset:
            case AircondPreset.NONE:
                pass
            case AircondPreset.COMFORT:
                default_settings["Set_Breeze"] = 1
            case AircondPreset.ECO:
                default_settings["Set_Ecoplus"] = 1
                default_settings["Set_SmEcomax"] = 0
            case AircondPreset.BOOST:
                default_settings["Set_Silent"] = 0
                default_settings["Set_Turbo"] = 1
            case AircondPreset.SLEEP:
                default_settings["Set_Sleep"] = 1
                default_settings["Set_SmSleepplus"] = 0

        await self._set_desired_state(unique_id, **default_settings)

    async def set_fan_mode(self, unique_id: UniqueID, fan: FanSpeed):
        _LOGGER.info("Setting fan mode %s for %s", fan.value, unique_id)

        await self._set_desired_state(
            unique_id,
            Set_Fan=fan.value,
        )

    async def set_swing(
        self, unique_id: UniqueID, swing: AircondSwing, horizontal: bool = False
    ):
        _LOGGER.info(
            "Setting swing %s (horizontal=%s) for %s",
            swing.value,
            horizontal,
            unique_id,
        )

        if horizontal:
            await self._set_desired_state(
                unique_id,
                Set_LRLvr=swing.value,
            )
        else:
            await self._set_desired_state(
                unique_id,
                Set_Swing=1 if swing == AircondSwing.AUTO else 0,
                Set_UDLvr=swing.value,
            )

    async def set_temperature(self, unique_id: UniqueID, temperature: int):
        _LOGGER.info("Setting temperature %s for %s", temperature, unique_id)

        await self._set_desired_state(
            unique_id,
            Set_Temp=temperature,
        )

    async def turn_off(self, unique_id: UniqueID):
        _LOGGER.info("Turning off %s", unique_id)

        await self._set_desired_state(unique_id, Set_OnOff=0)

    async def turn_on(self, unique_id: UniqueID):
        _LOGGER.info("Turning on %s", unique_id)

        await self._set_desired_state(unique_id, Set_OnOff=1)

    async def set_streamer(self, unique_id: UniqueID, on: bool):
        _LOGGER.info("Setting streamer %s for %s", on, unique_id)

        await self._set_desired_state(unique_id, Set_Streamer=1 if on else 0)

    async def set_status_led(self, unique_id: UniqueID, on: bool):
        _LOGGER.info("Setting status LED %s for %s", on, unique_id)

        if on:
            await self._set_desired_state(unique_id, Set_LEDOff=0, Set_PwrInd=1)
        else:
            await self._set_desired_state(unique_id, Set_LEDOff=1, Set_PwrInd=0)

    async def _set_desired_state(self, unique_id: UniqueID, **state):
        aircond = self.airconds_by_unique_id[unique_id]

        response_data = await self._api_request(
            "publishdevicestate",
            {
                "requestData": {
                    "type": 3,
                    "username": self.auth.username,
                    "thingName": aircond.ThingName,
                    "key": aircond.shadowState.key,
                    "payload": {"state": {"desired": state}},
                }
            },
        )
        _LOGGER.debug(
            "Set state request: ac_name=%s, unique_id=%s, state=%s",
            aircond.ACName,
            unique_id,
            state,
        )
        _LOGGER.debug(
            "Set state response: ac_name=%s, unique_id=%s, response=%s",
            aircond.ACName,
            unique_id,
            response_data,
        )
