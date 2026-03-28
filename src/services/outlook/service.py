"""
Outlook 邮箱服务主类
支持多种 IMAP/API 连接方式，自动故障切换
"""

import logging
import threading
import time
from typing import Optional, Dict, Any, List

from ..base import BaseEmailService, EmailServiceError, EmailServiceStatus, EmailServiceType
from ...config.constants import EmailServiceType as ServiceType
from ...config.settings import get_settings
from .account import OutlookAccount
from .base import ProviderType, EmailMessage
from .email_parser import EmailParser, get_email_parser
from .health_checker import HealthChecker, FailoverManager
from .providers.base import OutlookProvider, ProviderConfig
from .providers.imap_old import IMAPOldProvider
from .providers.imap_new import IMAPNewProvider
from .providers.graph_api import GraphAPIProvider


logger = logging.getLogger(__name__)
MAILBOX_POLL_SEQUENCE = ("INBOX", "JUNK")
OUTLOOK_POLL_MAX_ROUNDS = 5
OUTLOOK_POLL_PREFER_UNSEEN_ROUNDS = 3


# 默认提供者优先级
# IMAP_OLD 最兼容（只需 login.live.com token），IMAP_NEW 次之，Graph API 最后
# 原因：部分 client_id 没有 Graph API 权限，但有 IMAP 权限
DEFAULT_PROVIDER_PRIORITY = [
    ProviderType.IMAP_OLD,
    ProviderType.IMAP_NEW,
    ProviderType.GRAPH_API,
]


def build_outlook_code_poll_kwargs(
    *,
    timeout: int,
    lookback_seconds: int,
    fetch_count: int,
    strict_unseen_only: bool = False,
) -> Dict[str, Any]:
    """Build a consistent polling profile for Outlook inbox/test actions."""
    return {
        "timeout": timeout,
        "pattern": r"(?<!\d)(\d{6})(?!\d)",
        "allow_any_sender": True,
        "lookback_seconds": lookback_seconds,
        "prefer_unseen_rounds": OUTLOOK_POLL_PREFER_UNSEEN_ROUNDS,
        "fetch_count": fetch_count,
        "strict_unseen_only": strict_unseen_only,
        "max_poll_rounds": OUTLOOK_POLL_MAX_ROUNDS,
    }


def get_email_code_settings() -> dict:
    """获取验证码等待配置"""
    settings = get_settings()
    return {
        "timeout": settings.email_code_timeout,
        "poll_interval": settings.email_code_poll_interval,
    }


class OutlookService(BaseEmailService):
    """
    Outlook 邮箱服务
    支持多种 IMAP/API 连接方式，自动故障切换
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Outlook 服务

        Args:
            config: 配置字典，支持以下键:
                - accounts: Outlook 账户列表
                - provider_priority: 提供者优先级列表
                - health_failure_threshold: 连续失败次数阈值
                - health_disable_duration: 禁用时长（秒）
                - timeout: 请求超时时间
                - proxy_url: 代理 URL
            name: 服务名称
        """
        super().__init__(ServiceType.OUTLOOK, name)

        # 默认配置
        default_config = {
            "accounts": [],
            "provider_priority": [p.value for p in DEFAULT_PROVIDER_PRIORITY],
            "health_failure_threshold": 5,
            "health_disable_duration": 60,
            "timeout": 30,
            "proxy_url": None,
        }

        self.config = {**default_config, **(config or {})}

        # 解析提供者优先级
        self.provider_priority = [
            ProviderType(p) for p in self.config.get("provider_priority", [])
        ]
        if not self.provider_priority:
            self.provider_priority = DEFAULT_PROVIDER_PRIORITY

        # 提供者配置
        self.provider_config = ProviderConfig(
            timeout=self.config.get("timeout", 30),
            proxy_url=self.config.get("proxy_url"),
            health_failure_threshold=self.config.get("health_failure_threshold", 3),
            health_disable_duration=self.config.get("health_disable_duration", 300),
        )

        # 获取默认 client_id（供无 client_id 的账户使用）
        try:
            _default_client_id = get_settings().outlook_default_client_id
        except Exception:
            _default_client_id = "24d9a0ed-8787-4584-883c-2fd79308940a"

        # 解析账户
        self.accounts: List[OutlookAccount] = []
        self._current_account_index = 0
        self._account_lock = threading.Lock()

        # 支持两种配置格式
        if "email" in self.config and "password" in self.config:
            account = OutlookAccount.from_config(self.config)
            if not account.client_id and _default_client_id:
                account.client_id = _default_client_id
            if account.validate():
                self.accounts.append(account)
        else:
            for account_config in self.config.get("accounts", []):
                account = OutlookAccount.from_config(account_config)
                if not account.client_id and _default_client_id:
                    account.client_id = _default_client_id
                if account.validate():
                    self.accounts.append(account)

        if not self.accounts:
            logger.warning("未配置有效的 Outlook 账户")

        # 健康检查器和故障切换管理器
        self.health_checker = HealthChecker(
            failure_threshold=self.provider_config.health_failure_threshold,
            disable_duration=self.provider_config.health_disable_duration,
        )
        self.failover_manager = FailoverManager(
            health_checker=self.health_checker,
            priority_order=self.provider_priority,
        )

        # 邮件解析器
        self.email_parser = get_email_parser()

        # 提供者实例缓存: (email, provider_type) -> OutlookProvider
        self._providers: Dict[tuple, OutlookProvider] = {}
        self._provider_lock = threading.Lock()

        # IMAP 连接限制（防止限流）
        self._imap_semaphore = threading.Semaphore(5)

        # 验证码去重机制
        self._used_codes: Dict[str, set] = {}

    def _get_provider(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
    ) -> OutlookProvider:
        """
        获取或创建提供者实例

        Args:
            account: Outlook 账户
            provider_type: 提供者类型

        Returns:
            提供者实例
        """
        cache_key = (account.email.lower(), provider_type)

        with self._provider_lock:
            if cache_key not in self._providers:
                provider = self._create_provider(account, provider_type)
                self._providers[cache_key] = provider

            return self._providers[cache_key]

    def _create_provider(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
    ) -> OutlookProvider:
        """
        创建提供者实例

        Args:
            account: Outlook 账户
            provider_type: 提供者类型

        Returns:
            提供者实例
        """
        if provider_type == ProviderType.IMAP_OLD:
            return IMAPOldProvider(account, self.provider_config)
        elif provider_type == ProviderType.IMAP_NEW:
            return IMAPNewProvider(account, self.provider_config)
        elif provider_type == ProviderType.GRAPH_API:
            return GraphAPIProvider(account, self.provider_config)
        else:
            raise ValueError(f"未知的提供者类型: {provider_type}")

    def _get_provider_priority_for_account(self, account: OutlookAccount) -> List[ProviderType]:
        """Return provider priority list for one polling round."""
        return list(self.provider_priority)

    def _try_providers_for_emails(
        self,
        account: OutlookAccount,
        count: int = 20,
        only_unseen: bool = True,
        mailboxes: Optional[List[str]] = None,
        deadline_ts: Optional[float] = None,
    ) -> List[EmailMessage]:
        """
        尝试多个提供者获取邮件

        Args:
            account: Outlook 账户
            count: 获取数量
            only_unseen: 是否只获取未读

        Returns:
            邮件列表
        """
        errors = []
        all_emails: List[EmailMessage] = []
        seen_ids = set()

        # 根据账户类型选择合适的提供者优先级
        priority = self._get_provider_priority_for_account(account)
        mailbox_order = mailboxes or list(MAILBOX_POLL_SEQUENCE)

        # In each polling round, iterate all providers and all target mailboxes.
        for provider_type in priority:
            if deadline_ts and time.time() >= deadline_ts:
                break

            if provider_type in (ProviderType.IMAP_NEW, ProviderType.GRAPH_API) and not account.has_oauth():
                logger.debug(f"[{account.email}] 跳过 {provider_type.value}（无 OAuth 配置）")
                continue

            # 检查提供者是否可用
            if not self.health_checker.is_available(provider_type):
                logger.debug(
                    f"[{account.email}] {provider_type.value} 不可用，跳过"
                )
                continue

            try:
                provider = self._get_provider(account, provider_type)

                with self._imap_semaphore:
                    with provider:
                        got_any = False
                        for mailbox in mailbox_order:
                            if deadline_ts and time.time() >= deadline_ts:
                                break
                            if deadline_ts:
                                remaining = deadline_ts - time.time()
                                provider_cfg = getattr(provider, "config", None)
                                provider_timeout = getattr(provider_cfg, "timeout", None)
                                if provider_timeout is None:
                                    provider_timeout = 30
                                if remaining < provider_timeout:
                                    logger.debug(
                                        f"[{account.email}] 跳过 {provider_type.value}:{mailbox}，"
                                        f"剩余超时预算 {remaining:.1f}s < provider 超时 {provider_timeout}s"
                                    )
                                    break
                            emails = provider.get_recent_emails(
                                count=count,
                                only_unseen=only_unseen,
                                mailbox=mailbox,
                            )
                            if not emails:
                                continue

                            got_any = True
                            logger.debug(
                                f"[{account.email}] {provider_type.value}:{mailbox} 获取到 {len(emails)} 封邮件"
                            )
                            for item in emails:
                                msg_id = item.id or ""
                                dedup_key = f"{provider_type.value}:{mailbox}:{msg_id}:{item.received_timestamp}"
                                if dedup_key in seen_ids:
                                    continue
                                seen_ids.add(dedup_key)
                                all_emails.append(item)

                        # One successful provider pass resets its health failures.
                        if got_any:
                            self.health_checker.record_success(provider_type)

            except Exception as e:
                error_msg = str(e)
                errors.append(f"{provider_type.value}: {error_msg}")
                self.health_checker.record_failure(provider_type, error_msg)
                logger.warning(
                    f"[{account.email}] {provider_type.value} 获取邮件失败: {e}"
                )

        if errors and not all_emails:
            logger.error(
                f"[{account.email}] 所有提供者都失败: {'; '.join(errors)}"
            )
        return all_emails

    def trigger_test_verification_email(
        self,
        email: str,
        code: Optional[str] = None,
    ) -> Optional[str]:
        """
        Inject a test OTP mail into target mailbox.
        Returns injected code when successful.
        """
        account = None
        for acc in self.accounts:
            if acc.email.lower() == email.lower():
                account = acc
                break
        if not account:
            logger.warning(f"[{email}] 自动触发测试验证码失败: 未找到对应账户")
            return None

        import secrets
        test_code = code or f"{secrets.randbelow(10**6):06d}"
        priority = self._get_provider_priority_for_account(account)

        for provider_type in priority:
            if provider_type in (ProviderType.IMAP_NEW, ProviderType.GRAPH_API) and not account.has_oauth():
                continue

            try:
                provider = self._get_provider(account, provider_type)
                injector = getattr(provider, "inject_test_code_email", None)
                if not callable(injector):
                    continue

                if provider_type in (ProviderType.IMAP_OLD, ProviderType.IMAP_NEW):
                    with self._imap_semaphore:
                        with provider:
                            if injector(test_code, mailbox="INBOX"):
                                logger.info(
                                    f"[{email}] 自动触发测试验证码成功: provider={provider_type.value}, code={test_code}"
                                )
                                return test_code
                else:
                    with provider:
                        if injector(test_code, mailbox="INBOX"):
                            logger.info(
                                f"[{email}] 自动触发测试验证码成功: provider={provider_type.value}, code={test_code}"
                            )
                            return test_code
            except Exception as e:
                logger.warning(
                    f"[{email}] 自动触发测试验证码失败: provider={provider_type.value}, error={e}"
                )

        logger.warning(f"[{email}] 自动触发测试验证码失败: 所有可用收件通道均失败")
        return None

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        选择可用的 Outlook 账户

        Args:
            config: 配置参数（未使用）

        Returns:
            包含邮箱信息的字典
        """
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有可用的 Outlook 账户"))
            raise EmailServiceError("没有可用的 Outlook 账户")

        # 轮询选择账户
        with self._account_lock:
            account = self.accounts[self._current_account_index]
            self._current_account_index = (self._current_account_index + 1) % len(self.accounts)

        email_info = {
            "email": account.email,
            "service_id": account.email,
            "account": {
                "email": account.email,
                "has_oauth": account.has_oauth()
            }
        }

        logger.info(f"选择 Outlook 账户: {account.email}")
        self.update_status(True)
        return email_info

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = None,
        pattern: str = None,
        otp_sent_at: Optional[float] = None,
        allow_any_sender: bool = False,
        lookback_seconds: int = 60,
        prefer_unseen_rounds: int = OUTLOOK_POLL_PREFER_UNSEEN_ROUNDS,
        fetch_count: int = 15,
        strict_unseen_only: bool = False,
        max_poll_rounds: Optional[int] = None,
    ) -> Optional[str]:
        """
        从 Outlook 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用
            timeout: 超时时间（秒）
            pattern: 验证码正则表达式（未使用）
            otp_sent_at: OTP 发送时间戳

        Returns:
            验证码字符串
        """
        # 查找对应的账户
        account = None
        for acc in self.accounts:
            if acc.email.lower() == email.lower():
                account = acc
                break

        if not account:
            self.update_status(False, EmailServiceError(f"未找到邮箱对应的账户: {email}"))
            return None

        # 获取验证码等待配置
        code_settings = get_email_code_settings()
        actual_timeout = timeout or code_settings["timeout"]
        poll_interval = code_settings["poll_interval"]

        logger.info(
            f"[{email}] 开始获取验证码，超时 {actual_timeout}s，"
            f"提供者优先级: {[p.value for p in self.provider_priority]}"
        )

        # 初始化验证码去重集合
        if email not in self._used_codes:
            self._used_codes[email] = set()
        used_codes = self._used_codes[email]

        # 计算最小时间戳（留出 60 秒时钟偏差）
        safe_lookback = max(0, int(lookback_seconds))
        min_timestamp = int(otp_sent_at - safe_lookback) if otp_sent_at else 0
        logger.info(
            f"[{email}] 验证码筛选窗口: min_timestamp={min_timestamp}, "
            f"allow_any_sender={allow_any_sender}, pattern={'default' if not pattern else pattern}"
        )

        start_time = time.time()
        deadline_ts = start_time + actual_timeout
        poll_count = 0

        while time.time() < deadline_ts and (
            max_poll_rounds is None or poll_count < max(0, int(max_poll_rounds))
        ):
            poll_count += 1

            # 未读策略:
            # - strict_unseen_only=True: 全程仅检索未读
            # - 否则按 prefer_unseen_rounds 渐进切换
            only_unseen = True if strict_unseen_only else (poll_count <= max(0, int(prefer_unseen_rounds)))

            try:
                # 尝试多个提供者获取邮件
                emails = self._try_providers_for_emails(
                    account,
                    count=max(1, int(fetch_count)),
                    only_unseen=only_unseen,
                    mailboxes=list(MAILBOX_POLL_SEQUENCE),
                    deadline_ts=deadline_ts,
                )

                if emails:
                    logger.info(
                        f"[{email}] 第 {poll_count} 轮获取到 {len(emails)} 封候选邮件 "
                        f"(only_unseen={only_unseen}, fetch_count={fetch_count})"
                    )
                else:
                    logger.info(
                        f"[{email}] 第 {poll_count} 轮未获取到候选邮件 "
                        f"(only_unseen={only_unseen}, fetch_count={fetch_count})"
                    )

                # 从邮件中查找验证码
                code = self.email_parser.find_verification_code_in_emails(
                    emails,
                    target_email=email,
                    min_timestamp=min_timestamp,
                    used_codes=used_codes,
                    pattern=pattern,
                    allow_any_sender=allow_any_sender,
                )

                if code:
                    used_codes.add(code)
                    elapsed = int(time.time() - start_time)
                    logger.info(
                        f"[{email}] 找到验证码: {code}，"
                        f"总耗时 {elapsed}s，轮询 {poll_count} 次"
                    )
                    self.update_status(True)
                    return code

            except Exception as e:
                logger.warning(f"[{email}] 检查出错: {e}")

            # 等待下次轮询（不超过总超时截止）
            remaining = deadline_ts - time.time()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))

        elapsed = int(time.time() - start_time)
        logger.warning(f"[{email}] 验证码超时 ({actual_timeout}s)，共轮询 {poll_count} 次")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """列出所有可用的 Outlook 账户"""
        return [
            {
                "email": account.email,
                "id": account.email,
                "has_oauth": account.has_oauth(),
                "type": "outlook"
            }
            for account in self.accounts
        ]

    def delete_email(self, email_id: str) -> bool:
        """删除邮箱（Outlook 不支持删除账户）"""
        logger.warning(f"Outlook 服务不支持删除账户: {email_id}")
        return False

    def check_health(self) -> bool:
        """检查 Outlook 服务是否可用"""
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有配置的账户"))
            return False

        # 测试第一个账户的连接
        test_account = self.accounts[0]

        # 尝试任一提供者连接
        for provider_type in self.provider_priority:
            try:
                provider = self._get_provider(test_account, provider_type)
                if provider.test_connection():
                    self.update_status(True)
                    return True
            except Exception as e:
                logger.warning(
                    f"Outlook 健康检查失败 ({test_account.email}, {provider_type.value}): {e}"
                )

        self.update_status(False, EmailServiceError("健康检查失败"))
        return False

    def get_provider_status(self) -> Dict[str, Any]:
        """获取提供者状态"""
        return self.failover_manager.get_status()

    def get_account_stats(self) -> Dict[str, Any]:
        """获取账户统计信息"""
        total = len(self.accounts)
        oauth_count = sum(1 for acc in self.accounts if acc.has_oauth())

        return {
            "total_accounts": total,
            "oauth_accounts": oauth_count,
            "password_accounts": total - oauth_count,
            "accounts": [acc.to_dict() for acc in self.accounts],
            "provider_status": self.get_provider_status(),
        }

    def add_account(self, account_config: Dict[str, Any]) -> bool:
        """添加新的 Outlook 账户"""
        try:
            account = OutlookAccount.from_config(account_config)
            if not account.validate():
                return False

            self.accounts.append(account)
            logger.info(f"添加 Outlook 账户: {account.email}")
            return True
        except Exception as e:
            logger.error(f"添加 Outlook 账户失败: {e}")
            return False

    def remove_account(self, email: str) -> bool:
        """移除 Outlook 账户"""
        for i, acc in enumerate(self.accounts):
            if acc.email.lower() == email.lower():
                self.accounts.pop(i)
                logger.info(f"移除 Outlook 账户: {email}")
                return True
        return False

    def reset_provider_health(self):
        """重置所有提供者的健康状态"""
        self.health_checker.reset_all()
        logger.info("已重置所有提供者的健康状态")

    def force_provider(self, provider_type: ProviderType):
        """强制使用指定的提供者"""
        self.health_checker.force_enable(provider_type)
        # 禁用其他提供者
        for pt in ProviderType:
            if pt != provider_type:
                self.health_checker.force_disable(pt, 60)
        logger.info(f"已强制使用提供者: {provider_type.value}")
