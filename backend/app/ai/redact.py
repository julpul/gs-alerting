"""Redaction before any model call (spec 13.5): secrets and personal data become `[REDACTED:<type>]`."""

from __future__ import annotations

import re
from typing import Any

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.DOTALL,
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{3,}")
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")
_GITHUB = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}")
_SECRET_KV = re.compile(
    r"(?i)(?P<key>\b(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?token|"
    r"aws_secret_access_key|client_secret|authorization)\b[\"']?\s*[:=]\s*[\"']?)(?P<value>[^\s\"'&,;}]{3,})"
)
_SECRET_KEY = re.compile(
    r"(?i)^(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?token|client[_-]?secret|authorization)$"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.-]?(?:\(?\d{1,4}\)?[\s.-]?){2,5}\d|\d{3}[\s.-]\d{3}[\s.-]\d{3,4})(?![\w.])"
)


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def iban_valid(value: str) -> bool:
    compact = value.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1
    except ValueError:
        return False


def _tag(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _mask_kv(match: re.Match[str]) -> str:
    return f"{match.group('key')}{_tag('secret')}"


def _mask_card(match: re.Match[str]) -> str:
    return _tag("card") if luhn_valid(match.group(0)) else match.group(0)


def _mask_iban(match: re.Match[str]) -> str:
    return _tag("iban") if iban_valid(match.group(0)) else match.group(0)


def _mask_phone(match: re.Match[str]) -> str:
    digits = sum(c.isdigit() for c in match.group(0))
    return _tag("phone") if 9 <= digits <= 15 else match.group(0)


def redact(text: str) -> str:
    """Mask secrets and personal data in one string."""
    text = _PRIVATE_KEY.sub(_tag("private_key"), text)
    text = _JWT.sub(_tag("jwt"), text)
    text = _AWS_KEY.sub(_tag("aws_key"), text)
    text = _GITHUB.sub(_tag("github_token"), text)
    text = _BEARER.sub(lambda m: m.group(1) + _tag("token"), text)
    text = _SECRET_KV.sub(_mask_kv, text)
    text = _EMAIL.sub(_tag("email"), text)
    text = _IBAN.sub(_mask_iban, text)
    text = _CARD.sub(_mask_card, text)
    return _PHONE.sub(_mask_phone, text)


def redact_obj(value: Any) -> Any:
    """Redact every string inside nested dicts/lists (keys are kept); other types pass through."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: _tag("secret") if isinstance(v, str) and v and _SECRET_KEY.match(str(k)) else redact_obj(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_obj(v) for v in value]
    return value
