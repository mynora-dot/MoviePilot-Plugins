"""百度分享、文件操作和原样保存的协议回归测试（无真实账号或网络）。"""

import json
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

import requests

from plugin_env import OwnerDelegator, install_app_mocks, load_module, register_module, register_package

install_app_mocks("app.core", "app.core.cache", "app.core.metainfo", "app.db", "app.db.subscribe_oper",
                  "app.utils", "app.utils.string")

# 独立命名空间避免其他测试的 MoviePilot/网盘桩污染驱动契约。
PKG = "baidu_drive_test"
for suffix in ("", ".core", ".core.services", ".utils", ".drive", ".drive.baidu"):
    register_package(PKG + suffix)


class MemoryCache(dict):
    def set(self, key, value):
        self[key] = value

    def delete(self, key):
        self.pop(key, None)


register_module(PKG + ".utils.cache", create_platform_ttl_cache=lambda *a, **kw: MemoryCache())
cloud = load_module(PKG + ".core.cloud", "plugins.v2/cloudsubscribe/core/cloud.py")
register_module(PKG + ".core", package=True, CloudDriveCapability=cloud.CloudDriveCapability,
                OwnerDelegator=OwnerDelegator, SearchCapability=MagicMock())
register_module(PKG + ".core.services.runtime", sync_lock=threading.Lock())
load_module(PKG + ".drive.common", "plugins.v2/cloudsubscribe/drive/common.py")
client_mod = load_module(PKG + ".drive.baidu.client", "plugins.v2/cloudsubscribe/drive/baidu/client.py")
qrcode_mod = load_module(PKG + ".drive.baidu.qrcode", "plugins.v2/cloudsubscribe/drive/baidu/qrcode.py")
files_mod = load_module(PKG + ".drive.baidu.files", "plugins.v2/cloudsubscribe/drive/baidu/files.py")
share_mod = load_module(PKG + ".drive.baidu.share", "plugins.v2/cloudsubscribe/drive/baidu/share.py")
provider_mod = load_module(PKG + ".drive.baidu.provider", "plugins.v2/cloudsubscribe/drive/baidu/provider.py")
load_module(PKG + ".core.definitions", "plugins.v2/cloudsubscribe/core/definitions.py")
definition_mod = load_module(PKG + ".drive.baidu.definition", "plugins.v2/cloudsubscribe/drive/baidu/definition.py")
native_mod = load_module(PKG + ".core.services.native_save", "plugins.v2/cloudsubscribe/core/services/native_save.py")
register_package(PKG + ".search")
load_module(PKG + ".search.types", "plugins.v2/cloudsubscribe/search/types.py")
register_module(PKG + ".search.matching", positive_ints=lambda values: {int(v) for v in values if int(v) > 0})
register_module(PKG + ".core.media", media_identity=lambda media: ("themoviedb", str(media.tmdb_id)),
                recognize_media=MagicMock(return_value=types.SimpleNamespace(title="测试电影", year="2026", tmdb_id=123)),
                tmdb_id_of=lambda media: media.tmdb_id)
api_mod = load_module(PKG + ".core.api.sync", "plugins.v2/cloudsubscribe/core/api/sync.py")

BaiduClient = client_mod.BaiduClient
BaiduFileService = files_mod.BaiduFileService
BaiduShareService = share_mod.BaiduShareService
LINK = "https://pan.baidu.com/s/1Example_ID?pwd=aB12"


def raw(fid, name, is_dir=False, path=None):
    return {"fs_id": fid, "server_filename": name, "isdir": int(is_dir),
            "path": path or f"/分享/{name}", "size": 0 if is_dir else 100, "md5": "hash"}


def fake_client():
    client = MagicMock()
    client.is_success.side_effect = BaiduClient.is_success
    client.risk_cooldown = 1800
    return client


def share_fixture(rows=None):
    client, files = fake_client(), MagicMock()
    files.resolve_directory.return_value = cloud.DirectoryLookup(True, "/影视")
    root = {"list": rows if rows is not None else [raw(11, "剧集", True), raw(12, "电影.mkv")],
            "shareid": "share-id", "uk": "user-id", "seckey": "share%2Bkey", "has_more": False}

    def request(method, endpoint, **kw):
        if endpoint == "share/wxlist":
            if kw["data"]["dir"]:
                return {"errno": 0, "data": {"list": [raw(13, "E01.mkv", path="/分享/剧集/E01.mkv")], "has_more": False}}
            return {"errno": 0, "data": root}
        if endpoint == "share/transfer":
            return {"errno": 0}
        raise AssertionError(endpoint)

    client.request.side_effect = request
    return BaiduShareService(client, files), client, files, root


class TestBaiduClient(unittest.TestCase):
    def test_requires_explicit_success_code(self):
        for value in ({}, {"message": "ok"}, {"errno": -9}, None):
            self.assertFalse(BaiduClient.is_success(value))
        self.assertTrue(BaiduClient.is_success({"errno": "0"}))

    def test_cookie_jar_keeps_credentials_and_changes_only_share_session(self):
        client = BaiduClient("BDUSS=private; STOKEN=session; BDCLND=old")
        client._session.request = MagicMock(return_value=types.SimpleNamespace(status_code=200, json=lambda: {"errno": 0}))
        client.rate_limiter._before_request = lambda: None
        client.request("POST", "share/transfer", share_key="new")
        self.assertEqual(client._session.cookies.get("BDUSS"), "private")
        self.assertEqual(client._session.cookies.get("BDCLND"), "new")
        self.assertEqual(client._session.request.call_count, 1)

    def test_write_timeout_is_not_replayed_and_does_not_expose_token(self):
        client = BaiduClient("BDUSS=private")
        client.rate_limiter._before_request = lambda: None
        client._session.request = MagicMock(side_effect=requests.Timeout("url?bdstoken=secret"))
        result = client.request("POST", "share/transfer")
        self.assertFalse(client.is_success(result))
        self.assertNotIn("secret", str(result))
        self.assertEqual(client._session.request.call_count, 1)

    def test_login_rejects_empty_token_and_caches_valid_token(self):
        client = BaiduClient("BDUSS=private")
        client.request = MagicMock(return_value={"errno": 0, "result": {"bdstoken": ""}})
        self.assertFalse(client.check_login())
        client.request.return_value = {"errno": 0, "result": {"bdstoken": "token", "uk": 10}}
        self.assertEqual(client.get_token(), "token")
        self.assertEqual(client.get_token(), "token")
        self.assertEqual(client.request.call_count, 2)


class TestBaiduShare(unittest.TestCase):
    def test_extract_query_text_and_init_password(self):
        for link in (LINK, "链接：https://pan.baidu.com/s/1Example_ID 提取码：aB12",
                     "https://yun.baidu.com/share/init?surl=Example_ID&pwd=aB12"):
            with self.subTest(link=link):
                info = BaiduShareService.extract_share_info(link)
                self.assertEqual(info["share_id"], "Example_ID")
                self.assertEqual(info["password"], "aB12")
        self.assertEqual(BaiduShareService.extract_share_info("https://pan.baidu.com.evil/s/1foo"), {})

    def test_preview_directories_and_recursive_relative_paths(self):
        share, client, _, _ = share_fixture()
        rows = share.list_share_directory(LINK)
        self.assertEqual([row["id"] for row in rows], ["11", "12"])
        nested = share.list_share_directory(LINK, "11")
        self.assertEqual(nested[0]["relative_path"], "剧集/E01.mkv")
        self.assertEqual(nested[0]["parent_path"], "剧集")
        self.assertEqual(len(share.list_share_files(LINK)), 3)
        self.assertEqual(client.request.call_args.kwargs["data"]["dir"], "/分享/剧集")

    def test_root_pagination_and_error_do_not_return_partial_results(self):
        share, client, _, root = share_fixture([raw(1, "a.mkv")])
        root["has_more"] = True
        client.request.side_effect = [
            {"errno": 0, "data": root},
            {"errno": 0, "data": {"list": [raw(2, "b.mkv")], "has_more": False}},
        ]
        self.assertEqual([row["id"] for row in share.list_share_directory(LINK)], ["1", "2"])
        self.assertEqual(client.request.call_args.kwargs["data"]["page"], 2)
        share._shares.clear()
        client.request.side_effect = [{"errno": 0, "data": root}, {"errno": -9, "message": "提取码错误"}]
        with self.assertRaisesRegex(RuntimeError, "提取码错误"):
            share.list_share_files(LINK)

    def test_transfer_uses_share_session_and_baidu_destination(self):
        share, client, files, _ = share_fixture()
        self.assertTrue(share.transfer_file(LINK, "12", "/影视"))
        args = client.request.call_args.kwargs
        self.assertEqual(args["data"]["path"], "/影视")
        self.assertEqual(json.loads(args["data"]["fsidlist"]), [12])
        self.assertEqual(args["share_key"], "share%2Bkey")
        self.assertEqual(json.loads(args["data"]["extra"])["sekey"], "share+key")
        self.assertTrue(args["authenticated"])
        files.refresh_directories.assert_called()

    def test_unknown_selected_file_is_never_sent_to_transfer_api(self):
        share, client, _, _ = share_fixture()
        with self.assertRaisesRegex(ValueError, "所选文件"):
            share.transfer_file(LINK, "999", "/影视")
        self.assertFalse(any(call.args[1] == "share/transfer" for call in client.request.call_args_list))

    def test_risk_failure_stops_following_batches(self):
        share, client, _, _ = share_fixture()
        share.list_share_directory(LINK)
        client.request.side_effect = lambda *a, **kw: {"errno": 8001, "message": "需要验证"}
        succeeded, failed = share.transfer_files_batch(LINK, ["11", "12"], "/影视", batch_size=1)
        self.assertEqual(succeeded, [])
        self.assertEqual(failed, ["11", "12"])
        self.assertTrue(share.transfer_risk_blocked)
        self.assertEqual(client.request.call_count, 2)

    def test_partial_error_is_not_success(self):
        share, client, _, _ = share_fixture()
        share.list_share_directory(LINK)
        client.request.side_effect = lambda *a, **kw: {"errno": 0, "info": [{"fs_id": 12, "errno": -30}]}
        good, failed = share.transfer_files_batch(LINK, ["12"], "/影视")
        self.assertEqual((good, failed), ([], ["12"]))
        self.assertIn("部分", share.last_error)


class TestBaiduFiles(unittest.TestCase):
    def test_directory_lookup_create_and_pagination(self):
        client = fake_client()
        rows = [raw(1, "a.mkv", path="/a.mkv"), raw(2, "目录", True, path="/目录")]

        def request(method, endpoint, **kw):
            if endpoint == "api/list":
                if kw["params"]["dir"] == "/":
                    page = kw["params"]["page"]
                    return {"errno": 0, "list": [rows[page - 1]], "has_more": page < 2}
                return {"errno": 0, "list": [], "has_more": False}
            if endpoint == "api/create":
                return {"errno": 0, "fs_id": 3}
            raise AssertionError(endpoint)

        client.request.side_effect = request
        files = BaiduFileService(client, page_size=1)
        self.assertEqual(files.resolve_directory("/目录").directory_id, "/目录")
        self.assertEqual(files.resolve_directory("/新建", create=True).directory_id, "/新建")
        self.assertEqual(files.list_directories("/")[0]["path"], "/目录")

    def test_batch_rename_returns_new_path_id_for_subsequent_move(self):
        client = fake_client()
        client.file_manager.return_value = {"errno": 0}
        files = BaiduFileService(client)
        item = files_mod.cloud_file(raw(1, "old.mkv", path="/旧/old.mkv"))
        renamed = files.rename_files("/旧", {"1": {"item": item, "target_name": "new.mkv"}})
        self.assertEqual(renamed["1"].id, "/旧/new.mkv")
        self.assertEqual(renamed["1"].native["path"], "/旧/new.mkv")

    def test_registration_exposes_only_implemented_capabilities(self):
        provider = provider_mod.create_baidu_provider(fake_client())
        self.assertTrue(provider.supports(cloud.CloudDriveCapability.SHARE_TRANSFER))
        self.assertFalse(provider.supports(cloud.CloudDriveCapability.FILE_DOWNLOAD))
        self.assertFalse(provider.supports(cloud.CloudDriveCapability.LOCAL_UPLOAD))
        fields = definition_mod.BaiduDriverDefinition.get_config_groups()[0].fields
        self.assertIn("baidu_cookie", [field.key for field in fields])
        self.assertEqual(definition_mod.BaiduDriverDefinition.get_transfer_path_key(), "baidu_transfer_path")


class TestNativeSave(unittest.TestCase):
    def make_owner(self):
        owner = MagicMock()
        owner._cloud_drive = types.SimpleNamespace(key="115")
        owner._baidu_transfer_path = "/百度暂存"
        owner._batch_size, owner._batch_interval = 20, 0
        owner._subscribe_search_queue_shutdown = threading.Event()
        owner._stop_requested.return_value = False
        owner._sync_tasks_lock = threading.RLock()
        owner._sync_tasks = {}
        owner._release_sync_resources.side_effect = lambda _: native_mod.sync_lock.release()
        owner._notify = False
        provider = provider_mod.create_baidu_provider(fake_client())
        provider.require(cloud.CloudDriveCapability.AUTHENTICATION).check_login.return_value = True
        owner._cloud_drive_registry = cloud.CloudDriveRegistry([provider])
        return owner, provider

    def test_save_keeps_main_drive_and_uses_baidu_path_and_actual_file(self):
        owner, provider = self.make_owner()
        share = provider.services[cloud.CloudDriveCapability.SHARE_TRANSFER]
        files = provider.services[cloud.CloudDriveCapability.FILE_QUERY]
        share.list_share_directory = MagicMock(return_value=[
            {"id": "12", "name": "电影.mkv", "is_dir": False, "size": 100, "relative_path": "电影.mkv"},
        ])
        share.transfer_files_batch = MagicMock(return_value=(["12"], []))
        files.find_file_strict = MagicMock(return_value=cloud.CloudFile("/百度暂存/电影.mkv", "电影.mkv", False, 100))
        result = {}
        self.assertTrue(native_mod.NativeShareSaveService(owner).run("baidu", [{"url": LINK}], {"title": "电影"}, result))
        self.assertTrue(result["success"])
        self.assertEqual(owner._cloud_drive.key, "115")
        self.assertEqual(share.transfer_files_batch.call_args.args[2], "/百度暂存")
        record = owner._sync_handler.append_history_records.call_args.args[0][0]
        self.assertEqual(record["target_drive_key"], "baidu")
        self.assertEqual(record["status"], "成功")

    def test_missing_actual_file_cannot_report_success(self):
        owner, provider = self.make_owner()
        share = provider.services[cloud.CloudDriveCapability.SHARE_TRANSFER]
        share.list_share_directory = MagicMock(return_value=[
            {"id": "12", "name": "电影.mkv", "is_dir": False, "size": 100, "relative_path": "电影.mkv"},
        ])
        share.transfer_files_batch = MagicMock(return_value=(["12"], []))
        provider.services[cloud.CloudDriveCapability.FILE_QUERY].find_file_strict = MagicMock(return_value=None)
        result = {}
        self.assertFalse(native_mod.NativeShareSaveService(owner).run("baidu", [{"url": LINK}], result=result))
        self.assertFalse(result["success"])
        record = owner._sync_handler.append_history_records.call_args.args[0][0]
        self.assertEqual(record["status"], "失败")

    def test_folder_and_its_selected_child_are_saved_once(self):
        owner, provider = self.make_owner()
        share = provider.services[cloud.CloudDriveCapability.SHARE_TRANSFER]
        share.list_share_files = MagicMock(return_value=[
            {"id": "11", "name": "剧集", "is_dir": True, "size": 0, "relative_path": "剧集"},
            {"id": "12", "name": "E01.mkv", "is_dir": False, "size": 100, "relative_path": "剧集/E01.mkv"},
        ])
        share.transfer_files_batch = MagicMock(return_value=(["11"], []))
        provider.services[cloud.CloudDriveCapability.FILE_QUERY].find_file_strict = MagicMock(
            return_value=cloud.CloudFile("/百度暂存/剧集", "剧集", True))
        self.assertTrue(native_mod.NativeShareSaveService(owner).run("baidu", [
            {"url": LINK, "target_file_ids": ["11", "12"]},
        ]))
        self.assertEqual(share.transfer_files_batch.call_args.args[1], ["11"])


class TestBaiduManualApi(unittest.TestCase):
    def setUp(self):
        api_mod._RECENT_MANUAL_SUBMITS.clear()
        self.owner = types.SimpleNamespace(
            chain=None, _search_handler=None,
            _cloud_drive=types.SimpleNamespace(key="115", supports=lambda cap: False),
            _resource_type_order=["115"], _submit_sync_operation=MagicMock(),
            _resolve_manual_tmdb_media=lambda **kw: types.SimpleNamespace(
                title="测试电影", year="2026", tmdb_id=123),
        )
        provider = provider_mod.create_baidu_provider(fake_client())
        self.owner._cloud_drive_registry = cloud.CloudDriveRegistry([provider])

    def payload(self):
        return {"save_to_source": True, "target_cloud": "baidu",
                "media": {"tmdb_id": 123, "media_type": "movie"},
                "resources": [{"url": LINK, "resource_type": "baidu", "target_file_ids": ["12"]}]}

    def test_manual_api_queues_native_save_and_preserves_file_selection(self):
        result = api_mod.SyncApi(self.owner).api_vue_start_manual_sync(self.payload())
        self.assertTrue(result["success"], result)
        args = self.owner._submit_sync_operation.call_args.args[0]
        self.assertEqual(args["native_save_provider"], "baidu")
        self.assertEqual(args["manual_resources"][0]["target_file_ids"], ["12"])
        self.assertFalse(args["manual_resources"][0]["is_cross"])
        self.assertEqual(self.owner._cloud_drive.key, "115")

    def test_unconfigured_baidu_is_rejected_before_queueing(self):
        self.owner._cloud_drive_registry = cloud.CloudDriveRegistry()
        result = api_mod.SyncApi(self.owner).api_vue_start_manual_sync(self.payload())
        self.assertFalse(result["success"])
        self.owner._submit_sync_operation.assert_not_called()


class BaiduQRCodeTests(unittest.TestCase):
    def setUp(self):
        self.service = qrcode_mod.BaiduQRCodeService()
        self.session = MagicMock()
        self.addCleanup(self.service.close)

    @staticmethod
    def response(value):
        return MagicMock(text=json.dumps(value))

    def create(self):
        self.session.get.side_effect = [MagicMock(), self.response({"errno": 0, "sign": "test-sign"}),
                                        MagicMock(content=b"\x89PNG\r\n\x1a\ntest")]
        with patch.object(qrcode_mod.requests, "Session", return_value=self.session):
            return self.service.create_qrcode_login()

    def test_creation_keeps_passport_session_on_server(self):
        result = self.create()
        self.assertTrue(result["qrcode"].startswith("data:image/png;base64,"))
        self.assertNotIn("sign", result)
        self.assertNotIn("cookie", result)
        self.assertEqual(self.service._sessions[result["qr_token"]]["sign"], "test-sign")
        definition = definition_mod.BaiduDriverDefinition
        self.assertIsInstance(definition.create_qrcode_service(), qrcode_mod.BaiduQRCodeService)
        self.assertEqual(definition.qrcode_config_values({"cookie": "BDUSS=test"}),
                         {"baidu_cookie": "BDUSS=test"})
        field = next(f for g in definition.get_config_groups() for f in g.fields if f.key == "baidu_cookie")
        self.assertEqual(field.scan_provider, "baidu")

    def test_pending_and_timeout_do_not_discard_session(self):
        token = self.create()["qr_token"]
        self.session.get.side_effect = [self.response({"errno": 1}), requests.exceptions.Timeout()]
        for _ in range(2):
            self.assertEqual(self.service.check_qrcode_login(qr_token=token)["status"], "waiting")
        self.assertIn(token, self.service._sessions)

    def test_expiration_closes_session_and_invalid_token_does_not_request(self):
        token = self.create()["qr_token"]
        self.service._sessions[token]["expires"] = 0
        count = self.session.get.call_count
        self.assertEqual(self.service.check_qrcode_login(qr_token=token)["status"], "expired")
        self.assertEqual(self.service.check_qrcode_login(qr_token="unknown")["status"], "expired")
        self.assertEqual(self.session.get.call_count, count)
        self.session.close.assert_called_once()

    def test_success_exchanges_ticket_and_requires_cookie(self):
        for has_cookie in (True, False):
            with self.subTest(has_cookie=has_cookie):
                token = self.create()["qr_token"]
                self.session.cookies = requests.cookies.RequestsCookieJar()
                if has_cookie:
                    self.session.cookies.set("BDUSS", "test-login", domain=".baidu.com")
                    self.session.cookies.set("STOKEN", "test-token", domain="pan.baidu.com")
                self.session.get.side_effect = [self.response({"errno": 0, "channel_v": json.dumps(
                    {"status": 0, "v": "temporary-ticket"})}), self.response({"errInfo": {"no": "0"}}), MagicMock()]
                if has_cookie:
                    result = self.service.check_qrcode_login(qr_token=token)
                    self.assertEqual(result["status"], "success")
                    self.assertIn("BDUSS=test-login", result["cookie"])
                    self.assertIn("STOKEN=test-token", result["cookie"])
                    self.assertNotIn("temporary-ticket", result["cookie"])
                else:
                    with self.assertRaisesRegex(RuntimeError, "Cookie"):
                        self.service.check_qrcode_login(qr_token=token)
                self.assertNotIn(token, self.service._sessions)

    def test_jsonp_and_invalid_qrcode(self):
        self.assertEqual(self.service._payload(MagicMock(text='callback({"errno":0});')), {"errno": 0})
        self.session.get.side_effect = [MagicMock(), self.response({"sign": "s"}), MagicMock(content=b"html")]
        with patch.object(qrcode_mod.requests, "Session", return_value=self.session):
            with self.assertRaisesRegex(RuntimeError, "图片无效"):
                self.service.create_qrcode_login()
        self.assertFalse(self.service._sessions)
        self.session.close.assert_called_once()

    def test_failed_ticket_exchange_discards_session_without_credentials(self):
        token = self.create()["qr_token"]
        self.session.get.side_effect = [self.response({"errno": 0, "channel_v": {
            "status": 0, "v": "temporary-ticket"}}), self.response({"errInfo": {"no": "400"}})]
        with self.assertRaisesRegex(RuntimeError, "账号验证"):
            self.service.check_qrcode_login(qr_token=token)
        self.assertNotIn(token, self.service._sessions)
        self.session.close.assert_called_once()

    def test_expired_passport_response_discards_session(self):
        token = self.create()["qr_token"]
        self.session.get.side_effect = [self.response({"errno": 2})]
        self.assertEqual(self.service.check_qrcode_login(qr_token=token)["status"], "expired")
        self.assertNotIn(token, self.service._sessions)
        self.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
