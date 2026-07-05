from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - Home Assistant should provide paho via mqtt deps or custom install.
    mqtt = None  # type: ignore[assignment]

from .const import API_BASE, API_HOST, APP_ID, INDOOR_MESH_PREFIX, SITE_HOST

_LOGGER = logging.getLogger(__name__)
_MQTT_CLIENT_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class FGCAirError(Exception):
    """FGCAir 云端请求错误。"""


class FGCAirAuthError(FGCAirError):
    """FGCAir token 或账号认证错误。"""


WsMessageCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class FGCAirSession:
    uid: str
    token: str
    expire_at: int | None = None


def _is_token_expired_error(text: str) -> bool:
    lowered = text.lower()
    return "token expired" in lowered or '"error_code":9006' in text or '"error_code":"9006"' in text


def _request_sync(
    method: str,
    path: str,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    host: str | None = API_HOST,
    app_id: str = APP_ID,
    timeout: int = 20,
) -> Any:
    if query:
        path = f"{path}?{urllib.parse.urlencode(query)}"
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "GizWifiSDK (v21.23030112)",
        "X-Gizwits-Application-Id": app_id,
        "language": "zh-CN",
    }
    if host:
        headers["Host"] = host
    if token:
        headers["X-Gizwits-User-token"] = token
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=headers, method=method)
    _LOGGER.debug("FGCAir request %s %s body=%s", method, path, body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            _LOGGER.debug("FGCAir response %s %s status=%s body=%s", method, path, resp.status, raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        _LOGGER.warning("FGCAir request failed %s %s status=%s body=%s", method, path, exc.code, detail)
        if _is_token_expired_error(detail) or exc.code in (401, 403):
            raise FGCAirAuthError(detail) from exc
        raise FGCAirError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise FGCAirError(f"HTTP 请求失败: {exc}") from exc
    return json.loads(raw) if raw else None


class FGCAirClient:
    """FGCAir 私有云 HTTP 客户端。"""

    def __init__(self, username: str, password: str, session: FGCAirSession | None = None) -> None:
        self.username = username
        self.password = password
        self.session = session
        self._ws_subscribed_dids: set[str] = set()
        self._ws_message_callback: WsMessageCallback | None = None
        self._mqtt_task: asyncio.Task[None] | None = None
        self._mqtt_stop = asyncio.Event()
        self._mqtt_client: Any | None = None
        self._mqtt_loop: asyncio.AbstractEventLoop | None = None
        self._mqtt_devices: list[dict[str, Any]] = []
        self._mqtt_cached_devices: list[dict[str, Any]] = []
        self._mqtt_update_interval = 60

    async def _request(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(_request_sync, *args, **kwargs)

    async def login(self) -> FGCAirSession:
        payloads = [
            {"username": self.username, "password": self.password, "lang": "zh-cn"},
            {"phone": self.username, "password": self.password, "lang": "zh-cn"},
        ]
        last_error: Exception | None = None
        for payload in payloads:
            try:
                data = await self._request("POST", "/app/login", body=payload)
                uid = data.get("uid") or data.get("user_id")
                token = data.get("token")
                if uid and token:
                    self.session = FGCAirSession(str(uid), str(token), data.get("expire_at"))
                    return self.session
            except FGCAirError as exc:
                last_error = exc
        raise FGCAirAuthError(f"登录失败: {last_error}")

    async def ensure_session(self, force: bool = False) -> FGCAirSession:
        if force or not self.session or not self.session.token:
            return await self.login()
        return self.session

    async def list_bindings(self, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            await self.ensure_session()
        if not self.session:
            raise FGCAirAuthError("尚未登录")
        data = await self._request(
            "GET",
            "/app/bindings",
            token=self.session.token,
            query={"show_disabled": 0, "limit": 200, "skip": 0},
        )
        return list(data.get("devices", []))

    def set_ws_message_callback(self, callback: WsMessageCallback) -> None:
        self._ws_message_callback = callback

    async def start_ws_listener(
        self,
        dids: list[str],
        update_interval: int = 60,
        cached_devices: list[dict[str, Any]] | None = None,
    ) -> None:
        await self.start_mqtt_listener(dids, update_interval, cached_devices)

    async def stop_ws_listener(self) -> None:
        await self.stop_mqtt_listener()

    async def start_mqtt_listener(
        self,
        dids: list[str],
        update_interval: int = 60,
        cached_devices: list[dict[str, Any]] | None = None,
    ) -> None:
        self._ws_subscribed_dids = set(dids)
        self._mqtt_update_interval = max(10, int(update_interval))
        self._mqtt_cached_devices = cached_devices or []
        if self._mqtt_task and not self._mqtt_task.done():
            return
        self._mqtt_loop = asyncio.get_running_loop()
        self._mqtt_stop.clear()
        self._mqtt_task = asyncio.create_task(self._mqtt_loop_task(), name="fgcair_mqtt_listener")

    async def stop_mqtt_listener(self) -> None:
        self._mqtt_stop.set()
        if self._mqtt_client is not None:
            await asyncio.to_thread(_mqtt_disconnect, self._mqtt_client)
            self._mqtt_client = None
        task = self._mqtt_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._mqtt_task = None

    async def _mqtt_loop_task(self) -> None:
        if mqtt is None:
            _LOGGER.warning("FGCAir MQTT disabled because paho-mqtt is not installed")
            return
        backoff = 1
        while not self._mqtt_stop.is_set():
            try:
                await self.ensure_session()
                if not self.session:
                    raise FGCAirAuthError("尚未登录")
                bindings = self._mqtt_cached_devices or await self.list_bindings(refresh=False)
                selected = self._ws_subscribed_dids
                devices = [device for device in bindings if str(device.get("did")) in selected]
                if not devices:
                    devices = [device for device in bindings if indoor_index(device) is not None]
                gateway = _gateway_device_for(devices, bindings)
                if not gateway:
                    raise FGCAirError("找不到 MQTT 网关设备")
                self._mqtt_devices = devices
                await self._mqtt_session(gateway, devices)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except FGCAirAuthError:
                await self.ensure_session(force=True)
                await asyncio.sleep(1)
            except Exception as exc:
                _LOGGER.warning("FGCAir MQTT loop error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _mqtt_session(self, gateway: dict[str, Any], devices: list[dict[str, Any]]) -> None:
        assert self.session
        host = str(gateway.get("host") or "m2m.fgcawx.com")
        port = int(gateway.get("port_s") or 8883)
        gateway_did = str(gateway.get("did"))
        client_id = _mqtt_client_id(self.session.uid)
        client = await asyncio.to_thread(
            _build_mqtt_client,
            client_id,
            self.session.uid,
            self.session.token,
            self._handle_mqtt_message,
        )
        self._mqtt_client = client
        _LOGGER.info("FGCAir MQTT connect host=%s port=%s client_id=%s gateway=%s", host, port, client_id, gateway_did)
        await asyncio.to_thread(_mqtt_connect, client, host, port, gateway_did, client_id)
        try:
            while not self._mqtt_stop.is_set():
                for device in devices:
                    payload = _mqtt_query_payload(device)
                    if payload:
                        client.publish(f"app2dev/{gateway_did}/{client_id}", payload, qos=0)
                        await asyncio.sleep(0.35)
                await asyncio.sleep(self._mqtt_update_interval)
        finally:
            await asyncio.to_thread(_mqtt_disconnect, client)
            if self._mqtt_client is client:
                self._mqtt_client = None

    def _handle_mqtt_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        attrs_by_mesh = _parse_mqtt_state_payload(message.payload)
        if not attrs_by_mesh or not self._ws_message_callback or not self._mqtt_loop:
            return
        mesh_to_device = {
            str(device.get("mesh_id") or device.get("mac")): device
            for device in self._mqtt_devices
            if device.get("mesh_id") or device.get("mac")
        }
        for mesh_id, attrs in attrs_by_mesh.items():
            device = mesh_to_device.get(mesh_id)
            if not device or not device.get("did"):
                continue
            callback = self._ws_message_callback(str(device["did"]), attrs)
            future = asyncio.run_coroutine_threadsafe(callback, self._mqtt_loop)
            future.add_done_callback(_log_mqtt_callback_error)

    async def datapoint(self, product_key: str) -> dict[str, Any] | None:
        try:
            return await self._request(
                "GET",
                "/v2/datapoint",
                query={"product_key": product_key, "format": "json"},
                host=SITE_HOST,
            )
        except FGCAirError:
            return None

    async def control(self, did: str, attrs: dict[str, Any]) -> Any:
        await self.ensure_session()
        if not self.session:
            raise FGCAirAuthError("尚未登录")
        try:
            result = await self._request("POST", f"/app/control/{did}", token=self.session.token, body={"attrs": attrs})
            _LOGGER.info("FGCAir control accepted did=%s attrs=%s result=%s", did, attrs, result)
            return result
        except FGCAirAuthError:
            await self.ensure_session(force=True)
            result = await self._request("POST", f"/app/control/{did}", token=self.session.token, body={"attrs": attrs})
            _LOGGER.info("FGCAir control accepted after token refresh did=%s attrs=%s result=%s", did, attrs, result)
            return result

    async def control_sequence(self, did: str, attrs: dict[str, Any], delay: float = 0.35) -> list[Any]:
        results = []
        for key, value in attrs.items():
            results.append(await self.control(did, {key: value}))
            await asyncio.sleep(delay)
        return results

    async def latest(self, did: str) -> dict[str, Any]:
        await self.ensure_session()
        if not self.session:
            raise FGCAirAuthError("尚未登录")
        return await self._request("GET", f"/app/devdata/{did}/latest", token=self.session.token)


def _log_mqtt_callback_error(future: Any) -> None:
    try:
        future.result()
    except Exception as exc:  # pragma: no cover - defensive logging for background MQTT callbacks.
        _LOGGER.warning("FGCAir MQTT state callback failed: %s", exc)


def _mqtt_client_id(uid: str) -> str:
    cleaned = "".join(ch for ch in uid if ch in _MQTT_CLIENT_ID_ALPHABET)
    if cleaned and all(ch in "0123456789abcdefABCDEF" for ch in cleaned) and len(cleaned) >= 16:
        value = int(cleaned, 16)
        suffix = ""
        while value:
            value, index = divmod(value, len(_MQTT_CLIENT_ID_ALPHABET))
            suffix = _MQTT_CLIENT_ID_ALPHABET[index] + suffix
        cleaned = suffix or "0"
    return f"usr{cleaned[-20:].rjust(20, '0')}"


def _build_mqtt_client(client_id: str, uid: str, token: str, on_message: Callable[[Any, Any, Any], None]) -> Any:
    if mqtt is None:
        raise FGCAirError("paho-mqtt 未安装")
    client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv31)
    client.username_pw_set(f"2${APP_ID}${uid}", token)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.on_message = on_message
    return client


def _mqtt_connect(client: Any, host: str, port: int, gateway_did: str, client_id: str) -> None:
    connected = threading.Event()
    error: dict[str, int] = {}

    def on_connect(_client: Any, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc != 0:
            error["rc"] = rc
        connected.set()

    client.on_connect = on_connect
    client.connect(host, port, keepalive=15)
    client.loop_start()
    if not connected.wait(20):
        _mqtt_disconnect(client)
        raise FGCAirError("MQTT 连接超时")
    if error:
        _mqtt_disconnect(client)
        if error["rc"] in (4, 5):
            raise FGCAirAuthError(f"MQTT 认证失败 rc={error['rc']}")
        raise FGCAirError(f"MQTT 连接失败 rc={error['rc']}")
    client.subscribe(f"ser2cli_res/{client_id}/#", qos=0)
    client.subscribe(f"dev2app/{gateway_did}", qos=0)
    client.subscribe(f"dev2app/{gateway_did}/{client_id}", qos=0)


def _mqtt_disconnect(client: Any) -> None:
    try:
        client.disconnect()
    finally:
        client.loop_stop()


def _gateway_device_for(devices: list[dict[str, Any]], bindings: list[dict[str, Any]]) -> dict[str, Any] | None:
    gw_dids = {str(device.get("gw_did")) for device in devices if device.get("gw_did")}
    for device in bindings:
        if str(device.get("did")) in gw_dids:
            return device
    for device in bindings:
        if device.get("type") == "gateway":
            return device
    for device in devices:
        if device.get("gw_did"):
            return {
                "did": device.get("gw_did"),
                "host": device.get("host"),
                "port_s": device.get("port_s"),
                "type": "gateway",
            }
    return None


def _mqtt_query_payload(device: dict[str, Any]) -> bytes | None:
    mesh_id = str(device.get("mesh_id") or device.get("mac") or "")
    if not mesh_id:
        return None
    return bytes.fromhex("00 00 00 03 20 00 00 90 72 00 01 12") + mesh_id.encode("ascii") + bytes.fromhex("00 05 12 ff ff ff ff")


def _parse_mqtt_state_payload(payload: bytes) -> dict[str, dict[str, Any]]:
    if len(payload) < 33 or payload[:4] != b"\x00\x00\x00\x03":
        return {}
    if payload[4] == 0x28:
        return _parse_mqtt_short_payload(payload)
    if payload[4:6] != b"\xad\x01":
        return {}
    mesh_len = payload[12]
    mesh_start = 13
    mesh_end = mesh_start + mesh_len
    if mesh_len <= 0 or len(payload) < mesh_end + 31:
        return {}
    try:
        mesh_id = payload[mesh_start:mesh_end].decode("ascii")
    except UnicodeDecodeError:
        return {}
    body = payload[mesh_end:]
    attrs: dict[str, Any] = {}
    mode_flags = body[8]
    power = body[9]
    room_temp = body[12]
    target_temp = body[10] / 2 if 32 <= body[10] <= 60 else None
    mode = _mqtt_mode_from_flags(mode_flags)
    speed = body[7] * 2 + (1 if mode_flags & 0x80 else 0)
    if mode is not None:
        attrs["Mode_indoor_PK4"] = mode
    if 0 <= speed <= 6:
        attrs["Speed_indoor_PK4"] = speed
    if power in (0, 1, 0x80, 0x81):
        attrs["Power_indoor_PK4"] = bool(power & 0x01)
    if target_temp is not None:
        attrs["Temp_indoor_PK4"] = target_temp
    if room_temp:
        attrs["Roomtemp_indoor_PK4"] = room_temp
    if not attrs:
        return {}
    return {mesh_id: attrs}


def _parse_mqtt_short_payload(payload: bytes) -> dict[str, dict[str, Any]]:
    if len(payload) < 38 or payload[11] != 0x12:
        return {}
    try:
        mesh_id = payload[12:30].decode("ascii")
    except UnicodeDecodeError:
        return {}
    selector = payload[35]
    value = payload[37]
    attrs: dict[str, Any] = {}
    if selector == 0x08:
        mode = _mqtt_mode_from_code(value)
        if mode is not None:
            attrs["Mode_indoor_PK4"] = mode
    elif selector == 0x10 and 0 <= value <= 6:
        attrs["Speed_indoor_PK4"] = value
    return {mesh_id: attrs} if attrs else {}


def _mqtt_mode_from_flags(mode_flags: int) -> int | None:
    return _mqtt_mode_from_code((mode_flags & 0x70) >> 4)


def _mqtt_mode_from_code(mode_code: int) -> int | None:
    return mode_code if mode_code in (1, 2, 4) else None


def indoor_index(device: dict[str, Any]) -> int | None:
    mesh_id = str(device.get("mesh_id") or device.get("mac") or "")
    if len(mesh_id) >= 6 and mesh_id[-6:-2] == INDOOR_MESH_PREFIX:
        try:
            return int(mesh_id[-2:], 16) + 1
        except ValueError:
            return None
    return None


def indoor_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [device for device in devices if _is_indoor_device(device)]


def _is_indoor_device(device: dict[str, Any]) -> bool:
    if indoor_index(device) is not None:
        return True
    product_name = str(device.get("product_name") or "")
    return "室内机" in product_name


def state_attrs_from_cache(cache: dict[str, Any], did: str) -> dict[str, Any]:
    entry = cache.get(did, {}) if isinstance(cache, dict) else {}
    attrs = entry.get("attrs", {}) if isinstance(entry, dict) else {}
    return attrs if isinstance(attrs, dict) else {}


def merge_state_cache(cache: dict[str, Any], device: dict[str, Any], attrs: dict[str, Any]) -> dict[str, Any]:
    did = str(device.get("did") or "")
    entry = cache.get(did, {}) if isinstance(cache.get(did), dict) else {}
    old_attrs = entry.get("attrs", {}) if isinstance(entry.get("attrs"), dict) else {}
    old_attrs.update(attrs)
    cache[did] = {
        "did": did,
        "mac": device.get("mac"),
        "index": indoor_index(device),
        "attrs": old_attrs,
        "updated_at": int(time.time()),
    }
    return cache
