"""百度网盘配置与客户端装配。"""

from .client import BaiduClient
from .qrcode import BaiduQRCodeService
from .provider import create_baidu_provider
from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec


class BaiduDriverDefinition(DriverDefinition):
    id = "baidu"
    name = "百度网盘"
    icon = "mdi-cloud-circle-outline"
    order = 80
    qrcode_service_type = BaiduQRCodeService
    qrcode_credentials = {"cookie": "baidu_cookie"}
    qrcode_required_credentials = ("cookie",)
    qrcode_hint = "请使用百度网盘 App 扫码，并在手机上确认登录"

    @classmethod
    def get_config_groups(cls, context=None):
        return [GroupSpec(tab="baidu", title="百度账号", hide_heading=True, fields=[
            FieldSpec(key="baidu_account_info", type="account", account_key="drive:baidu", cols=12),
            FieldSpec(key="baidu_cookie", label="百度网盘 Cookie", type="password", default="", cols=12,
                      scan_provider="baidu",
                      hint="可点击右侧二维码按钮使用百度网盘 App 扫码登录，也可手动填写包含 BDUSS、STOKEN 的完整 Cookie。"),
            FieldSpec(key="baidu_transfer_path", label="网盘转存路径", type="cloud-directory", default="/",
                      placeholder="/", drive_provider="baidu", cols=6,
                      hint="百度分享保存到此目录；资源页的“保存到百度网盘”保留原目录与文件名。"),
            FieldSpec(key="baidu_media_path", label="媒体库目录", type="cloud-directory", default="/",
                      placeholder="/", drive_provider="baidu", cols=6,
                      hint="选百度作为主网盘并启用自动整理时，按平台规则整理到此目录。"),
            FieldSpec(key="baidu_request_timeout", label="请求超时（秒）", type="number", default=30,
                      min=5, max=300, cols=6),
        ])]

    @classmethod
    def create_client(cls, config, context=None):
        cookie = str(config.get("_baidu_cookie") or config.get("baidu_cookie") or "").strip()
        if not cookie:
            return None
        client = BaiduClient(cookie, int(config.get("_baidu_request_timeout") or config.get("baidu_request_timeout") or 30))
        client.risk_cooldown = max(60, int(config.get("_transfer_risk_cooldown") or config.get("transfer_risk_cooldown") or 1800))
        return client

    @classmethod
    def create_provider(cls, client, config, context=None):
        return create_baidu_provider(client) if client else None
