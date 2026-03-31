"""
Freemail 邮箱服务实现
基于自部署 Cloudflare Worker 临时邮箱服务 (https://github.com/idinging/freemail)
"""

import re
import time
import logging
import random
import string
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN

logger = logging.getLogger(__name__)


class FreemailService(BaseEmailService):
    """
    Freemail 邮箱服务
    基于自部署 Cloudflare Worker 的临时邮箱
    """

    DOMAIN_MODE_SECOND_LEVEL = "second_level"
    DOMAIN_MODE_THIRD_LEVEL = "third_level"
    DOMAIN_MODE_THIRD_RANDOM = "third_random"
    THIRD_LEVEL_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,6}[a-z0-9])$")
    RANDOM_WORD_POOL = (
        "sky", "sun", "moon", "star", "cloud", "wind", "rain", "snow",
        "leaf", "tree", "seed", "root", "bloom", "river", "brook", "lake",
        "stone", "flame", "ember", "glow", "spark", "nova", "comet", "orbit",
        "dawn", "dusk", "zen", "calm", "brave", "swift", "alpha", "omega",
        "pixel", "codec", "logic", "scope", "frame", "cache", "stack", "queue",
        "node", "link", "mesh", "relay", "router", "portal", "beacon", "signal",
        "mail", "inbox", "letter", "post", "sender", "anchor", "bridge", "harbor",
        "craft", "forge", "atlas", "terra", "forest", "garden", "planet", "meteor",
        "origin", "memory", "future", "vision", "lumen", "pulse", "echo", "vivid",
    )

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Freemail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: Worker 域名地址 (必需)
                - admin_token: Admin Token，对应 JWT_TOKEN (必需)
                - cf_access_client_id: Cloudflare Access Service Token Client ID (可选)
                - cf_access_client_secret: Cloudflare Access Service Token Client Secret (可选)
                - domain: 邮箱域名，如 example.com
                - timeout: 请求超时时间，默认 30
                - max_retries: 最大重试次数，默认 3
            name: 服务名称
        """
        super().__init__(EmailServiceType.FREEMAIL, name)

        required_keys = ["base_url", "admin_token"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = self.config["base_url"].rstrip("/")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        # 缓存 domain 列表
        self._domains = []

    def _get_headers(self) -> Dict[str, str]:
        """构造 admin 请求头"""
        headers = {
            "Authorization": f"Bearer {self.config['admin_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        cf_access_client_id = str(self.config.get("cf_access_client_id") or "").strip()
        cf_access_client_secret = str(self.config.get("cf_access_client_secret") or "").strip()
        if cf_access_client_id and cf_access_client_secret:
            headers["CF-Access-Client-Id"] = cf_access_client_id
            headers["CF-Access-Client-Secret"] = cf_access_client_secret
        return headers

    def _request_via_stdlib(self, method: str, url: str, **kwargs):
        """在 curl_cffi TLS 失败时回退到标准库 urllib。"""
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        headers.setdefault("Accept-Encoding", "identity")

        params = kwargs.get("params")
        if params:
            query = urlencode(params)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"

        body = kwargs.get("data")
        json_body = kwargs.get("json")
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")

        request = Request(
            url=url,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        with urlopen(request, timeout=kwargs.get("timeout", self.config["timeout"])) as response:
            payload = response.read()
            text = payload.decode("utf-8", errors="replace")
            headers = dict(response.headers.items())
            return SimpleNamespace(
                status_code=response.getcode(),
                text=text,
                headers=headers,
                json=lambda: json.loads(text),
            )

    def _parse_response(self, response) -> Any:
        """统一解析 Freemail API 响应。"""
        if response.status_code >= 400:
            error_msg = f"请求失败: {response.status_code}"
            try:
                error_data = response.json()
                error_msg = f"{error_msg} - {error_data}"
            except Exception:
                error_msg = f"{error_msg} - {response.text[:200]}"
            self.update_status(False, EmailServiceError(error_msg))
            raise EmailServiceError(error_msg)

        try:
            return response.json()
        except Exception:
            raw_text = response.text or ""
            lowered = raw_text.lower()
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if (
                "cloudflare access" in lowered
                or "sign in ・ cloudflare access" in raw_text
                or "<title>sign in" in lowered and "cloudflare access" in lowered
                or "text/html" in content_type and "<html" in lowered
            ):
                raise EmailServiceError(
                    "Freemail API 被 Cloudflare Access 保护，当前返回的是 Access 登录页而不是 JSON。"
                    "请在 Cloudflare Zero Trust 中为该 Worker 域名关闭 Access，"
                    "或至少对 /api/* 放行 / 创建 Bypass 规则后再重试。"
                )
            return {"raw_response": raw_text}

    def _make_request(self, method: str, path: str, **kwargs) -> Any:
        """
        发送请求并返回 JSON 数据

        Args:
            method: HTTP 方法
            path: 请求路径（以 / 开头）
            **kwargs: 传递给 http_client.request 的额外参数

        Returns:
            响应 JSON 数据

        Raises:
            EmailServiceError: 请求失败
        """
        url = f"{self.config['base_url']}{path}"
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._get_headers())

        try:
            try:
                response = self.http_client.request(method, url, **kwargs)
            except Exception as primary_error:
                logger.warning(
                    "Freemail curl_cffi request failed for %s %s, fallback to urllib: %s",
                    method,
                    path,
                    primary_error,
                )
                response = self._request_via_stdlib(method, url, **kwargs)

            return self._parse_response(response)

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _ensure_domains(self):
        """获取并缓存可用域名列表"""
        if not self._domains:
            try:
                domains = self._make_request("GET", "/api/domains")
                if isinstance(domains, list):
                    self._domains = domains
            except Exception as e:
                logger.warning(f"获取 Freemail 域名列表失败: {e}")

    @staticmethod
    def _normalize_domain_text(value: Any) -> str:
        return str(value or "").strip().lower().lstrip("@")

    def _find_domain_index(self, domain: str) -> Optional[int]:
        target = self._normalize_domain_text(domain)
        if not target:
            return None
        for idx, item in enumerate(self._domains):
            if self._normalize_domain_text(item) == target:
                return idx
        return None

    def _build_custom_third_level_domain(self, domain_name: str, base_domain: str) -> str:
        raw_domain = self._normalize_domain_text(domain_name)
        normalized_base = self._normalize_domain_text(base_domain)

        if not raw_domain:
            return ""
        if raw_domain.count(".") >= 2:
            return raw_domain
        if raw_domain.count(".") == 1 and not normalized_base:
            return raw_domain
        if normalized_base and raw_domain.endswith(f".{normalized_base}"):
            return raw_domain
        return f"{raw_domain}.{normalized_base}" if normalized_base else raw_domain

    def _generate_random_third_level_label(self, min_length: int = 3, max_length: int = 8) -> str:
        candidates = [
            w for w in self.RANDOM_WORD_POOL
            if min_length <= len(w) <= max_length and self.THIRD_LEVEL_LABEL_RE.fullmatch(w)
        ]
        if candidates:
            return random.choice(candidates)

        fallback_length = random.randint(min_length, max_length)
        return "".join(random.choice(string.ascii_lowercase) for _ in range(fallback_length))

    def _resolve_domain_request(self, req_config: Dict[str, Any]) -> Dict[str, Any]:
        merged = {**self.config, **(req_config or {})}

        mode = self._normalize_domain_text(merged.get("domain_mode"))
        domain_name = self._normalize_domain_text(merged.get("domain_name"))
        explicit_domain = self._normalize_domain_text(merged.get("domain"))
        base_domain = self._normalize_domain_text(merged.get("base_domain"))

        if base_domain and base_domain.count(".") >= 2:
            base_parts = base_domain.split(".")
            base_domain = ".".join(base_parts[-2:])

        if not base_domain and explicit_domain:
            parts = explicit_domain.split(".")
            if len(parts) >= 2:
                base_domain = ".".join(parts[-2:])

        domain_request = {
            "domain_index": 0,
            "domain": None,
        }

        if mode == self.DOMAIN_MODE_THIRD_RANDOM:
            if not base_domain and self._domains:
                first_domain = self._normalize_domain_text(self._domains[0])
                first_parts = first_domain.split(".")
                if len(first_parts) >= 2:
                    base_domain = ".".join(first_parts[-2:])
            if not base_domain:
                raise EmailServiceError("Freemail missing base_domain for random third-level domain generation")

            random_label = self._generate_random_third_level_label()
            domain_request["domain"] = f"{random_label}.{base_domain}"
            return domain_request

        if mode == self.DOMAIN_MODE_SECOND_LEVEL:
            second_level_domain = domain_name or explicit_domain
            if not second_level_domain:
                raise EmailServiceError("Freemail second_level mode requires domain input")
            domain_idx = self._find_domain_index(second_level_domain)
            if domain_idx is None:
                raise EmailServiceError(
                    f"Freemail second-level domain `{second_level_domain}` is not in Worker MAIL_DOMAIN list"
                )
            domain_request["domain_index"] = domain_idx
            return domain_request

        if mode == self.DOMAIN_MODE_THIRD_LEVEL:
            third_level_domain = self._build_custom_third_level_domain(domain_name, base_domain)
            if not third_level_domain:
                raise EmailServiceError("Freemail third_level mode requires domain input")
            domain_idx = self._find_domain_index(third_level_domain)
            if domain_idx is not None:
                domain_request["domain_index"] = domain_idx
            else:
                domain_request["domain"] = third_level_domain
            return domain_request

        # Backward compatible behavior:
        # - If domain exists in /api/domains, keep using domainIndex.
        # - If not listed but looks like third-level domain, pass domain directly.
        if explicit_domain:
            domain_idx = self._find_domain_index(explicit_domain)
            if domain_idx is not None:
                domain_request["domain_index"] = domain_idx
            elif explicit_domain.count(".") >= 2:
                domain_request["domain"] = explicit_domain

        return domain_request

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        通过 API 创建临时邮箱

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - service_id: 同 email（用作标识）
        """
        self._ensure_domains()

        req_config = {**self.config, **(config or {})}
        domain_request = self._resolve_domain_request(req_config)
        domain_index = int(domain_request.get("domain_index", 0) or 0)
        target_domain = domain_request.get("domain")

        prefix = req_config.get("name")
        try:
            if prefix:
                body = {
                    "local": prefix,
                    "domainIndex": domain_index
                }
                if target_domain:
                    body["domain"] = target_domain
                resp = self._make_request("POST", "/api/create", json=body)
            else:
                params = {"domainIndex": domain_index}
                if target_domain:
                    params["domain"] = target_domain
                length = req_config.get("length")
                if length:
                    params["length"] = length
                resp = self._make_request("GET", "/api/generate", params=params)

            email = resp.get("email")
            if not email:
                raise EmailServiceError(f"创建邮箱失败，未返回邮箱地址: {resp}")

            email_info = {
                "email": email,
                "service_id": email,
                "id": email,
                "created_at": time.time(),
            }

            logger.info(f"成功创建 Freemail 邮箱: {email}")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    @staticmethod
    def _normalize_candidate_code(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        digits = re.sub(r"\D+", "", raw)
        if 4 <= len(digits) <= 8:
            return digits
        return ""

    def _extract_code_from_text(self, text: str, pattern: str) -> str:
        blob = str(text or "")
        if not blob:
            return ""

        try:
            match = re.search(pattern, blob)
            if match:
                candidate = match.group(1) if match.lastindex else match.group(0)
                normalized = self._normalize_candidate_code(candidate)
                if normalized:
                    return normalized
        except Exception:
            pass

        # Fallback: support codes split by spaces/hyphens like "1 2 3 4 5 6".
        for match in re.finditer(r"(?<!\d)(\d(?:[\s\-.,:]\d){3,11}|\d{4,8})(?!\d)", blob):
            normalized = self._normalize_candidate_code(match.group(1))
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _to_timestamp(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
            return ts / 1000.0 if ts > 1_000_000_000_000 else ts

        raw = str(value).strip()
        if not raw:
            return None

        try:
            ts = float(raw)
            return ts / 1000.0 if ts > 1_000_000_000_000 else ts
        except Exception:
            pass

        iso_candidate = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                continue

        return None

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 Freemail 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用，保留接口兼容
            timeout: 超时时间（秒）
            pattern: 验证码正则
            otp_sent_at: OTP 发送时间戳（暂未使用）

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info(f"正在从 Freemail 邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_mail_ids: set = set()
        otp_cutoff = float(otp_sent_at) - 10 if otp_sent_at else None

        while time.time() - start_time < timeout:
            try:
                mails = self._make_request("GET", "/api/emails", params={"mailbox": email, "limit": 20})
                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                openai_related = []
                others = []
                for mail in mails:
                    sender = str(mail.get("sender", "")).lower()
                    subject = str(mail.get("subject", "")).lower()
                    preview = str(mail.get("preview", "")).lower()
                    if "openai" in f"{sender} {subject} {preview}" or "chatgpt" in f"{sender} {subject} {preview}":
                        openai_related.append(mail)
                    else:
                        others.append(mail)

                for mail in openai_related + others:
                    mail_id = mail.get("id")
                    if not mail_id or mail_id in seen_mail_ids:
                        continue

                    mail_ts = self._to_timestamp(mail.get("received_at"))
                    if otp_cutoff and mail_ts and mail_ts < otp_cutoff:
                        continue

                    seen_mail_ids.add(mail_id)

                    sender = str(mail.get("sender", ""))
                    subject = str(mail.get("subject", ""))
                    preview = str(mail.get("preview", ""))
                    summary_blob = f"{sender}\n{subject}\n{preview}"
                    summary_lower = summary_blob.lower()
                    is_openai_mail = "openai" in summary_lower or "chatgpt" in summary_lower
                    detail = None
                    detail_blob = ""

                    # Keep hard filtering for OpenAI mails only. If summary does not
                    # contain "openai", fetch detail once and check again.
                    if not is_openai_mail:
                        try:
                            detail = self._make_request("GET", f"/api/email/{mail_id}")
                            detail_blob = "\n".join([
                                str(detail.get("sender", "")),
                                str(detail.get("subject", "")),
                                str(detail.get("preview", "")),
                                str(detail.get("content", "")),
                                str(detail.get("html_content", "")),
                            ])
                            detail_lower = detail_blob.lower()
                            is_openai_mail = "openai" in detail_lower or "chatgpt" in detail_lower
                        except Exception as e:
                            logger.debug(f"获取 Freemail 邮件详情失败: {e}")
                            continue

                    if not is_openai_mail:
                        continue

                    v_code = self._normalize_candidate_code(mail.get("verification_code"))
                    if v_code:
                        logger.info(f"从 Freemail 邮箱 {email} 找到验证码: {v_code}")
                        self.update_status(True)
                        return v_code

                    summary_code = self._extract_code_from_text(summary_blob, pattern)
                    if summary_code:
                        logger.info(f"从 Freemail 邮箱 {email} 找到验证码: {summary_code}")
                        self.update_status(True)
                        return summary_code

                    try:
                        if detail is None:
                            detail = self._make_request("GET", f"/api/email/{mail_id}")
                            detail_blob = "\n".join([
                                str(detail.get("sender", "")),
                                str(detail.get("subject", "")),
                                str(detail.get("preview", "")),
                                str(detail.get("content", "")),
                                str(detail.get("html_content", "")),
                            ])
                        detail_code = self._normalize_candidate_code(detail.get("verification_code"))
                        if not detail_code:
                            detail_code = self._extract_code_from_text(detail_blob, pattern)
                        if detail_code:
                            logger.info(f"从 Freemail 邮箱 {email} 找到验证码: {detail_code}")
                            self.update_status(True)
                            return detail_code
                    except Exception as e:
                        logger.debug(f"获取 Freemail 邮件详情失败: {e}")

            except Exception as e:
                logger.debug(f"检查 Freemail 邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待 Freemail 验证码超时: {email}")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出邮箱

        Args:
            **kwargs: 额外查询参数

        Returns:
            邮箱列表
        """
        try:
            params = {
                "limit": kwargs.get("limit", 100),
                "offset": kwargs.get("offset", 0)
            }
            resp = self._make_request("GET", "/api/mailboxes", params=params)
            
            emails = []
            if isinstance(resp, list):
                for mail in resp:
                    address = mail.get("address")
                    if address:
                        emails.append({
                            "id": address,
                            "service_id": address,
                            "email": address,
                            "created_at": mail.get("created_at"),
                            "raw_data": mail
                        })
            self.update_status(True)
            return emails
        except Exception as e:
            logger.warning(f"列出 Freemail 邮箱失败: {e}")
            self.update_status(False, e)
            return []

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱
        """
        try:
            self._make_request("DELETE", "/api/mailboxes", params={"address": email_id})
            logger.info(f"已删除 Freemail 邮箱: {email_id}")
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"删除 Freemail 邮箱失败: {e}")
            self.update_status(False, e)
            return False

    def check_health(self) -> bool:
        """检查服务健康状态"""
        try:
            self._make_request("GET", "/api/domains")
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"Freemail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
