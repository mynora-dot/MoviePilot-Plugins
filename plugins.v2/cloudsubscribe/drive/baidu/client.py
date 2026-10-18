"""百度网盘网页会话与文件管理接口。"""

from __future__ import annotations

import json
import time
from threading import RLock
from typing import Any

import requests

from ..common import DriveRateLimiter, format_size, safe_int


class BaiduClient:
    BASE_URL = "https://pan.baidu.com"
    DEFAULT_PARAMS = {"app_id": "250528", "web": "1", "channel": "chunlei", "clienttype": "0"}
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://pan.baidu.com/disk/main",
        "Origin": "https://pan.baidu.com",
    }
    ERROR_MESSAGES = {
        -6: "百度登录已失效，请更新网盘 Cookie（包含 BDUSS、STOKEN）",
        -7: "百度分享已失效或文件已删除",
        -9: "百度分享提取码错误",
        -12: "百度分享需要提取码或验证",
        -30: "目标目录已有同名文件或文件夹",
        -62: "百度提取码尝试次数过多，请稍后重试",
        -63: "百度分享访问受限，请稍后重试",
        4: "目标目录已有相同文件",
        12: "百度文件操作部分失败或超过账号单次转存限制",
        105: "百度分享已取消",
        115: "百度分享已过期",
        130: "百度网盘空间不足",
        31024: "百度网盘空间不足",
        31034: "百度接口请求过于频繁，请稍后重试",
        31326: "百度账号需要验证，请在百度网盘网页完成验证后重试",
        8001: "百度账号触发验证，请在百度网盘网页完成验证后重试",
    }

    def __init__(self, cookie: str = "", timeout: int = 30):
        self.cookie = str(cookie or "").strip()
        self.request_timeout = max(5, min(int(timeout or 30), 300))
        self.risk_cooldown = 1800
        self.rate_limiter = DriveRateLimiter.shared("baidu", self.cookie, min_interval=0.8)
        self._session = requests.Session()
        self._session.headers.update(self.DEFAULT_HEADERS)
        # 交给 CookieJar 合并 BDCLND 等分享会话，避免手工 Cookie 头覆盖验证结果。
        for part in self.cookie.split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                self._session.cookies.set(key, value, domain=".baidu.com", path="/")
        self._token = ""
        self._profile: dict = {}
        self._token_expires_at = 0.0
        self._lock = RLock()

    def close(self) -> None:
        self._session.close()

    @staticmethod
    def is_success(response: Any) -> bool:
        return isinstance(response, dict) and response.get("errno") in (0, "0")

    @classmethod
    def error_message(cls, response: dict) -> str:
        code = safe_int(response.get("errno", -1))
        return cls.ERROR_MESSAGES.get(code) or f"百度接口请求失败（错误码 {code}）"

    def request(
            self, method: str, endpoint: str, *, params: dict | None = None,
            data: dict | None = None, authenticated: bool = False,
            headers: dict | None = None, share_key: str | None = None,
    ) -> dict:
        query = {**self.DEFAULT_PARAMS, **(params or {})}
        if authenticated:
            query["bdstoken"] = self.get_token()
        # 不自动重放写请求：超时可能发生在服务器已完成转存之后。
        with self._lock:
            if share_key is not None:
                for cookie in list(self._session.cookies):
                    if cookie.name == "BDCLND":
                        self._session.cookies.clear(cookie.domain, cookie.path, cookie.name)
                self._session.cookies.set("BDCLND", share_key, domain=".baidu.com", path="/")
            try:
                response = self.rate_limiter.call(
                    self._session.request, method, f"{self.BASE_URL}/{endpoint.lstrip('/')}",
                    params=query, data=data, headers=headers,
                    timeout=(10, self.request_timeout), allow_redirects=False,
                    max_retries=2 if method.upper() == "GET" else 0,
                )
                if response.status_code in (301, 302, 401, 403):
                    result = {"errno": -6}
                elif response.status_code >= 400:
                    result = {"errno": -1, "message": f"百度接口返回 HTTP {response.status_code}"}
                else:
                    result = response.json()
                    if not isinstance(result, dict) or "errno" not in result:
                        result = {"errno": -1, "message": "百度接口返回格式异常，请检查登录状态"}
            except requests.RequestException:
                # requests 异常可能包含带 bdstoken 的 URL，不写入日志或透传给前端。
                result = {"errno": -1, "message": "百度网盘请求失败或超时，请检查网络后重试"}
            except ValueError:
                result = {"errno": -1, "message": "百度接口未返回 JSON，请检查登录状态或完成网页验证"}
            if not self.is_success(result):
                if result.get("errno") in (-6, "-6"):
                    self._token = ""
                    self._token_expires_at = 0
                result.setdefault("message", self.error_message(result))
            return result

    def get_token(self, force: bool = False) -> str:
        with self._lock:
            if self._token and not force and time.monotonic() < self._token_expires_at:
                return self._token
            response = self.request("GET", "api/gettemplatevariable", params={
                "fields": json.dumps(["bdstoken", "uk", "username", "photo", "is_vip", "is_svip"]),
            })
            profile = response.get("result") or {}
            token = str(profile.get("bdstoken") or "")
            if not self.is_success(response) or not token:
                raise RuntimeError(response.get("message") or self.ERROR_MESSAGES[-6])
            self._profile = profile
            self._token = token
            self._token_expires_at = time.monotonic() + 300
            return token

    def check_login(self) -> bool:
        try:
            self.get_token(force=True)
            return True
        except RuntimeError:
            return False

    def get_account_info(self) -> dict:
        try:
            self.get_token(force=True)
            quota = self.request("GET", "api/quota", params={"checkfree": 1, "checkexpire": 1}, authenticated=True)
            storage = {}
            if self.is_success(quota):
                used, total = safe_int(quota.get("used")), safe_int(quota.get("total"))
                storage = {"used": format_size(used), "total": format_size(total),
                           "remaining": format_size(max(0, total - used))}
            return {
                "connected": True,
                "user": {"name": self._profile.get("username") or "百度网盘用户",
                         "avatar": self._profile.get("photo") or "", "membership_supported": False},
                "storage": storage,
                "details": [{"label": "用户 ID", "value": str(self._profile.get("uk") or "")}],
            }
        except RuntimeError as error:
            return {"connected": False, "error": str(error)}

    def file_manager(self, operation: str, items: list) -> dict:
        response = self.request("POST", "api/filemanager", authenticated=True,
                                params={"opera": operation, "async": 2, "onnest": "fail"},
                                data={"filelist": json.dumps(items, ensure_ascii=False)})
        task_id = response.get("taskid")
        if self.is_success(response) and task_id:
            response = self.wait_for_task(str(task_id))
        if self.is_success(response) and any(
                safe_int(item.get("errno")) != 0 for item in response.get("info", [])
                if isinstance(item, dict)
        ):
            return {"errno": 12, "message": "百度文件操作部分失败"}
        return response

    def wait_for_task(self, task_id: str) -> dict:
        deadline = time.monotonic() + self.request_timeout
        while time.monotonic() < deadline:
            response = self.request("GET", "share/taskquery", authenticated=True,
                                    params={"taskid": task_id})
            if not self.is_success(response):
                return response
            status = response.get("status")
            if status == "success":
                return response
            if status not in ("pending", "running"):
                return {"errno": -1, "message": "百度文件任务未成功完成"}
            time.sleep(0.5)
        return {"errno": -1, "message": "百度文件任务等待超时，请检查网盘中的实际结果"}
