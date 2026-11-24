"""密码哈希、登录令牌与登录失败限速。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

PBKDF2_ITERATIONS = 600_000
MAX_USERNAME_LENGTH = 32
MAX_PASSWORD_LENGTH = 128

# 账号名同时也是 accounts 目录下的文件夹名。自家使用不限制长度和字符组合（1 位、纯数字、中文都可以），
# 只拦 Windows 文件夹名本身不允许的写法，否则账号目录建不出来，或者会和别的账号落到同一个目录。
_USERNAME_FORBIDDEN_CHARS = '\\/:*?"<>|'
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in "0123456789¹²³"}


def validate_username(username: str) -> str | None:
    """返回错误信息；合法时返回 None。"""
    if not username:
        return "请输入账号名"
    if len(username) > MAX_USERNAME_LENGTH:
        return f"账号名最长 {MAX_USERNAME_LENGTH} 个字符"
    if any(c in _USERNAME_FORBIDDEN_CHARS or not c.isprintable() for c in username):
        # isprintable 同时挡住了控制字符和零宽字符这类看不见的字符
        return '账号名不能包含 \\ / : * ? " < > | 这些符号'
    if username != username.strip(" ") or username.endswith("."):
        # Windows 会悄悄去掉文件夹名结尾的点和空格，"a." 与 "a" 是同一个文件夹
        return "账号名不能以空格开头或结尾，也不能以点结尾"
    if username.split(".")[0].rstrip(" ").lower() in _RESERVED_NAMES:
        return "该账号名是 Windows 保留名称，请换一个"
    return None


def username_key(username: str) -> str:
    """用于判断两个账号名是否会落到同一个文件夹（Windows 文件夹名不区分大小写）。"""
    return username.upper().casefold()


def validate_password(password: str) -> str | None:
    """自家使用不限制密码强度，1 位数字也可以，只要求不为空。"""
    if not password:
        return "请输入密码"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"密码最长 {MAX_PASSWORD_LENGTH} 位"
    return None


def hash_password(password: str) -> str:
    iterations = PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """数据库只保存令牌的哈希，库文件泄露也无法直接冒用登录态。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginThrottle:
    """同一来源对同一账号在时间窗内失败过多则暂时拒绝登录，减缓局域网内的密码猜测。"""

    _MAX_KEYS = 5000

    def __init__(self, max_failures: int = 10, window_seconds: int = 600) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            if len(self._failures) >= self._MAX_KEYS:
                # 有人用大量不同账号名刷失败记录时，先清掉过期项，防止内存无限增长
                for stale in list(self._failures):
                    self._recent(stale, now)
                if len(self._failures) >= self._MAX_KEYS:
                    self._failures.clear()
            self._recent(key, now)
            self._failures.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
