"""TP-Link VIGI IPC OpenAPI client (port 20443).

Auth flow (two-step SHA-256, not the RSA+MD5 flow used on port 443):
  POST https://<ip>:20443/        {"method":"doAuth","params":null}
  → realm, nonce, uri, method
  a1       = SHA256(user:realm:password)
  a2       = SHA256(method:uri)
  response = SHA256(a1:nonce:a2)
  POST https://<ip>:20443/        {"method":"doAuth","params":{"nonce":...,"response":...}}
  → stok

Each request (including each auth step) requires a fresh TCP connection — the
camera closes the connection after every single response.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

OPENAPI_PORT = 20443
_STOK_TTL = 25 * 60  # 25 min (spec is 30 min — be conservative)
_TIMEOUT = aiohttp.ClientTimeout(total=10)


class VIGIOpenAPIError(Exception):
    pass


class VIGIOpenAPIAuthError(VIGIOpenAPIError):
    pass


class VIGIOpenAPI:
    """Async client for the TP-Link VIGI IPC OpenAPI on port 20443."""

    def __init__(self, ip: str, username: str, password: str) -> None:
        self._ip = ip
        self._username = username
        self._password = password
        self._base = f"https://{ip}:{OPENAPI_PORT}"
        self._stok: str | None = None
        self._stok_expiry: float = 0.0

        # Non-blocking SSL context (avoids load_default_certs() in HA event loop)
        self._ssl: ssl.SSLContext = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE

    def _session(self) -> aiohttp.ClientSession:
        """Fresh session per request — camera closes connection after each response."""
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=self._ssl, force_close=True),
            timeout=_TIMEOUT,
        )

    @staticmethod
    def _sha256(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()

    async def _do_auth(self) -> str:
        """Two-step doAuth. Each step uses a separate TCP connection."""
        async with self._session() as s:
            r1 = await s.post(self._base, json={"method": "doAuth", "params": None})
            d1 = await r1.json(content_type=None)

        auth = d1.get("authenticate", {})
        realm = auth.get("realm", "TP-LINK IP-Camera")
        nonce = auth.get("nonce", "")
        uri = auth.get("uri", "doAuth")
        meth = auth.get("method", "POST")

        a1 = self._sha256(f"{self._username}:{realm}:{self._password}")
        a2 = self._sha256(f"{meth}:{uri}")
        response = self._sha256(f"{a1}:{nonce}:{a2}")

        async with self._session() as s:
            r2 = await s.post(self._base, json={
                "method": "doAuth",
                "params": {"nonce": nonce, "response": response},
            })
            d2 = await r2.json(content_type=None)

        stok = d2.get("stok")
        if not stok:
            err = d2.get("errCode", d2.get("error_code", "unknown"))
            raise VIGIOpenAPIAuthError(f"doAuth failed (errCode={err})")
        return stok

    async def _ensure_stok(self) -> str:
        if self._stok and time.monotonic() < self._stok_expiry:
            return self._stok
        self._stok = await self._do_auth()
        self._stok_expiry = time.monotonic() + _STOK_TTL
        _LOGGER.debug("OpenAPI: new stok for %s (expires in %ds)", self._ip, _STOK_TTL)
        return self._stok

    async def call(self, method_name: str, params: dict[str, Any] | None = None) -> dict:
        """Call an OpenAPI method. Re-authenticates automatically on -10002."""
        stok = await self._ensure_stok()
        result = await self._call_raw(stok, method_name, params)

        if result.get("errCode") == -10002:
            _LOGGER.debug("OpenAPI: stok expired on %s, re-authenticating", self._ip)
            self._stok = None
            self._stok_expiry = 0.0
            stok = await self._ensure_stok()
            result = await self._call_raw(stok, method_name, params)

        return result

    async def _call_raw(
        self, stok: str, method_name: str, params: dict[str, Any] | None
    ) -> dict:
        url = f"{self._base}/stok={stok}"
        body: dict[str, Any] = {"method": method_name}
        if params is not None:
            body["params"] = params
        async with self._session() as s:
            r = await s.post(url, json=body)
            return await r.json(content_type=None)

    # ── System ────────────────────────────────────────────────────────────────

    async def get_device_info(self) -> dict:
        """Return full device info (alias, type, model, mac, hwVer, swVer, etc.)."""
        r = await self.call("getDeviceInfo")
        return r.get("result", {}).get("deviceInfo", r.get("result", {}))

    async def get_device_alias(self) -> str | None:
        """Return the current device alias string."""
        r = await self.call("getDeviceAlias")
        return r.get("result", {}).get("alias")

    async def set_device_alias(self, alias: str) -> None:
        """Set the camera display name (1-32 chars)."""
        await self.call("setDeviceAlias", {"alias": alias})

    async def do_soft_reset(self) -> None:
        """Reboot the camera (soft reset - does not wipe config)."""
        await self.call("doSoftReset")

    async def get_device_status(self) -> dict:
        """Return runtime status: uptime, cpu_usage, mem_usage, etc."""
        r = await self.call("getDeviceStatus")
        return r.get("result", {})

    async def search_system_log(
        self,
        start_time: int,
        end_time: int,
        log_type: str = "all",
    ) -> list[str]:
        """Return system log entries between two Unix timestamps.

        log_type: 'all' | 'alarm' | 'exception' | 'operation' | 'information'
        Returns a list of raw log strings.
        """
        r = await self.call("searchSystemLog", {
            "start_time": str(start_time),
            "end_time": str(end_time),
            "log_type": log_type,
        })
        result = r.get("result", {})
        logs = result.get("syslog", [])
        total = result.get("total", len(logs))
        _LOGGER.debug("searchSystemLog: %d entries (total=%s)", len(logs), total)
        return logs if isinstance(logs, list) else list(logs.values())

    # ── DateTime ──────────────────────────────────────────────────────────────

    async def get_system_time(self) -> dict:
        """Return current system time info (timestamp, timezone, ntp settings)."""
        r = await self.call("getSystemTime")
        return r.get("result", {})

    async def set_system_time(self, timestamp: int) -> None:
        """Set the camera clock to a Unix timestamp (requires NTP off)."""
        await self.call("setSystemTime", {"timestamp": str(timestamp)})

    async def get_timezone(self) -> dict:
        """Return timezone info (timezone string, dst settings)."""
        r = await self.call("getTimeZone")
        return r.get("result", {})

    async def set_timezone(self, timezone: str) -> None:
        """Set the camera timezone (e.g. 'GMT+05:30')."""
        await self.call("setTimeZone", {"timezone": timezone})

    # ── Audio ─────────────────────────────────────────────────────────────────

    async def get_speaker_volume(self) -> int | None:
        """Return speaker volume (0-100)."""
        r = await self.call("getSpeakerVolume")
        return r.get("result", {}).get("volume")

    async def set_speaker_volume(self, volume: int) -> None:
        """Set speaker volume (0-100)."""
        await self.call("setSpeakerVolume", {"volume": volume})

    async def get_microphone_volume(self) -> int | None:
        """Return microphone volume (0-100)."""
        r = await self.call("getMicrophoneVolume")
        return r.get("result", {}).get("volume")

    async def set_microphone_volume(self, volume: int) -> None:
        """Set microphone volume (0-100)."""
        await self.call("setMicrophoneVolume", {"volume": volume})

    async def get_audio_capability(self) -> dict:
        """Return audio capabilities of the camera."""
        r = await self.call("getAudioCapability")
        return r.get("result", {})

    # ── Video / Resolution ────────────────────────────────────────────────────

    async def get_resolution(self) -> dict:
        """Return current resolution config: {channel, width, height, frame_rate}."""
        r = await self.call("getResolution")
        return r.get("result", {})

    async def set_resolution(self, width: int, height: int, channel: int = 0) -> None:
        """Set video resolution. Common values: 2560x1440, 1920x1080, 1280x720."""
        await self.call("setResolution", {
            "channel": channel,
            "width": width,
            "height": height,
        })

    async def get_video_capability(self) -> dict:
        """Return supported resolutions and stream capabilities."""
        r = await self.call("getVideoCapability")
        return r.get("result", {})

    # ── SD Card ───────────────────────────────────────────────────────────────

    async def get_sd_card_status(self) -> dict:
        """Return SD card status including space, record times, and health."""
        r = await self.call("getSdCardStatus")
        return r.get("result", {})

    async def format_sd_card(self) -> None:
        """Format the SD card. DESTRUCTIVE - all recordings will be lost."""
        await self.call("formatSdCard")

    # ── Detection: CrossLine ──────────────────────────────────────────────────

    async def get_crossline_detection_switch(self) -> dict:
        """Return CrossLine detection switch state and msg_push_enabled."""
        r = await self.call("getCrosslineDetectionSwitch")
        return r.get("result", {})

    async def set_crossline_detection_switch(self, enabled: bool, msg_push: bool | None = None) -> None:
        """Enable or disable CrossLine detection."""
        params: dict = {"enabled": "on" if enabled else "off"}
        if msg_push is not None:
            params["msg_push_enabled"] = "on" if msg_push else "off"
        await self.call("setCrosslineDetectionSwitch", params)

    async def get_crossline_detection_region(self) -> dict:
        """Return CrossLine detection region configuration."""
        r = await self.call("getCrosslineDetectionRegion")
        return r.get("result", {})

    # ── Detection: Invasion ───────────────────────────────────────────────────

    async def get_invasion_detection_switch(self) -> dict:
        """Return Invasion (intrusion zone) detection switch state."""
        r = await self.call("getInvasionDetectionSwitch")
        return r.get("result", {})

    async def set_invasion_detection_switch(self, enabled: bool, msg_push: bool | None = None) -> None:
        """Enable or disable Invasion detection."""
        params: dict = {"enabled": "on" if enabled else "off"}
        if msg_push is not None:
            params["msg_push_enabled"] = "on" if msg_push else "off"
        await self.call("setInvasionDetectionSwitch", params)

    async def get_invasion_detection_region(self) -> dict:
        """Return Invasion detection region configuration."""
        r = await self.call("getInvasionDetectionRegion")
        return r.get("result", {})

    # ── Detection: Tamper ─────────────────────────────────────────────────────

    async def get_tamper_detection_switch(self) -> dict:
        r = await self.call("getTamperDetectionSwitch")
        return r.get("result", {})

    async def set_tamper_detection_switch(self, enabled: bool) -> None:
        await self.call("setTamperDetectionSwitch", {"enabled": "on" if enabled else "off"})

    # ── Detection: People ─────────────────────────────────────────────────────

    async def get_people_detection_switch(self) -> dict:
        r = await self.call("getPeopleDetectionSwitch")
        return r.get("result", {})

    async def set_people_detection_switch(self, enabled: bool) -> None:
        await self.call("setPeopleDetectionSwitch", {"enabled": "on" if enabled else "off"})

    # ── Detection: Vehicle ────────────────────────────────────────────────────

    async def get_vehicle_detection_switch(self) -> dict:
        r = await self.call("getVehicleDetectionSwitch")
        return r.get("result", {})

    async def set_vehicle_detection_switch(self, enabled: bool) -> None:
        await self.call("setVehicleDetectionSwitch", {"enabled": "on" if enabled else "off"})

    # ── Detection: AreaEntry / AreaLeave / DropAndTake / Loiter / SceneChange / AudioAnomaly ──

    async def get_area_entry_detection_switch(self) -> dict:
        r = await self.call("getAreaEntryDetectionSwitch")
        return r.get("result", {})

    async def set_area_entry_detection_switch(self, enabled: bool) -> None:
        await self.call("setAreaEntryDetectionSwitch", {"enabled": "on" if enabled else "off"})

    async def get_area_leave_detection_switch(self) -> dict:
        r = await self.call("getAreaLeaveDetectionSwitch")
        return r.get("result", {})

    async def set_area_leave_detection_switch(self, enabled: bool) -> None:
        await self.call("setAreaLeaveDetectionSwitch", {"enabled": "on" if enabled else "off"})

    async def get_drop_and_take_detection_switch(self) -> dict:
        r = await self.call("getDropAndTakeDetectionSwitch")
        return r.get("result", {})

    async def set_drop_and_take_detection_switch(self, enabled: bool) -> None:
        await self.call("setDropAndTakeDetectionSwitch", {"enabled": "on" if enabled else "off"})

    async def get_loiter_detection_switch(self) -> dict:
        r = await self.call("getLoiterDetectionSwitch")
        return r.get("result", {})

    async def set_loiter_detection_switch(self, enabled: bool) -> None:
        await self.call("setLoiterDetectionSwitch", {"enabled": "on" if enabled else "off"})

    async def get_scene_change_detection_switch(self) -> dict:
        r = await self.call("getSceneChangeDetectionSwitch")
        return r.get("result", {})

    async def set_scene_change_detection_switch(self, enabled: bool) -> None:
        await self.call("setSceneChangeDetectionSwitch", {"enabled": "on" if enabled else "off"})

    async def get_audio_anomaly_detection_switch(self) -> dict:
        r = await self.call("getAudioAnomalyDetectionSwitch")
        return r.get("result", {})

    async def set_audio_anomaly_detection_switch(self, enabled: bool) -> None:
        await self.call("setAudioAnomalyDetectionSwitch", {"enabled": "on" if enabled else "off"})

    async def get_event_enhance_capability(self) -> dict:
        """Return which enhanced detection types this camera supports."""
        r = await self.call("getEventEnhanceCapability")
        return r.get("result", {})

    # ── PTZ ───────────────────────────────────────────────────────────────────

    async def get_preset_points(self) -> dict:
        """Return all saved PTZ preset points."""
        r = await self.call("getPresetPoint")
        return r.get("result", {})

    async def set_preset_point(self, preset_id: str, name: str, channel: int = 0) -> None:
        """Save current PTZ position as a named preset."""
        await self.call("setPresetPoint", {
            "channel": channel,
            "id": preset_id,
            "name": name,
        })

    async def remove_preset_point(self, preset_id: str, channel: int = 0) -> None:
        """Delete a saved PTZ preset."""
        await self.call("removePresetPoint", {"channel": channel, "id": preset_id})

    async def goto_preset_point(self, preset_id: str, channel: int = 0) -> None:
        """Move camera to a saved PTZ preset position."""
        await self.call("gotoPresetPoint", {"channel": channel, "id": preset_id})

    async def motor_move(self, pan: float, tilt: float, zoom: float = 0.0) -> None:
        """Start continuous PTZ movement. Call stop_move() to stop."""
        await self.call("motorMove", {"pan": pan, "tilt": tilt, "zoom": zoom})

    async def stop_move(self) -> None:
        """Stop all PTZ movement."""
        await self.call("stopMove")

    async def cruise_move(
        self,
        cruise_id: int = 1,
        action: str = "start",
        channel: int = 0,
    ) -> None:
        """Start or stop a PTZ cruise (auto-patrol) route.

        action: 'start' | 'stop'
        cruise_id: identifier of the cruise route (camera-defined, typically 1).
        """
        await self.call("cruiseMove", {
            "channel": channel,
            "id": cruise_id,
            "action": action,
        })

    async def get_ptz_capability(self) -> dict:
        """Return PTZ capabilities: pan/tilt/zoom ranges, supported modes."""
        r = await self.call("getPTZCapability")
        return r.get("result", {})

    # ── Stream Port ───────────────────────────────────────────────────────────

    async def get_stream_port(self) -> dict:
        """Return stream port configuration (RTSP port, etc.)."""
        r = await self.call("getStreamPort")
        return r.get("result", {})

    # ── Record Schedule ───────────────────────────────────────────────────────

    async def get_record_schedule(self) -> dict:
        """Return current recording schedule configuration."""
        r = await self.call("getRecordSchedule")
        return r.get("result", {})

    async def set_record_schedule(self, schedule: dict) -> None:
        """Set recording schedule.

        schedule should follow the API format:
        {
            "channel": 0,
            "mode": "schedule",   # "always" | "schedule" | "motion" | "off"
            "plan": [...]         # optional weekly schedule array
        }
        """
        await self.call("setRecordSchedule", schedule)

    # ── Message Push / Subscriptions ──────────────────────────────────────────

    async def get_msgpush_interval(self) -> dict:
        """Return message push interval settings."""
        r = await self.call("getMsgpushInterval")
        return r.get("result", {})

    async def set_msgpush_interval(self, interval: int) -> None:
        """Set message push interval in seconds."""
        await self.call("setMsgpushInterval", {"interval": interval})

    # ── Playback ──────────────────────────────────────────────────────────────

    async def search_video_calendar(self, year: int, month: int, channel: int = 0) -> dict:
        """Return a bitmap of days in the given month that have recordings.

        Response includes a 'days' bitmask - bit N set means day N+1 has recordings.
        """
        r = await self.call("searchVideoCalendar", {
            "channel": channel,
            "year": year,
            "month": month,
        })
        return r.get("result", {})

    async def search_video_list(
        self,
        start_time: int,
        end_time: int,
        channel: int = 0,
        event_type: str = "all",
    ) -> list[dict]:
        """Return list of video recordings between two Unix timestamps."""
        r = await self.call("searchVideoList", {
            "channel": channel,
            "start_time": str(start_time),
            "end_time": str(end_time),
            "event_type": event_type,
        })
        result = r.get("result", {})
        return result.get("video_list", [])

    # ── Alarm ─────────────────────────────────────────────────────────────────

    async def manual_alarm(self, action: str = "start") -> None:
        """Trigger or stop the manual alarm. action: 'start' | 'stop'."""
        await self.call("manualAlarm", {"action": action})


async def try_openapi(ip: str, username: str, password: str) -> bool:
    """Return True if OpenAPI is reachable on port 20443 and auth succeeds."""
    client = VIGIOpenAPI(ip, username, password)
    try:
        await asyncio.wait_for(client._do_auth(), timeout=8)
        return True
    except Exception as exc:
        _LOGGER.debug("OpenAPI probe failed for %s: %s", ip, exc)
        return False
