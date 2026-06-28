from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .const import API_BASE, API_HOST, APP_ID, SITE_HOST, KNOWN_INDOOR_DIDS


class FGCAirError(Exception):
    """FGCAir 云端请求错误。"""


class FGCAirAuthError(FGCAirError):
    """FGCAir token 或账号认证错误。"""


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
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
        try:
            await self.list_bindings(refresh=False)
        except FGCAirAuthError:
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

    async def bind_captured_gateway(self) -> Any:
        await self.ensure_session()
        if not self.session:
            raise FGCAirAuthError("尚未登录")
        return await self._request(
            "POST",
            "/app/bindings",
            token=self.session.token,
            body={"devices": [{"did": "9CEVveZCS9upwabSnRyUTW", "passcode": "ANFILBWOJF", "remark": ""}]},
        )

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
            return await self._request("POST", f"/app/control/{did}", token=self.session.token, body={"attrs": attrs})
        except FGCAirAuthError:
            await self.ensure_session(force=True)
            return await self._request("POST", f"/app/control/{did}", token=self.session.token, body={"attrs": attrs})

    async def latest(self, did: str) -> dict[str, Any]:
        await self.ensure_session()
        if not self.session:
            raise FGCAirAuthError("尚未登录")
        return await self._request("GET", f"/app/devdata/{did}/latest", token=self.session.token)


def indoor_index(device: dict[str, Any]) -> int | None:
    did = device.get("did")
    for index, known_did in KNOWN_INDOOR_DIDS.items():
        if did == known_did:
            return index
    return None


def indoor_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [device for device in devices if indoor_index(device) is not None]


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
