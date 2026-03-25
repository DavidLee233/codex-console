"""
Email parsing and OTP extraction helpers for Outlook service.
"""

import logging
import re
from typing import Optional, List, Pattern

from ...config.constants import (
    OTP_CODE_SIMPLE_PATTERN,
    OTP_CODE_SEMANTIC_PATTERN,
    OPENAI_EMAIL_SENDERS,
    OPENAI_VERIFICATION_KEYWORDS,
)
from .base import EmailMessage


logger = logging.getLogger(__name__)


class EmailParser:
    """
    Parse verification emails and extract six-digit codes.
    """

    def __init__(self):
        self._simple_pattern = re.compile(OTP_CODE_SIMPLE_PATTERN)
        self._semantic_pattern = re.compile(OTP_CODE_SEMANTIC_PATTERN, re.IGNORECASE)

    def is_openai_verification_email(
        self,
        email: EmailMessage,
        target_email: Optional[str] = None,
    ) -> bool:
        """
        Check whether this email is likely an OpenAI verification email.
        """
        sender = (email.sender or "").lower()
        if not any(s in sender for s in OPENAI_EMAIL_SENDERS):
            logger.debug(f"Non-OpenAI sender skipped: {sender}")
            return False

        subject = (email.subject or "").lower()
        body = (email.body or "").lower()
        body_preview = (email.body_preview or "").lower()
        combined = f"{subject} {body} {body_preview}"
        if not any(kw in combined for kw in OPENAI_VERIFICATION_KEYWORDS):
            logger.debug(f"No OpenAI keyword in subject/body: {subject[:50]}")
            return False

        logger.debug(f"Detected OpenAI verification email: {subject[:50]}")
        return True

    def extract_verification_code(
        self,
        email: EmailMessage,
    ) -> Optional[str]:
        """
        Extract code with priority:
        1) subject
        2) semantic regex in body/preview
        3) simple six-digit regex in body/preview
        """
        code = self._extract_from_subject(email.subject)
        if code:
            logger.debug(f"Extracted OTP from subject: {code}")
            return code

        full_body = self._compose_search_text(email)
        code = self._extract_semantic(full_body)
        if code:
            logger.debug(f"Extracted OTP by semantic body regex: {code}")
            return code

        code = self._extract_simple(full_body)
        if code:
            logger.debug(f"Extracted OTP by simple body regex: {code}")
            return code

        return None

    @staticmethod
    def _compose_search_text(email: EmailMessage) -> str:
        return " ".join(
            part for part in [email.body or "", email.body_preview or ""] if part
        ).strip()

    @staticmethod
    def _extract_with_pattern(text: str, compiled_pattern: Pattern[str]) -> Optional[str]:
        if not text:
            return None
        match = compiled_pattern.search(text)
        if not match:
            return None
        if match.lastindex:
            return match.group(1)
        return match.group(0)

    def _extract_from_subject(self, subject: str) -> Optional[str]:
        match = self._simple_pattern.search(subject or "")
        if match:
            return match.group(1)
        return None

    def _extract_semantic(self, body: str) -> Optional[str]:
        match = self._semantic_pattern.search(body or "")
        if match:
            return match.group(1)
        return None

    def _extract_simple(self, body: str) -> Optional[str]:
        match = self._simple_pattern.search(body or "")
        if match:
            return match.group(1)
        return None

    def find_verification_code_in_emails(
        self,
        emails: List[EmailMessage],
        target_email: Optional[str] = None,
        min_timestamp: int = 0,
        used_codes: Optional[set] = None,
        pattern: Optional[str] = None,
        allow_any_sender: bool = False,
    ) -> Optional[str]:
        """
        Find OTP code from candidate emails.

        Args:
            emails: candidate emails (order does not matter).
            target_email: for logs only.
            min_timestamp: skip emails older than this unix timestamp.
            used_codes: dedupe within one workflow.
            pattern: optional custom regex; defaults to six-digit regex.
            allow_any_sender: when True, fallback to any sender after OpenAI pass.
        """
        used_codes = used_codes or set()
        code_pattern = self._simple_pattern
        if pattern:
            try:
                code_pattern = re.compile(pattern)
            except re.error:
                logger.warning(
                    f"Invalid OTP regex, fallback to default six-digit regex. pattern={pattern}"
                )
                code_pattern = self._simple_pattern

        sorted_emails = sorted(
            emails,
            key=lambda item: item.received_timestamp or 0,
            reverse=True,
        )

        for email in sorted_emails:
            if min_timestamp > 0 and email.received_timestamp > 0:
                if email.received_timestamp < min_timestamp:
                    continue

            if not self.is_openai_verification_email(email, target_email):
                continue

            code = self.extract_verification_code(email)
            if not code:
                code = self._extract_with_pattern(
                    f"{email.subject or ''} {self._compose_search_text(email)}",
                    code_pattern,
                )
            if not code:
                continue
            if code in used_codes:
                continue

            logger.info(
                f"[{target_email or 'unknown'}] Found OTP: {code}, subject: {(email.subject or '')[:30]}"
            )
            return code

        if not allow_any_sender:
            return None

        for email in sorted_emails:
            if min_timestamp > 0 and email.received_timestamp > 0:
                if email.received_timestamp < min_timestamp:
                    continue

            code = self._extract_with_pattern(
                f"{email.subject or ''} {self._compose_search_text(email)}",
                code_pattern,
            )
            if not code:
                continue
            if code in used_codes:
                continue

            logger.info(
                f"[{target_email or 'unknown'}] Fallback OTP: {code}, sender: {(email.sender or '')[:50]}, subject: {(email.subject or '')[:30]}"
            )
            return code

        return None

    def filter_emails_by_sender(
        self,
        emails: List[EmailMessage],
        sender_patterns: List[str],
    ) -> List[EmailMessage]:
        filtered = []
        for email in emails:
            sender = (email.sender or "").lower()
            if any(pattern.lower() in sender for pattern in sender_patterns):
                filtered.append(email)
        return filtered

    def filter_emails_by_subject(
        self,
        emails: List[EmailMessage],
        keywords: List[str],
    ) -> List[EmailMessage]:
        filtered = []
        for email in emails:
            subject = (email.subject or "").lower()
            if any(kw.lower() in subject for kw in keywords):
                filtered.append(email)
        return filtered


_parser: Optional[EmailParser] = None


def get_email_parser() -> EmailParser:
    """Get global parser singleton."""
    global _parser
    if _parser is None:
        _parser = EmailParser()
    return _parser
