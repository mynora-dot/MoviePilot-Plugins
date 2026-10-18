"""百度分享目录、提取码访问和同盘转存。"""

from __future__ import annotations

import json
import re
import time
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .files import cloud_file
from ..common import safe_int, should_skip_season_dir
from ...core.cloud import ShareLinkStatus
from ...utils.cache import create_platform_ttl_cache


class BaiduShareService:
    def __init__(self, client: Any, files: Any):
        self.client = client
        self._files = files
        self.page_size = 100
        self._shares = create_platform_ttl_cache("baidu:shares", client, maxsize=128, ttl=600)
        self._blocked_until = 0.0
        self.last_error = ""

    @property
    def transfer_risk_blocked(self) -> bool:
        return time.monotonic() < self._blocked_until

    @staticmethod
    def extract_share_info(share_url: str) -> dict:
        match = re.search(r"https?://(?:pan|yun)\.baidu\.com/(?:s/[A-Za-z0-9_-]+|share/init\?[^\s<>]+)",
                          str(share_url or ""), re.I)
        if not match:
            return {}
        # /s/ 后的分享标识以 1 开头；wxlist 的 shorturl 不含此前缀。
        parsed = urlsplit(match.group(0))
        if parsed.path.startswith("/s/"):
            full_id = parsed.path.split("/")[2]
            if not full_id.startswith("1") or len(full_id) < 2:
                return {}
            short_id = full_id[1:]
        else:
            short_id = (parse_qs(parsed.query).get("surl") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", short_id):
            return {}
        password_match = re.search(r"[?&#]pwd=([A-Za-z0-9]{4})(?![A-Za-z0-9])|(?:提取码|密码)\s*[:：]?\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])",
                                   str(share_url), re.I)
        password = next((v for v in password_match.groups() if v), "") if password_match else ""
        return {"share_code": short_id, "share_id": short_id, "password": password,
                "receive_code": password, "url": f"https://pan.baidu.com/s/1{short_id}"}

    def _listing_request(self, info: dict, path: str = "", page: int = 1) -> dict:
        response = self.client.request("POST", "share/wxlist", params={
            "channel": "weixin", "version": "2.2.2", "clienttype": 25,
        }, data={"shorturl": info["share_id"], "pwd": info["password"],
                 "root": 0 if path else 1, "dir": path,
                 "page": page, "num": self.page_size, "order": "name"},
            headers={"User-Agent": "netdisk", "Referer": info["url"]})
        if not self.client.is_success(response):
            if safe_int(response.get("errno")) in (8001, 31326, 31034):
                self._blocked_until = time.monotonic() + self.client.risk_cooldown
            raise RuntimeError(response.get("message") or "读取百度分享失败")
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RuntimeError("百度分享未返回文件列表，请检查分享状态")
        return data

    def _access(self, share_url: str) -> tuple[dict, dict]:
        info = self.extract_share_info(share_url)
        if not info:
            raise ValueError("无效的百度网盘分享链接")
        key = f"{info['share_id']}:{info['password']}"
        context = self._shares.get(key)
        if context is None:
            root = self._listing_request(info)
            context = {"root": root, "items": {}}
            self._shares.set(key, context)
        return info, context

    def _directory(self, share_url: str, parent_id: str = "") -> list[dict]:
        info, context = self._access(share_url)
        parent = None
        if parent_id and parent_id != "0":
            parent = context["items"].get(str(parent_id))
            if parent is None or str(parent.get("isdir")) != "1":
                raise ValueError("百度分享目录已失效，请重新打开分享预览")
        remote_path = str(parent.get("path") or "") if parent else ""
        parent_path = str(parent.get("relative_path") or "") if parent else ""
        result, page = [], 1
        while True:
            data = context["root"] if not remote_path and page == 1 else self._listing_request(info, remote_path, page)
            rows = data["list"]
            for raw in rows:
                item = cloud_file(raw, shared=True)
                if not item:
                    continue
                relative = str(PurePosixPath(parent_path) / item.name)
                context["items"][item.id] = {**raw, "relative_path": relative}
                result.append({**dict(item), "parent_path": parent_path, "relative_path": relative})
            more = data.get("has_more")
            if not rows or (more is not None and str(more).lower() in ("0", "false")) or (
                    more is None and len(rows) < self.page_size
            ):
                return result
            page += 1

    def list_share_directory(self, share_url: str, parent_id: str = "") -> list:
        return self._directory(share_url, parent_id)

    def list_share_files(self, share_url: str, **kwargs: Any) -> list:
        result, stack, visited = [], [""], set()
        wanted = {str(value) for value in kwargs.get("file_ids", [])}
        found = set()
        while stack:
            stop = kwargs.get("stop_requested")
            if stop and stop():
                raise InterruptedError("百度分享读取已停止")
            parent_id = stack.pop()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            for item in self._directory(share_url, parent_id):
                if item["is_dir"]:
                    if should_skip_season_dir(item["name"], kwargs.get("target_season")):
                        continue
                    stack.append(item["id"])
                result.append(item)
                found.add(item["id"])
                if wanted and wanted <= found:
                    return result
        return result

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        try:
            rows = self._directory(share_url)
            return ShareLinkStatus(is_valid=True, file_count=len(rows))
        except (ValueError, RuntimeError) as error:
            return ShareLinkStatus(error_message=str(error))

    def _save(self, share_url: str, file_ids: list[str], save_path: str) -> list[str]:
        if self.transfer_risk_blocked:
            raise RuntimeError("百度分享转存处于风控冷却期，请稍后重试")
        info, context = self._access(share_url)
        if not file_ids:
            file_ids = [item["id"] for item in self._directory(share_url)]
        if not file_ids:
            raise RuntimeError("百度分享为空，无法转存")
        # 不接受任意 fs_id；只转存当前分享中已列举的文件。
        if any(str(fid) not in context["items"] for fid in file_ids):
            self.list_share_files(share_url, file_ids=file_ids)
        if any(str(fid) not in context["items"] for fid in file_ids):
            raise ValueError("所选文件已不在百度分享中，请刷新后重试")
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError("百度网盘转存目录不可用")
        root = context["root"]
        if not root.get("shareid") or not root.get("uk") or not root.get("seckey"):
            raise RuntimeError("百度分享缺少转存凭据，请刷新分享或更新 Cookie")
        response = self.client.request("POST", "share/transfer", authenticated=True, params={
            "shareid": root["shareid"], "from": root["uk"], "ondup": "fail", "async": 1,
        }, data={"fsidlist": json.dumps([int(fid) for fid in file_ids]), "path": lookup.directory_id,
                 "extra": json.dumps({"sekey": unquote(str(root["seckey"]))})},
            share_key=str(root["seckey"]), headers={"Referer": info["url"]})
        try:
            if self.client.is_success(response) and response.get("taskid"):
                response = self.client.wait_for_task(str(response["taskid"]))
            if not self.client.is_success(response):
                if safe_int(response.get("errno")) in (8001, 31326, 31034):
                    self._blocked_until = time.monotonic() + self.client.risk_cooldown
                raise RuntimeError(response.get("message") or "百度分享转存失败")
            failed = {str(row.get("fs_id") or row.get("from_fs_id") or "") for row in response.get("info", [])
                      if isinstance(row, dict) and safe_int(row.get("errno")) != 0}
            if failed:
                raise RuntimeError("百度分享部分文件转存失败，请检查目标目录")
            return [str(fid) for fid in file_ids]
        finally:
            # 即使超时也清理快照，后续由同步流程核验真实落盘结果。
            self._files.refresh_directories()

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        ids = [row["id"] for row in self._directory(share_url)]
        if not ids:
            raise RuntimeError("百度分享为空，无法转存")
        succeeded, failed = self.transfer_files_batch(share_url, ids, save_path)
        return bool(succeeded) and not failed

    def transfer_file(self, share_url: str, file_id: str, save_path: str, target_name: str = "") -> bool:
        return bool(self._save(share_url, [str(file_id)], save_path))

    def transfer_files_batch(self, share_url: str, file_ids: list, save_path: str, **kwargs: Any) -> tuple:
        values = list(dict.fromkeys(str(fid) for fid in file_ids))
        size = max(1, min(safe_int(kwargs.get("batch_size", 20)), 100))
        succeeded, failed = [], []
        self.last_error = ""
        for offset in range(0, len(values), size):
            stop = kwargs.get("stop_requested")
            if stop and stop():
                failed.extend(values[offset:])
                break
            batch = values[offset:offset + size]
            try:
                succeeded.extend(self._save(share_url, batch, save_path))
            except (ValueError, RuntimeError) as error:
                self.last_error = str(error)
                failed.extend(batch)
                if self.transfer_risk_blocked:
                    failed.extend(values[offset + size:])
                    break
            if offset + size < len(values):
                time.sleep(max(0, min(float(kwargs.get("batch_interval", 3)), 60)))
        return succeeded, failed
