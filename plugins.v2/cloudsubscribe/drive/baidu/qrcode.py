"""百度 Passport 扫码登录；临时会话仅保存在服务端。"""

import base64
import json
import re
import secrets
import time
from threading import RLock

import requests

from .client import BaiduClient


class BaiduQRCodeService:
    PASSPORT = "https://passport.baidu.com"
    SESSION_TTL = 120

    def __init__(self):
        self._sessions = {}
        self._lock = RLock()

    @staticmethod
    def _payload(response):
        response.raise_for_status()
        text = response.text.strip()
        # Passport 部分接口即使不指定 callback 也会返回 JSONP。
        match = re.fullmatch(r"[\w.$]+\s*\((.*)\)\s*;?", text, re.S)
        value = json.loads(match.group(1) if match else text)
        if not isinstance(value, dict):
            raise RuntimeError("百度扫码接口返回格式异常")
        return value

    def _discard(self, token):
        item = self._sessions.pop(token, None)
        if item:
            item["session"].close()

    def close(self):
        with self._lock:
            for token in list(self._sessions):
                self._discard(token)

    def create_qrcode_login(self, client_type=""):
        with self._lock:
            for token, item in list(self._sessions.items()):
                if item["expires"] <= time.monotonic():
                    self._discard(token)
            if len(self._sessions) >= 32:
                self._discard(next(iter(self._sessions)))
            session = requests.Session()
            session.headers.update(BaiduClient.DEFAULT_HEADERS)
            try:
                response = session.get(f"{BaiduClient.BASE_URL}/", timeout=15)
                response.raise_for_status()
                data = self._payload(session.get(
                    f"{self.PASSPORT}/v2/api/getqrcode",
                    params={"lp": "pc", "qrloginfrom": "pc", "tpl": "netdisk",
                            "apiver": "v3", "tt": str(int(time.time() * 1000))}, timeout=15,
                ))
                sign = str(data.get("sign") or "")
                if str(data.get("errno", 0)) != "0" or not sign:
                    raise RuntimeError("百度未返回登录二维码，请稍后重试")
                # 固定图片接口，避免请求响应中未经校验的外部地址。
                image = session.get(f"{self.PASSPORT}/v2/api/qrcode",
                                    params={"sign": sign, "lp": "pc"}, timeout=15)
                image.raise_for_status()
                if not image.content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("百度登录二维码图片无效，请稍后重试")
                token = secrets.token_urlsafe(32)
                self._sessions[token] = {"session": session, "sign": sign,
                                         "expires": time.monotonic() + self.SESSION_TTL}
                return {"qr_token": token, "interval": 3,
                        "qrcode": "data:image/png;base64," + base64.b64encode(image.content).decode("ascii")}
            except Exception:
                session.close()
                raise

    def check_qrcode_login(self, qr_token="", **kwargs):
        with self._lock:
            item = self._sessions.get(qr_token)
            if not item or item["expires"] <= time.monotonic():
                self._discard(qr_token)
                return {"status": "expired", "message": "二维码已过期，请刷新后重新扫码"}
            session = item["session"]
            try:
                data = self._payload(session.get(
                    "https://passport.baidu.com/channel/unicast",
                    params={"channel_id": item["sign"], "tpl": "netdisk", "apiver": "v3"},
                    timeout=10,
                ))
            except requests.exceptions.Timeout:
                return {"status": "waiting", "message": "等待百度网盘 App 扫码确认"}
            if str(data.get("errno", 0)) == "1":
                return {"status": "waiting", "message": "等待百度网盘 App 扫码确认"}
            if str(data.get("errno", 0)) != "0":
                self._discard(qr_token)
                return {"status": "expired", "message": "二维码已失效，请重新扫码"}
            channel = data.get("channel_v") or {}
            if isinstance(channel, str):
                channel = json.loads(channel)
            if not isinstance(channel, dict):
                raise RuntimeError("百度扫码状态格式异常")
            status = str(channel.get("status", ""))
            if status in {"1", "2"}:
                return {"status": "waiting", "message": "请在百度网盘 App 中确认登录"}
            if status != "0" or not channel.get("v"):
                self._discard(qr_token)
                return {"status": "cancelled", "message": "扫码登录已取消，请重新扫码"}
            try:
                result = self._payload(session.get(
                    f"{self.PASSPORT}/v3/login/main/qrbdusslogin",
                    params={"bduss": channel["v"], "loginVersion": "v4", "qrcode": "1",
                            "tpl": "netdisk", "apiver": "v3",
                            "u": "https://pan.baidu.com/disk/main"}, timeout=15,
                ))
                if str((result.get("errInfo") or {}).get("no", "-1")) != "0":
                    raise RuntimeError("百度登录未完成，请在百度网盘网页完成账号验证后重试")
                response = session.get("https://pan.baidu.com/disk/main", timeout=15)
                response.raise_for_status()
                cookies = {cookie.name: cookie.value for cookie in session.cookies
                           if cookie.domain.lstrip(".") == "baidu.com"
                           or cookie.domain.endswith(".baidu.com")}
                if not cookies.get("BDUSS"):
                    raise RuntimeError("百度未返回登录 Cookie，请重新扫码或手动填写 Cookie")
                return {"status": "success", "cookie": "; ".join(
                    f"{name}={value}" for name, value in cookies.items()), "message": "百度网盘登录成功"}
            finally:
                self._discard(qr_token)
