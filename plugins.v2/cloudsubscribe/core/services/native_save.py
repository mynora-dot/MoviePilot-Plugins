"""将分享原样保存到指定网盘；与主网盘的整理和跨盘下载流程独立。"""

from __future__ import annotations

import time
from threading import Event
from uuid import uuid4

from app.log import logger

from .runtime import sync_lock
from .. import CloudDriveCapability as Cap, OwnerDelegator


class NativeShareSaveService(OwnerDelegator):
    def run(self, provider_key: str, resources: list, media: dict | None = None, result=None) -> bool:
        while not sync_lock.acquire(timeout=0.5):
            if self._subscribe_search_queue_shutdown.is_set():
                if result is not None:
                    result.update(success=False, message="排队任务已取消")
                return False
        task_id = f"native:{uuid4().hex}"
        stop = Event()
        saved = 0
        media = media or {}
        started = time.time()
        try:
            if self._subscribe_search_queue_shutdown.is_set():
                raise InterruptedError("排队任务已取消")
            self._apply_pending_config_if_idle()
            provider = self._cloud_drive_registry.get(provider_key)
            share = provider.require(Cap.SHARE_TRANSFER)
            files = provider.require(Cap.FILE_QUERY)
            path = str(getattr(self, f"_{provider.config_prefix}_transfer_path", "/") or "/")
            with self._sync_tasks_lock:
                self._sync_tasks[task_id] = {
                    "id": task_id, "title": media.get("title") or resources[0].get("title") or "分享保存",
                    "task_kind": "native_save", "media_type": "电视剧" if media.get("media_type") == "tv" else "电影",
                    "status": "transferring", "phase": f"保存到{provider.name}", "progress": 0,
                    "transferred": 0, "queued_at": started, "started_at": started, "stop_event": stop,
                    "message": f"目标目录：{path}",
                }
            self._mark_runtime_changed()
            if not provider.require(Cap.AUTHENTICATION).check_login():
                raise RuntimeError(f"{provider.name}登录已失效，请更新 Cookie")
            for resource in resources:
                if stop.is_set() or self._stop_requested():
                    raise InterruptedError("分享保存已停止")
                url = resource["url"]
                ids = resource.get("target_file_ids") or []
                rows = share.list_share_files(
                    url, file_ids=ids, stop_requested=lambda: stop.is_set() or self._stop_requested(),
                ) if ids else share.list_share_directory(url)
                selected = {str(value) for value in ids}
                entries = [row for row in rows if not ids or str(row["id"]) in selected]
                if ids and selected != {str(row["id"]) for row in entries}:
                    raise ValueError("所选文件已不在分享中，请刷新后重试")
                # 同时选择目录与其子文件时，只保存上层目录，避免重复提交。
                directories = [row["relative_path"].rstrip("/") + "/" for row in entries if row["is_dir"]]
                entries = [row for row in entries if not any(
                    row["relative_path"].startswith(parent) for parent in directories
                )]
                if not entries:
                    raise ValueError("分享中没有可保存的内容")
                succeeded, failed = share.transfer_files_batch(
                    url, [row["id"] for row in entries], path,
                    batch_size=self._batch_size, batch_interval=self._batch_interval,
                    stop_requested=lambda: stop.is_set() or self._stop_requested(),
                )
                records = []
                for row in entries:
                    if str(row["id"]) not in succeeded:
                        continue
                    target = files.find_file_strict(path, row["name"])
                    if target is None or target.is_directory != row["is_dir"] or (
                            not row["is_dir"] and target.size != int(row.get("size") or 0)
                    ):
                        raise RuntimeError(f"{provider.name}已提交保存，但目标内容尚未确认：{row['name']}")
                    saved += 1
                    records.append({
                        "title": media.get("title") or resource.get("title") or row["name"],
                        "year": media.get("year") or "", "tmdb_id": media.get("tmdb_id"),
                        "type": "电视剧" if media.get("media_type") == "tv" else "电影",
                        "status": "成功", "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "resource_type": provider_key, "source": resource.get("source") or "manual",
                        "share_url": url, "file_name": row["name"], "source_file_name": row["name"],
                        "file_size": row.get("size") or 0, "cloud_dir": path, "file_id": target.id,
                        "target_drive_key": provider_key, "target_drive_name": provider.name,
                        "cloud_drive_key": provider_key, "cloud_drive_name": provider.name,
                        "is_dir": row["is_dir"], "transfer_mode": "native",
                    })
                if records and self._sync_handler:
                    self._sync_handler.append_history_records(records)
                self._update_sync_task(task_id, transferred=saved)
                if failed:
                    if stop.is_set() or self._stop_requested():
                        raise InterruptedError("分享保存已停止")
                    raise RuntimeError(getattr(share, "last_error", "") or "部分内容未能保存，请检查网盘后重试")
            message = f"已保存 {saved} 项到{provider.name}：{path}"
            self._update_sync_task(task_id, status="completed", phase="保存完成", progress=100,
                                   transferred=saved, message=message, finished_at=time.time())
            logger.info(message)
            if self._notify:
                self.post_message(mtype=self._notification_type, title="【网盘订阅助手】分享保存完成", text=message)
            if result is not None:
                result.update(success=True, message=message, data={"transferred": saved})
            return True
        except Exception as error:
            status = "stopped" if isinstance(error, InterruptedError) else "failed"
            message = str(error)
            self._update_sync_task(task_id, status=status, phase="保存已停止" if status == "stopped" else "保存失败",
                                   transferred=saved, message=message, finished_at=time.time())
            logger.warning(f"分享保存失败：{message}")
            if self._sync_handler:
                try:
                    self._sync_handler.append_history_records([{
                        "title": media.get("title") or resources[0].get("title") or "百度分享保存",
                        "tmdb_id": media.get("tmdb_id"), "year": media.get("year") or "",
                        "type": "电视剧" if media.get("media_type") == "tv" else "电影",
                        "status": "已停止" if status == "stopped" else "失败", "message": message,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"), "resource_type": provider_key,
                        "source": "manual", "share_url": resources[0].get("url", ""),
                        "file_name": "分享保存任务", "transfer_mode": "native",
                        "target_drive_key": provider_key, "target_drive_name": "百度网盘",
                        "cloud_drive_key": provider_key, "cloud_drive_name": "百度网盘",
                    }])
                except Exception:
                    logger.warning("写入分享保存失败历史时发生异常")
            if self._notify:
                self.post_message(mtype=self._notification_type, title="【网盘订阅助手】分享保存失败", text=message)
            if result is not None:
                result.update(success=False, message=message, data={"transferred": saved})
            return False
        finally:
            # 沿用同步队列的释放流程，应用执行期间保存的配置。
            self._release_sync_resources(False)
