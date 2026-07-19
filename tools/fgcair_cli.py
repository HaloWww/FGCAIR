#!/usr/bin/env python3
"""FGCAir/机智云命令行工具。

HTTP 接口和请求头来自当前目录的 HAR 抓包；设备控制通过机智云 MQTT 通道发送。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


APP_ID = "56f717d9c96145a3a517d96c0e35853e"
DEFAULT_API_BASE = "http://115.190.119.84"
DEFAULT_API_HOST = "api.fgcawx.com"
DEFAULT_SITE_HOST = "site.fgcawx.com"
DEFAULT_USERNAME = ""
DEFAULT_PASSWORD = ""
DEFAULT_TOKEN_CACHE = ".fgcair_token.json"
DEFAULT_STATE_CACHE = ".fgcair_state.json"
DEFAULT_MQTT_CACHE = ".fgcair_mqtt_cache.json"
MQTT_CLIENT_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

MODE_MAP = {
    "auto": 0,
    "cool": 1,
    "dry": 2,
    "fan": 3,
    "heat": 4,
    "自动": 0,
    "制冷": 1,
    "除湿": 2,
    "通风": 3,
    "制热": 4,
}

SPEED_MAP = {
    "auto": 0,
    "lowest": 1,
    "low": 2,
    "mid": 3,
    "medium": 3,
    "mid": 3,
    "high": 4,
    "mid_high": 5,
    "midhigh": 5,
    "highest": 6,
    "strong": 6,
    "自动": 0,
    "超低": 1,
    "低档": 2,
    "低速": 2,
    "中速": 3,
    "高档": 4,
    "高速": 4,
    "中高": 5,
    "超高": 6,
    "强劲": 6,
}

MODE_NAME = {0: "自动", 1: "制冷", 2: "除湿", 3: "通风", 4: "制热"}
SPEED_NAME = {0: "自动", 1: "超低", 2: "低档", 3: "中档", 4: "高档", 5: "中高", 6: "超高"}
KNOWN_INDOOR_DIDS: dict[int, str] = {}


class FgcairError(RuntimeError):
    pass


@dataclass
class Session:
    uid: str
    token: str


class FgcairHttpClient:
    def __init__(
        self,
        base_url: str,
        app_id: str,
        host_header: str | None = DEFAULT_API_HOST,
        site_host_header: str | None = DEFAULT_SITE_HOST,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.host_header = host_header
        self.site_host_header = site_host_header

    def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        host_header: str | None = None,
    ) -> Any:
        if query:
            path = f"{path}?{urllib.parse.urlencode(query)}"
        url = f"{self.base_url}{path}"
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "GizWifiSDK (v21.23030112)",
            "X-Gizwits-Application-Id": self.app_id,
            "language": "zh-CN",
        }
        effective_host = self.host_header if host_header is None else host_header
        if effective_host:
            headers["Host"] = effective_host
        if token:
            headers["X-Gizwits-User-token"] = token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FgcairError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise FgcairError(f"HTTP 请求失败: {exc}") from exc
        return json.loads(raw) if raw else None

    def login(self, username: str, password: str) -> Session:
        payloads = [
            {"username": username, "password": password, "lang": "zh-cn"},
            {"phone": username, "password": password, "lang": "zh-cn"},
        ]
        last_error: Exception | None = None
        for payload in payloads:
            try:
                data = self._request("POST", "/app/login", body=payload)
                uid = data.get("uid") or data.get("user_id")
                token = data.get("token")
                if not uid or not token:
                    raise FgcairError(f"登录响应缺少 uid/token: {data}")
                return Session(uid=str(uid), token=str(token))
            except FgcairError as exc:
                last_error = exc
        raise FgcairError(f"登录失败: {last_error}")

    def list_bindings(self, token: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/app/bindings",
            token=token,
            query={"show_disabled": 0, "limit": 200, "skip": 0},
        )
        return list(data.get("devices", []))

    def bind_devices(self, token: str, devices: list[dict[str, str]]) -> Any:
        return self._request(
            "POST",
            "/app/bindings",
            token=token,
            body={"devices": devices},
        )

    def bind_device(self, token: str, did: str, passcode: str, remark: str = "") -> Any:
        return self.bind_devices(token, [{"did": did, "passcode": passcode, "remark": remark}])

    def control_device(self, token: str, did: str, attrs: dict[str, Any]) -> Any:
        return self._request("POST", f"/app/control/{did}", token=token, body={"attrs": attrs})

    def latest_devdata(self, token: str, did: str) -> Any:
        return self._request("GET", f"/app/devdata/{did}/latest", token=token)

    def datapoint(self, product_key: str) -> dict[str, Any] | None:
        try:
            return self._request(
                "GET",
                "/v2/datapoint",
                query={"product_key": product_key, "format": "json"},
                host_header=self.site_host_header,
            )
        except FgcairError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise


def indoor_pk_from_device(device: dict[str, Any]) -> int | None:
    mac = str(device.get("mac") or "")
    if len(mac) >= 6 and mac[-6:-4] == "04":
        try:
            return int(mac[-2:], 16) + 1
        except ValueError:
            return None
    return None


def bool_arg(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "on", "yes", "y", "开", "开启"}:
        return True
    if lowered in {"0", "false", "off", "no", "n", "关", "关闭"}:
        return False
    raise argparse.ArgumentTypeError(f"不是有效的开关值: {value}")


def enum_value(value: str, mapping: dict[str, int]) -> int:
    lowered = value.strip().lower()
    if lowered.isdigit():
        return int(lowered)
    if value in mapping:
        return mapping[value]
    if lowered in mapping:
        return mapping[lowered]
    allowed = ", ".join(sorted(k for k in mapping if k.isascii()))
    raise argparse.ArgumentTypeError(f"未知枚举值 {value!r}；可用值: {allowed}，也可以直接传数字")


def temp_to_control_value(celsius: float) -> int:
    # 私有云 /app/control 接口按 ac_control.py 的可行逻辑直接接收摄氏度整数。
    value = int(round(celsius))
    if value < 18 or value > 30:
        raise argparse.ArgumentTypeError("温度必须在 18.0 到 30.0 摄氏度之间")
    return value


def select_device(devices: list[dict[str, Any]], did: str | None, mac: str | None, index: int | None) -> dict[str, Any]:
    candidates = devices
    if did:
        candidates = [d for d in candidates if d.get("did") == did]
    if mac:
        candidates = [d for d in candidates if str(d.get("mac", "")).lower() == mac.lower()]
    if index is not None:
        known_did = KNOWN_INDOOR_DIDS.get(index)
        if known_did:
            candidates = [d for d in candidates if d.get("did") == known_did]
        else:
            indoor = [d for d in candidates if indoor_pk_from_device(d) is not None]
            candidates = [d for d in indoor if indoor_pk_from_device(d) == index]
    if not candidates:
        raise FgcairError("没有匹配的设备")
    if len(candidates) > 1:
        raise FgcairError("匹配到多台设备，请使用 --did、--mac 或 --index 精确选择")
    return candidates[0]


def product_indoor_pk_index(datapoint: dict[str, Any] | None) -> int | None:
    if not datapoint:
        return None
    for entity in datapoint.get("entities", []):
        for attr in entity.get("attrs", []):
            name = str(attr.get("name") or "")
            match = re.fullmatch(r"Power_indoor_PK(\d+)", name)
            if match:
                return int(match.group(1))
    return None


def captured_devices_from_har(har_path: str, all_devices: bool) -> list[dict[str, str]]:
    data = json.loads(Path(har_path).read_text(encoding="utf-8"))
    entries = data.get("log", {}).get("entries", [])
    found: list[dict[str, Any]] = []
    for entry in entries:
        request = entry.get("request", {})
        response = entry.get("response", {})
        if "/app/bindings" not in request.get("url", ""):
            continue
        text = response.get("content", {}).get("text")
        if not text:
            continue
        try:
            devices = json.loads(text).get("devices", [])
        except json.JSONDecodeError:
            continue
        if devices:
            found = devices
            break
    if not found:
        raise FgcairError(f"HAR 中没有找到绑定设备: {har_path}")
    if not all_devices:
        gateways = [d for d in found if d.get("type") == "gateway" and d.get("did") and d.get("passcode")]
        if not gateways:
            raise FgcairError("HAR 中有设备，但没有包含 did/passcode 的网关")
        found = gateways[:1]
    result = []
    for device in found:
        did = device.get("did")
        passcode = device.get("passcode")
        if did and passcode:
            result.append({"did": str(did), "passcode": str(passcode), "remark": str(device.get("remark") or "")})
    if not result:
        raise FgcairError("HAR 设备中没有 did/passcode")
    return result


def make_control_attrs(args: argparse.Namespace, device: dict[str, Any], product_pk_index: int | None = None) -> dict[str, Any]:
    pk_index = args.pk_index or product_pk_index
    if pk_index is None:
        raise FgcairError("无法推断产品点位 PK 后缀，请传 --pk-index")
    suffix = f"_indoor_PK{pk_index}"
    attrs: dict[str, Any] = {}
    if args.power is not None:
        attrs[f"Power{suffix}"] = args.power
    if args.energy_save is not None:
        attrs[f"Energy_save{suffix}"] = args.energy_save
    if args.lock is not None:
        attrs[f"Lock{suffix}"] = args.lock
    if args.mode is not None:
        attrs[f"Mode{suffix}"] = enum_value(args.mode, MODE_MAP)
    if args.speed is not None:
        attrs[f"Speed{suffix}"] = enum_value(args.speed, SPEED_MAP)
    if args.temp is not None:
        attrs[f"Temp{suffix}"] = int(args.temp) if args.raw_temp else temp_to_control_value(float(args.temp))
    for item in args.attr or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise FgcairError(f"无效 --attr {item!r}，格式应为 name=value")
        attrs[key] = parse_scalar(value)
    if not attrs:
        raise FgcairError("没有提供任何控制参数")
    return attrs


def build_control_payload(device: dict[str, Any], attrs: dict[str, Any], payload_format: str) -> dict[str, Any]:
    did = str(device["did"])
    mac = str(device.get("mac") or "")
    product_key = str(device["product_key"])
    sn = int(time.time()) & 0x7FFFFFFF
    if payload_format == "sdk":
        return {"cmd": 0x40B, "sn": sn, "did": did, "mac": mac, "productKey": product_key, "data": attrs}
    if payload_format == "attrs":
        return {"cmd": 1, "data": {"attrs": attrs}}
    if payload_format == "entity0":
        return {"cmd": 1, "entity0": attrs}
    raise FgcairError(f"未知控制 payload 格式: {payload_format}")


def build_status_payload(device: dict[str, Any], attrs: list[str] | None, payload_format: str) -> dict[str, Any]:
    did = str(device["did"])
    mac = str(device.get("mac") or "")
    product_key = str(device["product_key"])
    sn = int(time.time()) & 0x7FFFFFFF
    if payload_format == "sdk":
        return {"cmd": 0x409, "sn": sn, "did": did, "mac": mac, "productKey": product_key, "attrs": attrs}
    return {"cmd": 2, "data": {"attrs": attrs or []}}


def mqtt_topics(device: dict[str, Any], topic_mode: str) -> tuple[str, str]:
    product_key = str(device["product_key"])
    did = str(device["did"])
    if topic_mode == "pk_did":
        return f"app2dev/{product_key}/{did}", f"dev2app/{product_key}/{did}"
    if topic_mode == "did":
        return f"app2dev/{did}", f"dev2app/{did}"
    raise FgcairError(f"未知 MQTT topic 模式: {topic_mode}")


def mqtt_auth_candidates(auth_mode: str) -> list[str]:
    if auth_mode == "auto":
        return ["uid_token", "appid_token", "uid_amp_token"]
    return [auth_mode]


def mqtt_endpoint_candidates(device: dict[str, Any], mqtt_port: int | None, mqtt_tls: str, mqtt_transport: str) -> list[tuple[int, bool, str]]:
    normal_port = int(mqtt_port or device.get("port") or 1883)
    tls_port = int(mqtt_port or device.get("port_s") or 8883)
    ws_port = int(mqtt_port or device.get("ws_port") or 8080)
    wss_port = int(mqtt_port or device.get("wss_port") or 8880)
    endpoints: list[tuple[int, bool, str]] = []
    if mqtt_transport in {"auto", "tcp"}:
        if mqtt_tls in {"auto", "off"}:
            endpoints.append((normal_port, False, "tcp"))
        if mqtt_tls in {"auto", "on"} and tls_port != normal_port:
            endpoints.append((tls_port, True, "tcp"))
    if mqtt_transport in {"auto", "ws"}:
        if mqtt_tls in {"auto", "off"}:
            endpoints.append((ws_port, False, "websockets"))
        if mqtt_tls in {"auto", "on"} and wss_port != ws_port:
            endpoints.append((wss_port, True, "websockets"))
    return endpoints


def mqtt_sub_topics(device: dict[str, Any], topic_mode: str) -> list[str]:
    product_key = str(device["product_key"])
    did = str(device["did"])
    mac = str(device.get("mac") or "")
    if topic_mode != "auto":
        return [mqtt_topics(device, topic_mode)[1]]
    return [
        f"dev2app/{product_key}/{did}",
        f"dev2app/{did}",
        f"ser2cli_res/{product_key}/{did}",
        f"ser2cli_res/{did}",
        f"ser2cli/{product_key}/{did}",
        f"ser2cli/{did}",
        f"app2dev/{product_key}/{did}",
        f"app2dev/{did}",
        f"dev2app/{product_key}/{mac}",
        f"ser2cli_res/{product_key}/{mac}",
    ]


def token_expired_error(exc: Exception) -> bool:
    text = str(exc)
    lowered = text.lower()
    return (
        "token expired" in lowered
        or "token invalid" in lowered
        or '"error_code":9004' in text
        or '"error_code":"9004"' in text
        or '"error_code":9006' in text
        or '"error_code":"9006"' in text
    )


def load_cached_session(cache_path: str, username: str, app_id: str) -> Session | None:
    path = Path(cache_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("username") != username or data.get("app_id") != app_id:
        return None
    uid = data.get("uid")
    token = data.get("token")
    if not uid or not token:
        return None
    return Session(uid=str(uid), token=str(token))


def save_cached_session(cache_path: str, username: str, app_id: str, session: Session) -> None:
    path = Path(cache_path)
    data = {
        "username": username,
        "app_id": app_id,
        "uid": session.uid,
        "token": session.token,
        "saved_at": int(time.time()),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_mqtt_cache(cache_path: str, username: str, app_id: str) -> tuple[Session, list[dict[str, Any]]] | None:
    path = Path(cache_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("username") != username or data.get("app_id") != app_id:
        return None
    session_data = data.get("session")
    devices = data.get("devices")
    if not isinstance(session_data, dict) or not isinstance(devices, list):
        return None
    uid = session_data.get("uid")
    token = session_data.get("token")
    if not uid or not token:
        return None
    return Session(str(uid), str(token)), [device for device in devices if isinstance(device, dict)]


def save_mqtt_cache(cache_path: str, username: str, app_id: str, session: Session, devices: list[dict[str, Any]]) -> None:
    data = {
        "username": username,
        "app_id": app_id,
        "session": {"uid": session.uid, "token": session.token},
        "devices": devices,
        "saved_at": int(time.time()),
    }
    Path(cache_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_mqtt_cache(client: FgcairHttpClient, args: argparse.Namespace) -> tuple[Session, list[dict[str, Any]]]:
    session = get_session(client, args)
    devices = client.list_bindings(session.token)
    save_mqtt_cache(args.mqtt_cache, args.username, args.app_id, session, devices)
    return session, devices


def load_state_cache(cache_path: str) -> dict[str, Any]:
    path = Path(cache_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state_cache(cache_path: str, data: dict[str, Any]) -> None:
    Path(cache_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_control_state(cache_path: str, device: dict[str, Any], attrs: dict[str, Any]) -> None:
    did = str(device.get("did") or "")
    if not did:
        return
    cache = load_state_cache(cache_path)
    entry = cache.get(did, {}) if isinstance(cache.get(did), dict) else {}
    old_attrs = entry.get("attrs", {}) if isinstance(entry.get("attrs"), dict) else {}
    old_attrs.update(attrs)
    cache[did] = {
        "did": did,
        "mac": device.get("mac"),
        "index": next((idx for idx, known_did in KNOWN_INDOOR_DIDS.items() if known_did == did), indoor_pk_from_device(device)),
        "attrs": old_attrs,
        "updated_at": int(time.time()),
        "source": "本地控制缓存",
    }
    save_state_cache(cache_path, cache)


def control_device_sequence(client: FgcairHttpClient, token: str, did: str, attrs: dict[str, Any], delay: float = 0.35) -> list[Any]:
    results = []
    for key, value in attrs.items():
        results.append(client.control_device(token, did, {key: value}))
        time.sleep(delay)
    return results


def mqtt_app_client_id(uid: str, unique: bool = False) -> str:
    cleaned = "".join(ch for ch in uid if ch in MQTT_CLIENT_ID_ALPHABET)
    if cleaned and all(ch in "0123456789abcdefABCDEF" for ch in cleaned) and len(cleaned) >= 16:
        value = int(cleaned, 16)
        suffix = ""
        while value:
            value, index = divmod(value, len(MQTT_CLIENT_ID_ALPHABET))
            suffix = MQTT_CLIENT_ID_ALPHABET[index] + suffix
        cleaned = suffix or "0"
    client_id = f"usr{cleaned[-20:].rjust(20, '0')}"
    if unique:
        return f"{client_id[:-4]}{time.time_ns() % 10000:04d}"
    return client_id


def mqtt_gateway_device(devices: list[dict[str, Any]], selected_devices: list[dict[str, Any]]) -> dict[str, Any]:
    gw_dids = {str(device.get("gw_did")) for device in selected_devices if device.get("gw_did")}
    for device in devices:
        if str(device.get("did")) in gw_dids:
            return device
    for device in devices:
        if device.get("type") == "gateway":
            return device
    for device in selected_devices:
        if device.get("gw_did"):
            return {
                "did": device.get("gw_did"),
                "host": device.get("host"),
                "port_s": device.get("port_s"),
                "type": "gateway",
            }
    raise FgcairError("找不到网关设备")


def mqtt_query_payload(device: dict[str, Any]) -> bytes | None:
    mesh_id = str(device.get("mesh_id") or device.get("mac") or "")
    if not mesh_id:
        return None
    return bytes.fromhex("00 00 00 03 20 00 00 90 72 00 01 12") + mesh_id.encode("ascii") + bytes.fromhex("00 05 12 ff ff ff ff")


def parse_mqtt_payload(payload: bytes) -> tuple[str, dict[str, Any], bytes] | None:
    if len(payload) < 33 or payload[:4] != b"\x00\x00\x00\x03" or payload[4:6] != b"\xad\x01":
        return None
    mesh_len = payload[12]
    mesh_start = 13
    mesh_end = mesh_start + mesh_len
    if mesh_len <= 0 or len(payload) < mesh_end + 31:
        return None
    try:
        mesh_id = payload[mesh_start:mesh_end].decode("ascii")
    except UnicodeDecodeError:
        return None
    body = payload[mesh_end:]
    room_raw = body[12]
    power = body[9]
    mode_flags = body[8]
    mode_raw = mode_flags & 0x7F
    mode_code = (mode_flags & 0x70) >> 4
    speed = body[7] * 2 + (1 if mode_flags & 0x80 else 0)
    state = {
        "power": bool(power & 0x01),
        "mode_raw": mode_raw,
        "mode_code": mode_code,
        "mode": mqtt_mode_name(mode_code),
        "speed": speed if 0 <= speed <= 6 else None,
        "speed_raw": speed,
        "target": body[10] / 2 if 32 <= body[10] <= 60 else None,
        "room": round(room_raw * 0.5 - 75, 1) if room_raw else None,
        "room_raw": room_raw,
    }
    return mesh_id, state, body


def parse_mqtt_short_payload(payload: bytes) -> tuple[str, dict[str, Any], bytes] | None:
    if len(payload) < 38 or payload[:4] != b"\x00\x00\x00\x03" or payload[4] != 0x28 or payload[11] != 0x12:
        return None
    try:
        mesh_id = payload[12:30].decode("ascii")
    except UnicodeDecodeError:
        return None
    tail = payload[30:]
    selector = payload[35]
    value = payload[37]
    state: dict[str, Any] = {"short_selector": selector, "short_value": value}
    if selector == 0x10:
        state.update({"speed": value if 0 <= value <= 6 else None, "speed_raw": value})
    elif selector == 0x08:
        state.update({"mode_code": value, "mode": mqtt_mode_name(value)})
    return mesh_id, state, tail


def mqtt_mode_name(mode_code: int | None) -> str | None:
    if mode_code in (1, 0x10, 0x14):
        return "cool"
    if mode_code in (2, 0x20, 0x24):
        return "dry"
    if mode_code in (4, 0x40, 0x44):
        return "heat"
    return None


def mqtt_speed_name(speed: Any) -> str | None:
    if speed is None:
        return None
    return "auto" if speed == 0 else str(speed)


def indexed_bytes(data: bytes) -> list[dict[str, Any]]:
    return [{"index": index, "hex": f"{value:02x}", "dec": value} for index, value in enumerate(data)]


def changed_bytes(previous: bytes | None, current: bytes) -> list[dict[str, Any]] | None:
    if previous is None:
        return None
    changes: list[dict[str, Any]] = []
    for index in range(max(len(previous), len(current))):
        old = previous[index] if index < len(previous) else None
        new = current[index] if index < len(current) else None
        if old != new:
            changes.append(
                {
                    "index": index,
                    "from_hex": None if old is None else f"{old:02x}",
                    "from_dec": old,
                    "to_hex": None if new is None else f"{new:02x}",
                    "to_dec": new,
                }
            )
    return changes


def monitor_device_summary(device: dict[str, Any] | None) -> dict[str, Any] | None:
    if not device:
        return None
    did = str(device.get("did") or "")
    return {
        "index": next((idx for idx, known_did in KNOWN_INDOOR_DIDS.items() if known_did == did), indoor_pk_from_device(device)),
        "did": did or None,
        "mac": device.get("mac"),
        "mesh_id": device.get("mesh_id") or device.get("mac"),
        "alias": device.get("dev_alias") or device.get("remark"),
        "product_name": device.get("product_name"),
    }


def monitor_timestamp() -> dict[str, Any]:
    now = time.time()
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "epoch_ms": int(now * 1000)}


def format_monitor_value(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    return text[:width].ljust(width)


def print_monitor_table(devices: list[dict[str, Any]], state: dict[str, dict[str, Any]], seen: set[str], timestamp: str) -> None:
    print(f"\n{timestamp}")
    print("device    status   power mode  speed target room  mesh")
    print("--------  -------  ----- ----- ----- ------ ----- ------------------")
    for device in sorted(devices, key=lambda item: next((idx for idx, did in KNOWN_INDOOR_DIDS.items() if did == item.get("did")), 999)):
        index = next((idx for idx, did in KNOWN_INDOOR_DIDS.items() if did == device.get("did")), None)
        mesh_id = str(device.get("mesh_id") or device.get("mac") or "")
        item = state.get(mesh_id, {}) if mesh_id in seen else {}
        status = "ok" if mesh_id in seen else "empty"
        power = "on" if item.get("power") is True else "off" if item.get("power") is False else None
        print(
            f"{format_monitor_value(f'indoor-{index}', 8)}  {format_monitor_value(status, 7)}  "
            f"{format_monitor_value(power, 5)} "
            f"{format_monitor_value(item.get('mode'), 5)} "
            f"{format_monitor_value(mqtt_speed_name(item.get('speed')), 5)} "
            f"{format_monitor_value(item.get('target'), 6)} {format_monitor_value(item.get('room'), 5)} "
            f"{mesh_id}"
        )


def mqtt_monitor(
    devices: list[dict[str, Any]],
    selected_devices: list[dict[str, Any]],
    session: Session,
    interval: float,
    once: bool,
    timeout: float,
    settle: float,
    debug_fields: bool,
    raw: bool,
    record_file: str | None,
    no_table: bool,
    app_id: str,
) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise FgcairError("MQTT 监控需要安装 paho-mqtt: python -m pip install paho-mqtt") from exc

    gateway = mqtt_gateway_device(devices, selected_devices)
    host = str(gateway.get("host") or "m2m.fgcawx.com")
    port = int(gateway.get("port_s") or 8883)
    gateway_did = str(gateway["did"])
    client_id = mqtt_app_client_id(session.uid, unique=True)
    topic_pub = f"app2dev/{gateway_did}/{client_id}"
    connected = threading.Event()
    connect_error: dict[str, int] = {}
    state: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    round_id = 0
    record_lock = threading.Lock()
    record_handle = open(record_file, "a", encoding="utf-8") if record_file else None
    last_body_by_mesh: dict[str, bytes] = {}
    device_by_mesh = {str(device.get("mesh_id") or device.get("mac") or ""): device for device in selected_devices}

    def emit_record(record: dict[str, Any]) -> None:
        if not raw and record_handle is None:
            return
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with record_lock:
            if raw:
                print(f"MQTT_RECORD {line}", flush=True)
            if record_handle is not None:
                record_handle.write(line + "\n")
                record_handle.flush()

    mqtt_client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv31)
    mqtt_client.username_pw_set(f"2${app_id}${session.uid}", session.token)
    mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
    mqtt_client.tls_insecure_set(True)

    def on_connect(_client: Any, _userdata: Any, _flags: Any, rc: Any, *_extra: Any) -> None:
        code = int(getattr(rc, "value", rc))
        if code != 0:
            connect_error["rc"] = code
        connected.set()

    def on_message(_client: Any, _userdata: Any, msg: Any) -> None:
        parsed = parse_mqtt_payload(msg.payload)
        short_parsed = None if parsed else parse_mqtt_short_payload(msg.payload)
        record: dict[str, Any] = {
            "record_type": "mqtt_message",
            "direction": "incoming",
            **monitor_timestamp(),
            "round": round_id,
            "topic": msg.topic,
            "qos": getattr(msg, "qos", None),
            "retain": getattr(msg, "retain", None),
            "gateway_did": gateway_did,
            "client_id": client_id,
            "payload_len": len(msg.payload),
            "payload_hex": msg.payload.hex(" "),
        }
        if not parsed and not short_parsed:
            record["parsed"] = None
            emit_record(record)
            return
        if short_parsed:
            mesh_id, parsed_state, tail = short_parsed
            tail_start = len(msg.payload) - len(tail)
            selector = parsed_state.get("short_selector")
            value = parsed_state.get("short_value")
            message_type = "fgcair_speed_28" if selector == 0x10 else "fgcair_mode_28" if selector == 0x08 else "fgcair_short_28"
            record["parsed"] = {
                "message_type": message_type,
                "mesh_id": mesh_id,
                "device": monitor_device_summary(device_by_mesh.get(mesh_id)),
                "tail_start": tail_start,
                "tail_len": len(tail),
                "tail_hex": tail.hex(" "),
                "tail_bytes": indexed_bytes(tail),
                "known_fields": {
                    "selector_tail5_hex": None if selector is None else f"{selector:02x}",
                    "selector_tail5_dec": selector,
                    "value_tail7_hex": None if value is None else f"{value:02x}",
                    "value_tail7_dec": value,
                    "speed": parsed_state.get("speed"),
                    "speed_raw": parsed_state.get("speed_raw"),
                    "speed_name": mqtt_speed_name(parsed_state.get("speed")),
                    "mode_code": parsed_state.get("mode_code"),
                    "mode": parsed_state.get("mode"),
                },
            }
            emit_record(record)
            state.setdefault(mesh_id, {}).update(parsed_state)
            seen.add(mesh_id)
            return
        mesh_id, parsed_state, body = parsed
        previous_body = last_body_by_mesh.get(mesh_id)
        body_changes = changed_bytes(previous_body, body)
        last_body_by_mesh[mesh_id] = body
        body_start = len(msg.payload) - len(body)
        power_raw = body[9] if len(body) > 9 else None
        speed_high_raw = body[7] if len(body) > 7 else None
        mode_flags_raw = body[8] if len(body) > 8 else None
        target_raw = body[10] if len(body) > 10 else None
        room_raw = body[12] if len(body) > 12 else None
        record["parsed"] = {
            "message_type": "fgcair_state_ad01",
            "mesh_id": mesh_id,
            "mesh_len": msg.payload[12] if len(msg.payload) > 12 else None,
            "mesh_start": 13,
            "mesh_end": body_start,
            "device": monitor_device_summary(device_by_mesh.get(mesh_id)),
            "body_start": body_start,
            "body_len": len(body),
            "body_hex": body.hex(" "),
            "body_bytes": indexed_bytes(body),
            "known_fields": {
                "speed_high_b07": speed_high_raw,
                "mode_flags_b08_hex": None if mode_flags_raw is None else f"{mode_flags_raw:02x}",
                "mode_flags_b08_dec": mode_flags_raw,
                "speed_low_bit_from_b08": None if mode_flags_raw is None else bool(mode_flags_raw & 0x80),
                "speed_formula": "body[7] * 2 + ((body[8] & 0x80) != 0)",
                "speed": parsed_state.get("speed"),
                "speed_raw": parsed_state.get("speed_raw"),
                "speed_name": mqtt_speed_name(parsed_state.get("speed")),
                "mode_raw_b08_masked_hex": None if parsed_state.get("mode_raw") is None else f"{parsed_state['mode_raw']:02x}",
                "mode_raw_b08_masked_dec": parsed_state.get("mode_raw"),
                "mode_code_formula": "(body[8] & 0x70) >> 4",
                "mode_code": parsed_state.get("mode_code"),
                "mode": parsed_state.get("mode"),
                "power_flags_b09_hex": None if power_raw is None else f"{power_raw:02x}",
                "power_flags_b09_dec": power_raw,
                "power_bool": parsed_state.get("power"),
                "target_raw_b10": target_raw,
                "target_temperature": parsed_state.get("target"),
                "room_raw_b12": room_raw,
                "room_temperature": parsed_state.get("room"),
            },
            "body_changes_from_previous": body_changes,
        }
        emit_record(record)
        if debug_fields:
            fields = " ".join(f"b{i:02d}={byte:02x}" for i, byte in enumerate(body[:32]))
            print(f"FIELDS mesh={mesh_id} {fields}")
        state[mesh_id] = parsed_state
        seen.add(mesh_id)

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect(host, port, keepalive=15)
    mqtt_client.loop_start()
    if not connected.wait(20):
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        raise FgcairError("MQTT 连接超时")
    if connect_error:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        raise FgcairError(f"MQTT 连接失败 rc={connect_error['rc']}")
    mqtt_client.subscribe(f"ser2cli_res/{client_id}/#", qos=0)
    mqtt_client.subscribe(f"dev2app/{gateway_did}", qos=0)
    mqtt_client.subscribe(f"dev2app/{gateway_did}/{client_id}", qos=0)
    print(f"connected host={host} port={port} gateway={gateway_did} client_id={client_id}")
    try:
        while True:
            round_id += 1
            seen.clear()
            for device in selected_devices:
                payload = mqtt_query_payload(device)
                if payload:
                    emit_record(
                        {
                            "record_type": "mqtt_publish",
                            "direction": "outgoing",
                            **monitor_timestamp(),
                            "round": round_id,
                            "reason": "query_state",
                            "topic": topic_pub,
                            "qos": 0,
                            "gateway_did": gateway_did,
                            "client_id": client_id,
                            "device": monitor_device_summary(device),
                            "payload_len": len(payload),
                            "payload_hex": payload.hex(" "),
                        }
                    )
                    mqtt_client.publish(topic_pub, payload, qos=0)
            expected = {str(device.get("mesh_id") or device.get("mac")) for device in selected_devices}
            deadline = time.time() + (timeout if once else settle)
            while time.time() < deadline and not expected.issubset(seen):
                time.sleep(0.2)
            if not no_table:
                print_monitor_table(selected_devices, state, seen, time.strftime("%Y-%m-%d %H:%M:%S"))
            if once:
                break
            time.sleep(max(interval - settle, 0.0))
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        if record_handle is not None:
            record_handle.close()


def get_session(client: FgcairHttpClient, args: argparse.Namespace) -> Session:
    if args.token:
        return Session(uid=args.uid or "", token=args.token)
    if not args.no_token_cache:
        cached = load_cached_session(args.token_cache, args.username, args.app_id)
        if cached:
            try:
                client.list_bindings(cached.token)
                return cached
            except FgcairError as exc:
                if not token_expired_error(exc):
                    raise
    session = client.login(args.username, args.password)
    if not args.no_token_cache:
        save_cached_session(args.token_cache, args.username, args.app_id, session)
    return session


def parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def mqtt_publish(
    device: dict[str, Any],
    session: Session,
    app_id: str,
    attrs: dict[str, Any],
    qos: int,
    wait_seconds: float,
    auth_mode: str,
    payload_format: str,
    topic_mode: str,
    mqtt_port: int | None,
    mqtt_tls: str,
    mqtt_transport: str,
) -> list[dict[str, Any]]:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise FgcairError("控制设备需要安装 paho-mqtt: python -m pip install paho-mqtt") from exc

    host = str(device.get("host") or "m2m.fgcawx.com")
    endpoints = mqtt_endpoint_candidates(device, mqtt_port, mqtt_tls, mqtt_transport)
    topic_pub, topic_sub = mqtt_topics(device, topic_mode)
    payload = build_control_payload(device, attrs, payload_format)
    last_error: str | None = None

    for port, use_tls, transport in endpoints:
        for candidate in mqtt_auth_candidates(auth_mode):
            responses: list[dict[str, Any]] = []
            connected = {"ok": False, "rc": None}
            client_id = f"app:{app_id}:{session.uid}:{int(time.time())}"
            client = mqtt.Client(client_id=client_id, transport=transport)
            if use_tls:
                client.tls_set(cert_reqs=ssl.CERT_NONE)
                client.tls_insecure_set(True)
            if candidate == "uid_token":
                client.username_pw_set(session.uid, session.token)
            elif candidate == "appid_token":
                client.username_pw_set(app_id, session.token)
            elif candidate == "uid_amp_token":
                client.username_pw_set(f"{session.uid}&{session.token}", session.token)
            else:
                raise FgcairError(f"未知 MQTT 鉴权模式: {candidate}")

            def on_connect(_client: Any, _userdata: Any, _flags: Any, rc: Any, *_extra: Any) -> None:
                code = int(getattr(rc, "value", rc))
                connected["ok"] = code == 0
                connected["rc"] = code

            def on_message(_client: Any, _userdata: Any, msg: Any) -> None:
                try:
                    responses.append({"topic": msg.topic, "data": json.loads(msg.payload.decode("utf-8"))})
                except Exception:
                    responses.append({"topic": msg.topic, "payload_hex": msg.payload.hex()})

            client.on_connect = on_connect
            client.on_message = on_message
            try:
                client.connect(host, port, keepalive=60)
                client.loop_start()
                deadline = time.time() + 8
                while connected["rc"] is None and time.time() < deadline:
                    time.sleep(0.1)
                if not connected["ok"]:
                    last_error = f"MQTT 连接失败，端口 {port}，TLS={use_tls}，传输={transport}，鉴权模式 {candidate}，返回码 {connected['rc']}"
                    client.loop_stop()
                    client.disconnect()
                    continue
                client.subscribe(topic_sub, qos=qos)
                info = client.publish(topic_pub, json.dumps(payload, separators=(",", ":")), qos=qos)
                info.wait_for_publish(timeout=10)
                time.sleep(wait_seconds)
                client.loop_stop()
                client.disconnect()
                return [{"auth_mode": candidate, "port": port, "tls": use_tls, "transport": transport, "topic_pub": topic_pub, "payload": payload, "replies": responses}]
            except Exception as exc:
                last_error = f"MQTT 发送失败，端口 {port}，TLS={use_tls}，传输={transport}，鉴权模式 {candidate}: {exc}"
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
                continue
    raise FgcairError(last_error or "MQTT 发送失败")


def extract_attrs_from_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    for key in ("attr", "attrs", "entity0"):
        value = message.get(key)
        if isinstance(value, dict):
            return value
    data = message.get("data")
    if isinstance(data, dict):
        for key in ("attr", "attrs", "entity0"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        if any(str(k).startswith(("Power_indoor_", "Mode_indoor_", "Speed_indoor_", "Temp_indoor_", "Roomtemp_indoor_")) for k in data):
            return data
    return {}


def mqtt_read_state(
    client: FgcairHttpClient,
    token: str,
    device: dict[str, Any],
    app_id: str,
    session: Session,
    auth_mode: str,
    topic_mode: str,
    mqtt_port: int | None,
    mqtt_tls: str,
    mqtt_transport: str,
    wait_seconds: float,
    query_attrs: dict[str, Any],
) -> dict[str, Any]:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise FgcairError("读取 MQTT 状态需要安装 paho-mqtt: python -m pip install paho-mqtt") from exc

    if mqtt_transport == "ws":
        return {"state_error": "当前 FGCAir 状态读取使用已验证的 8883 TLS TCP 协议，不支持 WebSocket"}
    if mqtt_tls == "off":
        return {"state_error": "当前 FGCAir 状态读取需要 TLS；请使用 --mqtt-tls auto 或 --mqtt-tls on"}

    payload = mqtt_query_payload(device)
    if not payload:
        return {"state_error": "设备缺少 mesh_id/mac，无法构造 MQTT 查询 payload"}

    gateway = mqtt_gateway_device([device], [device])
    host = str(gateway.get("host") or device.get("host") or "m2m.fgcawx.com")
    port = int(mqtt_port or gateway.get("port_s") or device.get("port_s") or 8883)
    gateway_did = str(gateway["did"])
    client_id = mqtt_app_client_id(session.uid, unique=True)
    topic_pub = f"app2dev/{gateway_did}/{client_id}"
    expected_mesh = str(device.get("mesh_id") or device.get("mac") or "")
    connected = threading.Event()
    found = threading.Event()
    connect_error: dict[str, int] = {}
    result: dict[str, Any] = {}

    mqtt_client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv31)
    mqtt_client.username_pw_set(f"2${app_id}${session.uid}", session.token)
    mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
    mqtt_client.tls_insecure_set(True)

    def on_connect(_client: Any, _userdata: Any, _flags: Any, rc: Any, *_extra: Any) -> None:
        code = int(getattr(rc, "value", rc))
        if code != 0:
            connect_error["rc"] = code
        connected.set()

    def on_message(_client: Any, _userdata: Any, msg: Any) -> None:
        parsed = parse_mqtt_payload(msg.payload)
        if not parsed:
            return
        mesh_id, parsed_state, body = parsed
        if mesh_id != expected_mesh:
            return
        power = parsed_state.get("power")
        result.update(
            {
                "state_source": "MQTT",
                "mqtt_port": port,
                "mqtt_tls": True,
                "mqtt_transport": "tcp",
                "mqtt_client_id": client_id,
                "mqtt_topic": msg.topic,
                "mesh": mesh_id,
                "power": "on" if power is True else "off" if power is False else None,
                "power_bool": power,
                "temperature": parsed_state.get("target"),
                "room_temperature": parsed_state.get("room"),
                "room_temperature_raw": parsed_state.get("room_raw"),
                "raw_body_prefix": body[:32].hex(" "),
            }
        )
        found.set()

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(host, port, keepalive=15)
        mqtt_client.loop_start()
        connect_wait = max(8.0, min(wait_seconds, 20.0))
        if not connected.wait(connect_wait):
            return {"state_error": f"MQTT 连接超时: host={host} port={port} TLS=True transport=tcp"}
        if connect_error:
            return {"state_error": f"MQTT 连接失败 rc={connect_error['rc']}: host={host} port={port} TLS=True transport=tcp"}
        mqtt_client.subscribe(f"ser2cli_res/{client_id}/#", qos=0)
        mqtt_client.subscribe(f"dev2app/{gateway_did}", qos=0)
        mqtt_client.subscribe(f"dev2app/{gateway_did}/{client_id}", qos=0)
        info = mqtt_client.publish(topic_pub, payload, qos=0)
        info.wait_for_publish(timeout=10)
        if found.wait(wait_seconds):
            return result
        return {
            "state_note": "MQTT 已连接但等待期间未收到目标室内机状态消息",
            "state_source": "MQTT",
            "mqtt_port": port,
            "mqtt_tls": True,
            "mqtt_transport": "tcp",
            "mqtt_client_id": client_id,
            "mqtt_topic_pub": topic_pub,
            "mesh": expected_mesh,
        }
    except Exception as exc:
        return {"state_error": f"MQTT 状态读取失败: host={host} port={port} TLS=True transport=tcp: {exc}"}
    finally:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def find_attr_name(attrs: dict[str, Any], prefix: str) -> str | None:
    for key in attrs:
        if key.startswith(prefix):
            return key
    return None


def decode_device_state(attrs: dict[str, Any]) -> dict[str, Any]:
    power_key = find_attr_name(attrs, "Power_indoor_PK")
    mode_key = find_attr_name(attrs, "Mode_indoor_PK")
    speed_key = find_attr_name(attrs, "Speed_indoor_PK")
    temp_key = find_attr_name(attrs, "Temp_indoor_PK")
    room_key = find_attr_name(attrs, "Roomtemp_indoor_PK")
    error_key = find_attr_name(attrs, "Error_indoor_PK")
    state: dict[str, Any] = {}
    if power_key:
        value = attrs.get(power_key)
        state["power"] = "开" if value else "关" if value is not None else None
    if mode_key:
        value = attrs.get(mode_key)
        state["mode"] = MODE_NAME.get(value, value)
    if speed_key:
        value = attrs.get(speed_key)
        state["speed"] = SPEED_NAME.get(value, value)
    if temp_key:
        state["temperature"] = attrs.get(temp_key)
    if room_key:
        value = attrs.get(room_key)
        state["room_temperature"] = round(value * 0.5 - 75, 1) if isinstance(value, (int, float)) else value
    if error_key:
        value = attrs.get(error_key)
        state["error"] = "有" if value else "无" if value is not None else None
    return state


def query_and_read_state(
    client: FgcairHttpClient,
    session: Session,
    app_id: str,
    token: str,
    device: dict[str, Any],
    wait_seconds: float,
    state_source: str,
    state_cache_path: str,
    mqtt_auth: str = "auto",
    mqtt_topic_mode: str = "auto",
    mqtt_port: int | None = None,
    mqtt_tls: str = "auto",
    mqtt_transport: str = "auto",
) -> dict[str, Any]:
    did = str(device.get("did") or "")
    if state_source == "mqtt":
        return mqtt_read_state(
            client,
            token,
            device,
            app_id,
            session,
            mqtt_auth,
            mqtt_topic_mode,
            mqtt_port,
            mqtt_tls,
            mqtt_transport,
            wait_seconds,
            {},
        )
    product_key = str(device.get("product_key") or "")
    datapoint = client.datapoint(product_key) if product_key else None
    pk_index = product_indoor_pk_index(datapoint)
    query_attrs = {f"Query_indoor_PK{pk_index}": True} if pk_index is not None else {}
    if state_source in {"auto", "http"}:
        if query_attrs:
            try:
                client.control_device(token, did, query_attrs)
                time.sleep(wait_seconds)
            except FgcairError:
                pass
        try:
            latest = client.latest_devdata(token, did)
        except FgcairError as exc:
            if state_source == "http":
                return {"state_error": str(exc)}
        else:
            attrs = latest.get("attr", {}) if isinstance(latest, dict) else {}
            if isinstance(attrs, dict) and attrs:
                state = decode_device_state(attrs)
                state["state_source"] = "云端 HTTP 缓存"
                state["updated_at"] = latest.get("updated_at")
                return state
            if state_source == "http":
                return {"state_source": "云端 HTTP 缓存", "updated_at": latest.get("updated_at"), "state_note": "云端暂无缓存状态"}
    if state_source in {"auto", "cache"}:
        entry = load_state_cache(state_cache_path).get(did, {})
        attrs = entry.get("attrs", {}) if isinstance(entry, dict) else {}
        if isinstance(attrs, dict) and attrs:
            state = decode_device_state(attrs)
            state["state_source"] = entry.get("source") or "本地控制缓存"
            state["updated_at"] = entry.get("updated_at")
            return state
    return {"state_note": "暂无状态；请先用本脚本控制一次，或使用可接收 MQTT 推送的环境读取实时状态"}


def summarize_devices(
    client: FgcairHttpClient,
    session: Session,
    app_id: str,
    token: str,
    devices: list[dict[str, Any]],
    include_datapoints: bool,
    include_state: bool,
    state_wait: float,
    show_attrs: bool,
    state_source: str,
    state_cache_path: str,
    mqtt_auth: str = "auto",
    mqtt_topic_mode: str = "auto",
    mqtt_port: int | None = None,
    mqtt_tls: str = "auto",
    mqtt_transport: str = "auto",
) -> list[dict[str, Any]]:
    by_pk: dict[str, dict[str, Any] | None] = {}
    rows = []
    for device in devices:
        product_key = str(device.get("product_key") or "")
        datapoint = None
        if include_datapoints and product_key:
            if product_key not in by_pk:
                by_pk[product_key] = client.datapoint(product_key)
            datapoint = by_pk[product_key]
        attrs = []
        if datapoint:
            for entity in datapoint.get("entities", []):
                attrs.extend(entity.get("attrs", []))
        row = {
            "index": next((idx for idx, did in KNOWN_INDOOR_DIDS.items() if did == device.get("did")), indoor_pk_from_device(device)),
            "did": device.get("did"),
            "mac": device.get("mac"),
            "name": device.get("dev_alias") or device.get("product_name"),
            "type": device.get("type"),
            "online": device.get("is_online"),
            "gateway_did": device.get("gw_did"),
            "product_key": product_key,
        }
        writable = [a.get("name") for a in attrs if a.get("type") == "status_writable"]
        if show_attrs and writable:
            row["writable_attrs"] = writable
        is_known_indoor = device.get("did") in KNOWN_INDOOR_DIDS.values()
        if include_state and (is_known_indoor or product_indoor_pk_index(datapoint) is not None):
            row.update(
                query_and_read_state(
                    client,
                    session,
                    app_id,
                    token,
                    device,
                    state_wait,
                    state_source,
                    state_cache_path,
                    mqtt_auth,
                    mqtt_topic_mode,
                    mqtt_port,
                    mqtt_tls,
                    mqtt_transport,
                )
            )
        rows.append({k: v for k, v in row.items() if v not in (None, "", [])})
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FGCAir/机智云命令行工具：登录、绑定网关、获取设备列表和控制室内机。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="账号，也可用 FGCair/机智云账号")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="密码")
    parser.add_argument("--uid", help="直接指定已有 uid；配合 --token 使用，MQTT 控制需要 uid")
    parser.add_argument("--token", help="直接指定已有 token，跳过账号密码登录")
    parser.add_argument("--token-cache", default=DEFAULT_TOKEN_CACHE, help="token 缓存文件路径")
    parser.add_argument("--state-cache", default=DEFAULT_STATE_CACHE, help="本地设备状态缓存文件路径")
    parser.add_argument("--mqtt-cache", default=DEFAULT_MQTT_CACHE, help="MQTT 监控所需 token/设备信息缓存文件路径")
    parser.add_argument("--no-token-cache", action="store_true", help="不读取也不写入本地 token 缓存")
    parser.add_argument("--app-id", default=APP_ID, help="机智云应用 ID")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="API 基础地址")
    parser.add_argument("--api-host", default=DEFAULT_API_HOST, help="登录/绑定接口 Host 头，传空字符串则不设置")
    parser.add_argument("--site-host", default=DEFAULT_SITE_HOST, help="datapoint 接口 Host 头，传空字符串则不设置")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="登录并输出 uid/token，同时写入本地缓存")

    list_parser = sub.add_parser("devices", help="获取已绑定设备列表", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    list_parser.add_argument("--no-datapoints", action="store_true", help="不获取产品 datapoint 元数据")
    list_parser.add_argument("--no-state", action="store_true", help="不触发 Query 读取室内机状态")
    list_parser.add_argument("--state-wait", type=float, default=2.0, help="触发 Query 后等待状态上报的秒数")
    list_parser.add_argument("--state-source", default="auto", choices=["auto", "http", "cache", "mqtt"], help="状态来源：auto 优先云端缓存，没有则用本地控制缓存；mqtt 会尝试订阅推送，建议配合 --index")
    list_parser.add_argument("--index", type=int, help="只列出指定室内机，MQTT 状态读取建议使用该参数避免逐台探测过慢")
    list_parser.add_argument("--show-attrs", action="store_true", help="显示可写 datapoint 列表，默认隐藏以保持输出简洁")
    list_parser.add_argument("--mqtt-auth", default="auto", choices=["auto", "uid_token", "appid_token", "uid_amp_token"], help="MQTT 鉴权模式")
    list_parser.add_argument("--mqtt-topic-mode", default="auto", choices=["auto", "pk_did", "did"], help="MQTT 订阅 topic 模式")
    list_parser.add_argument("--mqtt-transport", default="auto", choices=["auto", "tcp", "ws"], help="MQTT 传输方式")
    list_parser.add_argument("--mqtt-port", type=int, help="指定 MQTT 端口")
    list_parser.add_argument("--mqtt-tls", default="auto", choices=["auto", "on", "off"], help="是否使用 MQTT TLS")

    bind_parser = sub.add_parser("bind", help="按 did/passcode 绑定设备或网关", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    bind_parser.add_argument("--did", required=True, help="设备 did")
    bind_parser.add_argument("--passcode", required=True, help="设备 passcode")
    bind_parser.add_argument("--remark", default="", help="设备备注")

    captured_parser = sub.add_parser("bind-captured", help="从 HAR 抓包提取网关 did/passcode 并绑定", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    captured_parser.add_argument("--har", default="2026-06-20-140258.har", help="HAR 文件路径")
    captured_parser.add_argument("--all", action="store_true", help="绑定 HAR 中所有设备；默认只绑定网关")

    ctl = sub.add_parser("control", help="控制一台室内机", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ctl.add_argument("--did", help="按 did 选择设备")
    ctl.add_argument("--mac", help="按 mac 选择设备")
    ctl.add_argument("--index", type=int, help="按室内机序号选择设备，序号从 mac 后缀推断，通常为 1..12")
    ctl.add_argument("--pk-index", type=int, help="点位名里的 PK 后缀，例如 Power_indoor_PK4 中的 4；默认自动从 datapoint 推断")
    ctl.add_argument("--power", type=bool_arg, help="开关：on/off、true/false、1/0、开/关")
    ctl.add_argument("--energy-save", type=bool_arg, help="节能开关")
    ctl.add_argument("--lock", type=bool_arg, help="锁定开关")
    ctl.add_argument("--mode", help="模式：auto/cool/dry/fan/heat 或数字 0..4")
    ctl.add_argument("--speed", help="风速：auto/lowest/low/mid/high/midhigh/highest 或数字 0..6")
    ctl.add_argument("--temp", type=float, help="温度，默认按 ac_control.py 的逻辑直接传摄氏度整数 18..30；配合 --raw-temp 可绕过转换")
    ctl.add_argument("--raw-temp", action="store_true", help="--temp 直接使用传入整数，不做 18..30 校验以外的换算")
    ctl.add_argument("--attr", action="append", help="直接传点位，例如 Power_indoor_PK4=true")
    ctl.add_argument("--control-channel", default="http", choices=["http", "mqtt"], help="控制通道，默认 http；mqtt 仅用于调试直连 broker")
    ctl.add_argument("--mqtt-auth", default="auto", choices=["auto", "uid_token", "appid_token", "uid_amp_token"], help="MQTT 鉴权模式，默认自动尝试")
    ctl.add_argument("--payload-format", default="sdk", choices=["sdk", "attrs", "entity0"], help="MQTT 控制包格式，默认 sdk，最接近 App SDK daemon")
    ctl.add_argument("--topic-mode", default="pk_did", choices=["pk_did", "did"], help="MQTT topic 模式")
    ctl.add_argument("--mqtt-transport", default="auto", choices=["auto", "tcp", "ws"], help="MQTT 传输方式，默认自动尝试 TCP 和 WebSocket")
    ctl.add_argument("--mqtt-port", type=int, help="指定 MQTT 端口；默认自动使用设备返回的 1883 和 8883")
    ctl.add_argument("--mqtt-tls", default="auto", choices=["auto", "on", "off"], help="是否使用 MQTT TLS，默认自动先试明文再试 TLS")
    ctl.add_argument("--qos", type=int, default=0, choices=[0, 1], help="MQTT QoS")
    ctl.add_argument("--wait", type=float, default=2.0, help="发送后等待设备响应的秒数")

    mqtt_status = sub.add_parser("mqtt-status", help="通过已验证的 FGCAir MQTT TLS 协议读取单台室内机状态", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    mqtt_status.add_argument("--did", help="按 did 选择设备")
    mqtt_status.add_argument("--mac", help="按 mac 选择设备")
    mqtt_status.add_argument("--index", type=int, default=1, help="按室内机序号选择设备")
    mqtt_status.add_argument("--wait", type=float, default=12.0, help="订阅后等待状态消息的秒数")
    mqtt_status.add_argument("--mqtt-auth", default="auto", choices=["auto", "uid_token", "appid_token", "uid_amp_token"], help="兼容旧参数；当前状态读取固定使用 App MQTT 鉴权")
    mqtt_status.add_argument("--mqtt-topic-mode", default="auto", choices=["auto", "pk_did", "did"], help="兼容旧参数；当前状态读取固定使用网关 topic")
    mqtt_status.add_argument("--mqtt-transport", default="auto", choices=["auto", "tcp", "ws"], help="MQTT 传输方式；当前状态读取支持 auto/tcp")
    mqtt_status.add_argument("--mqtt-port", type=int, help="指定 MQTT 端口")
    mqtt_status.add_argument("--mqtt-tls", default="auto", choices=["auto", "on", "off"], help="是否使用 MQTT TLS")

    mqtt_monitor_parser = sub.add_parser("mqtt-monitor", help="通过已验证的 FGCAir MQTT TLS 协议监控室内机状态", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    mqtt_monitor_parser.add_argument("--index", type=int, action="append", help="只监控指定室内机；可重复传。不传则监控全部已知室内机")
    mqtt_monitor_parser.add_argument("--interval", type=float, default=60.0, help="持续监控时的查询间隔秒数")
    mqtt_monitor_parser.add_argument("--once", action="store_true", help="查询一次后退出")
    mqtt_monitor_parser.add_argument("--timeout", type=float, default=25.0, help="--once 模式等待响应秒数")
    mqtt_monitor_parser.add_argument("--settle", type=float, default=3.0, help="每轮查询后等待响应再输出表格的秒数")
    mqtt_monitor_parser.add_argument("--debug-fields", action="store_true", help="输出状态 body 的字段编号，便于分析协议")
    mqtt_monitor_parser.add_argument("--raw", action="store_true", help="在控制台输出完整 MQTT JSONL 记录，每行以 MQTT_RECORD 开头")
    mqtt_monitor_parser.add_argument("--record-file", help="把完整 MQTT JSONL 记录追加写入文件，便于后续分析风速/模式字段")
    mqtt_monitor_parser.add_argument("--no-table", action="store_true", help="只输出原始记录，不输出汇总表格")
    mqtt_monitor_parser.add_argument("--refresh-cache", action="store_true", help="忽略 MQTT 缓存，重新登录并拉取绑定设备信息")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_host = args.api_host if args.api_host else None
    site_host = args.site_host if args.site_host else None
    client = FgcairHttpClient(args.api_base, args.app_id, api_host, site_host)
    if args.command == "login":
        session = client.login(args.username, args.password)
        if not args.no_token_cache:
            save_cached_session(args.token_cache, args.username, args.app_id, session)
        print_json({"uid": session.uid, "token": session.token})
        return 0

    if args.command == "mqtt-monitor":
        cached = None if args.refresh_cache else load_mqtt_cache(args.mqtt_cache, args.username, args.app_id)
        if cached is None:
            session, devices = refresh_mqtt_cache(client, args)
        else:
            session, devices = cached
        known_devices = [device for device in devices if device.get("did") in KNOWN_INDOOR_DIDS.values()]
        selected_devices = known_devices
        if args.index:
            selected_devices = [select_device(known_devices, None, None, index) for index in args.index]
        try:
            mqtt_monitor(devices, selected_devices, session, args.interval, args.once, args.timeout, args.settle, args.debug_fields, args.raw, args.record_file, args.no_table, args.app_id)
        except FgcairError as exc:
            if "MQTT 连接失败 rc=4" not in str(exc) and "MQTT 连接失败 rc=5" not in str(exc):
                raise
            session, devices = refresh_mqtt_cache(client, args)
            known_devices = [device for device in devices if device.get("did") in KNOWN_INDOOR_DIDS.values()]
            selected_devices = known_devices
            if args.index:
                selected_devices = [select_device(known_devices, None, None, index) for index in args.index]
            mqtt_monitor(devices, selected_devices, session, args.interval, args.once, args.timeout, args.settle, args.debug_fields, args.raw, args.record_file, args.no_table, args.app_id)
        return 0

    session = get_session(client, args)

    if args.command == "bind":
        print_json(client.bind_device(session.token, args.did, args.passcode, args.remark))
        return 0

    if args.command == "bind-captured":
        devices_to_bind = captured_devices_from_har(args.har, args.all)
        print_json(client.bind_devices(session.token, devices_to_bind))
        return 0

    devices = client.list_bindings(session.token)
    save_mqtt_cache(args.mqtt_cache, args.username, args.app_id, session, devices)
    if getattr(args, "index", None) is not None and args.command == "devices":
        devices = [select_device(devices, None, None, args.index)]
    if args.command == "devices":
        print_json(
            summarize_devices(
                client,
                session,
                args.app_id,
                session.token,
                devices,
                not args.no_datapoints,
                not args.no_state,
                args.state_wait,
                args.show_attrs,
                args.state_source,
                args.state_cache,
                args.mqtt_auth,
                args.mqtt_topic_mode,
                args.mqtt_port,
                args.mqtt_tls,
                args.mqtt_transport,
            )
        )
        return 0

    if args.command == "mqtt-status":
        if not session.uid:
            raise FgcairError("使用 --token 读取 MQTT 状态时必须同时提供 --uid")
        device = select_device(devices, args.did, args.mac, args.index)
        state = mqtt_read_state(
            client,
            session.token,
            device,
            args.app_id,
            session,
            args.mqtt_auth,
            args.mqtt_topic_mode,
            args.mqtt_port,
            args.mqtt_tls,
            args.mqtt_transport,
            args.wait,
            {},
        )
        print_json({"device": {"did": device.get("did"), "mac": device.get("mac")}, "query": {"protocol": "fgcair_mqtt_tls"}, "state": state})
        return 0

    if args.command == "control":
        if not session.uid:
            raise FgcairError("使用 --token 控制 MQTT 时必须同时提供 --uid")
        device = select_device(devices, args.did, args.mac, args.index)
        datapoint = client.datapoint(str(device.get("product_key") or ""))
        attrs = make_control_attrs(args, device, product_indoor_pk_index(datapoint))
        if args.control_channel == "http":
            result = control_device_sequence(client, session.token, str(device["did"]), attrs)
            merge_control_state(args.state_cache, device, attrs)
            latest = client.latest_devdata(session.token, str(device["did"]))
            print_json({"device": {"did": device.get("did"), "mac": device.get("mac")}, "sent_attrs": attrs, "result": result, "latest": latest})
            return 0
        responses = mqtt_publish(
            device,
            session,
            args.app_id,
            attrs,
            args.qos,
            args.wait,
            args.mqtt_auth,
            args.payload_format,
            args.topic_mode,
            args.mqtt_port,
            args.mqtt_tls,
            args.mqtt_transport,
        )
        print_json({"device": {"did": device.get("did"), "mac": device.get("mac")}, "sent_attrs": attrs, "responses": responses})
        return 0

    raise FgcairError(f"未处理的命令: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FgcairError, argparse.ArgumentTypeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
