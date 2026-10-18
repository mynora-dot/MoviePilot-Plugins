"""百度网盘的能力注册。仅声明已经实现的文件和分享能力。"""

from .files import BaiduFileService
from .share import BaiduShareService
from ...core.cloud import CloudDriveCapability as Cap, CloudDrivePolicy, CloudDriveProvider


def create_baidu_provider(client) -> CloudDriveProvider:
    files = BaiduFileService(client)
    return CloudDriveProvider(
        key="baidu", name="百度网盘", config_prefix="baidu", resource_types=frozenset({"baidu"}),
        services={Cap.AUTHENTICATION: client, Cap.ACCOUNT: client,
                  Cap.SHARE_TRANSFER: BaiduShareService(client, files),
                  Cap.DIRECTORY_READ: files, Cap.FILE_QUERY: files, Cap.FILE_MUTATION: files},
        policy=CloudDrivePolicy(pagination_mode="offset", max_page_size=100,
                                supports_batch=True, max_batch_size=100, max_concurrency=1),
    )
