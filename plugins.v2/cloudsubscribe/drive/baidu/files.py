"""百度目录和文件操作；自有文件以完整路径定位，重命名后同步更新定位值。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from threading import RLock
from typing import Any

from ..common import CloudDriveFileServiceBase, create_directory_cache, normalize_path, safe_int
from ...core.cloud import CloudFile


def cloud_file(raw: Any, *, shared: bool = False) -> CloudFile | None:
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "")
    name = str(raw.get("server_filename") or raw.get("name") or PurePosixPath(path).name)
    identity = raw.get("fs_id") if shared else path
    if identity in (None, "") or not name:
        return None
    return CloudFile(id=str(identity), name=name, is_directory=str(raw.get("isdir")) == "1",
                     size=safe_int(raw.get("size")), md5=str(raw.get("md5") or ""), native=raw)


class BaiduFileService(CloudDriveFileServiceBase):
    root_directory_id = "/"
    provider_name = "百度网盘"
    provider_key = "baidu"

    def __init__(self, client: Any, page_size: int = 100):
        self.client = client
        self.page_size = page_size
        self._directory_lock = RLock()
        self._directory_cache = create_directory_cache("baidu", client)

    def _list(self, directory_id: str) -> list[CloudFile]:
        path = normalize_path(directory_id)
        cached = self._directory_cache.get(path)
        if cached is not None:
            return list(cached)
        files = []
        page = 1
        while True:
            response = self.client.request("GET", "api/list", authenticated=True, params={
                "dir": path, "num": self.page_size, "page": page, "order": "name", "desc": 0,
            })
            if not self.client.is_success(response) or not isinstance(response.get("list"), list):
                raise RuntimeError(response.get("message") or "读取百度网盘目录失败")
            rows = response["list"]
            files.extend(item for row in rows if (item := cloud_file(row)))
            if not rows or ("has_more" in response and str(response["has_more"]).lower() in ("0", "false")) or (
                    "has_more" not in response and len(rows) < self.page_size
            ):
                self._directory_cache.set(path, tuple(files))
                return files
            page += 1

    def _create_folder(self, name: str, parent_id: str) -> CloudFile | None:
        path = str(PurePosixPath(parent_id) / name)
        response = self.client.request("POST", "api/create", authenticated=True,
                                       params={"a": "commit"}, data={"path": path, "isdir": 1, "block_list": "[]"})
        self._invalidate_directory_cache()
        if not self.client.is_success(response):
            # 并发创建或已有同名目录时，必须确认目录确实存在。
            existing = next((f for f in self._list(parent_id) if f.name == name and f.is_directory), None)
            if existing:
                return existing
            raise RuntimeError(response.get("message") or "创建百度网盘目录失败")
        return cloud_file({**response, "path": path, "server_filename": name, "isdir": 1})

    def _is_success(self, response: Any) -> bool:
        return self.client.is_success(response)

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        result = self.client.file_manager("rename", [{"path": item.id, "newname": target_name}])
        self.refresh_directories()
        return self._is_success(result)

    def move_file(self, item: CloudFile, save_path: str, target_name: str) -> CloudFile | None:
        target_name = target_name or item.name
        destination = str(PurePosixPath(normalize_path(save_path)) / target_name)
        if item.id == destination:
            return item
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        # 百度同目录改名走 rename，跨目录移动可以同时指定新文件名。
        if str(PurePosixPath(item.id).parent) == lookup.directory_id:
            success = self.rename_file(save_path, item, target_name)
        else:
            result = self.client.file_manager("move", [{"path": item.id, "dest": lookup.directory_id,
                                                       "newname": target_name, "ondup": "fail"}])
            success = self._is_success(result)
            self.refresh_directories()
        return self.find_file(save_path, target_name) if success else None

    def delete_file(self, file_id: str) -> bool:
        result = self.client.file_manager("delete", [file_id])
        self.refresh_directories()
        return self._is_success(result)

    def rename_files(self, path: str, items: dict) -> dict[str, CloudFile]:
        renamed = {}
        for key, value in items.items():
            item, name = value.get("item"), value.get("target_name")
            if item and name and self.rename_file(path, item, name):
                target_path = str(PurePosixPath(item.id).parent / name)
                renamed[str(key)] = replace(item, id=target_path, name=name,
                                            native={**(item.native or {}), "path": target_path, "server_filename": name})
        return renamed
