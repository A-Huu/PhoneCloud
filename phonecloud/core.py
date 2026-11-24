"""业务核心：账号、登录会话、空间与授权。HTTP 层只做参数收发，规则都在这里。"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import fsutil, security
from .config import Settings
from .db import Database

log = logging.getLogger("phonecloud")

_SESSION_TOUCH_INTERVAL = 3600

# 账号不存在时也做一次等价的哈希计算，避免通过响应时间探测账号是否存在。
_DUMMY_HASH: str | None = None

VISIBLE_TO_ALL = "all"  # 所有登录账号可见，名单里的账号除外
VISIBLE_TO_LISTED = "only"  # 只有名单里的账号可见

# 空间 s 对账号 ?1 是否可见。列目录、预览、下载等所有访问都经过这一个条件：
# 所有者始终可见；其余账号按空间的可见范围和名单（space_grants）判断。
_ON_LIST = "EXISTS (SELECT 1 FROM space_grants g WHERE g.space_id = s.id AND g.user_id = ?1)"
_VISIBLE_TO_USER = (
    "(s.owner_user_id = ?1"
    f" OR (s.visibility = '{VISIBLE_TO_ALL}' AND NOT {_ON_LIST})"
    f" OR (s.visibility = '{VISIBLE_TO_LISTED}' AND {_ON_LIST}))"
)


class AppError(Exception):
    """可以直接展示给用户的业务错误。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class User:
    id: int
    username: str
    is_admin: bool
    disabled: bool


@dataclass(frozen=True)
class Space:
    id: int
    name: str
    kind: str
    root: Path
    owner_user_id: int | None
    # 随账号创建的个人空间。用户另建的个人文件夹不是它，文件直接放在文件夹根目录。
    account_space: bool = False

    def writable_by(self, user: User) -> bool:
        """创建的相册：能看到就能上传。个人空间和个人文件夹仍然只有所有者能上传。"""
        if self.kind == "shared":
            return True
        return self.kind == "personal" and self.owner_user_id == user.id

    def display_name(self, user: User) -> str:
        if self.account_space:
            return "我的空间" if self.owner_user_id == user.id else f"{self.name} 的空间"
        return self.name


def _user_from_row(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        disabled=bool(row["disabled"]),
    )


class Core:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.throttle = security.LoginThrottle()
        # 缩略图生成吃 CPU 和内存，限制并发，避免占满线程池拖慢其它请求。
        self.thumb_slots = threading.BoundedSemaphore(3)

    # ---- 初始化 -------------------------------------------------------

    def init(self) -> None:
        for directory in (
            self.settings.accounts_dir,
            self.settings.data_dir,
            self.settings.thumbs_dir,
            self.settings.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.db.init()
        with self.db.session() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))

    def cleanup_stale_uploads(self) -> int:
        """删除上次异常退出时留下的上传临时文件。只应在确认本实例独占主目录后调用。"""
        roots: list[Path] = []
        with self.db.session() as conn:
            for row in conn.execute("SELECT * FROM spaces"):
                roots.append(self._space_from_row(row).root)
        chosen: list[Path] = []
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                real = root.resolve()
            except OSError:
                continue
            if any(real == old or fsutil.is_within(real, old) for old in chosen):
                continue
            chosen = [old for old in chosen if not fsutil.is_within(old, real)]
            chosen.append(real)
        removed = 0
        for root in chosen:
            for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
                for name in filenames:
                    if not fsutil.is_upload_temp(name):
                        continue
                    try:
                        (Path(dirpath) / name).unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

    # ---- 账号 ---------------------------------------------------------

    def has_admin(self) -> bool:
        with self.db.session() as conn:
            row = conn.execute("SELECT 1 FROM users WHERE is_admin = 1 AND disabled = 0 LIMIT 1").fetchone()
        return row is not None

    def create_user(self, username: str, password: str, *, is_admin: bool = False) -> User:
        username = username.strip()
        error = security.validate_username(username) or security.validate_password(password)
        if error:
            raise AppError(400, error)
        now = int(time.time())
        password_hash = security.hash_password(password)
        key = security.username_key(username)
        try:
            with self.db.session() as conn:
                # 数据库的唯一约束只对英文字母不区分大小写；账号名允许其它文字后，
                # 这里再按文件夹名的规则查一遍，保证两个账号不会共用同一个目录。
                if any(security.username_key(r["username"]) == key for r in conn.execute("SELECT username FROM users")):
                    raise AppError(409, "账号名已存在")
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                    (username, password_hash, int(is_admin), now),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            raise AppError(409, "账号名已存在") from None
        # 不预建个人文件夹。文件夹由用户之后另建。
        log.info("创建账号 %s（管理员：%s）", username, is_admin)
        return User(id=user_id, username=username, is_admin=is_admin, disabled=False)

    def list_users(self) -> list[dict[str, object]]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT id, username, is_admin, disabled, created_at FROM users ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "is_admin": bool(r["is_admin"]),
                "disabled": bool(r["disabled"]),
                "created_at": r["created_at"],
                "has_files": self._account_has_files(r["username"]),
            }
            for r in rows
        ]

    def get_user(self, user_id: int) -> User:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AppError(404, "账号不存在")
        return _user_from_row(row)

    def set_disabled(self, user_id: int, disabled: bool, *, acting: User) -> None:
        target = self.get_user(user_id)
        if disabled and target.id == acting.id:
            raise AppError(400, "不能禁用当前登录的管理员账号")
        with self.db.session() as conn:
            conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))
            if disabled:
                # 禁用后立即踢下线，而不是等令牌过期
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        log.info("%s 账号 %s（操作人 %s）", "禁用" if disabled else "启用", target.username, acting.username)

    def set_password(self, user_id: int, password: str, *, keep_token: str | None = None) -> None:
        target = self.get_user(user_id)
        error = security.validate_password(password)
        if error:
            raise AppError(400, error)
        password_hash = security.hash_password(password)
        keep_hash = security.hash_token(keep_token) if keep_token else ""
        with self.db.session() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
            # 改密码后旧登录态全部失效
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep_hash))
        log.info("重置账号 %s 的密码", target.username)

    def delete_user(self, user_id: int, *, acting: User) -> None:
        """删除账号及其空的账号文件夹。文件夹里还有文件时拒绝，避免把照片一起清掉。"""
        target = self.get_user(user_id)
        if target.id == acting.id:
            raise AppError(400, "不能删除当前登录的账号")
        if self._account_has_files(target.username):
            raise AppError(400, "这个账号的文件夹里还有文件，清空后才能删除")
        with self.db.session() as conn:
            space_ids = [
                row["id"]
                for row in conn.execute("SELECT id FROM spaces WHERE owner_user_id = ?", (user_id,))
            ]
            thumbs: list[str] = []
            if space_ids:
                marks = ",".join("?" * len(space_ids))
                thumbs = [
                    row["thumb_file"]
                    for row in conn.execute(
                        f"SELECT thumb_file FROM thumb_cache WHERE space_id IN ({marks})", space_ids
                    )
                ]
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        for name in thumbs:
            try:
                (self.settings.thumbs_dir / name).unlink()
            except OSError:
                pass
        self._remove_empty_account_dir(target.username)
        log.info("删除账号 %s（操作人 %s）", target.username, acting.username)

    def _account_has_files(self, username: str) -> bool:
        """账号目录下是否有文件。空文件夹不算。"""
        root = self.settings.accounts_dir / username
        try:
            if not root.is_dir():
                return False
        except OSError:
            return True
        for _dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            if filenames:
                return True
        return False

    def _remove_empty_account_dir(self, username: str) -> None:
        """删掉账号目录里的空文件夹。遇到文件就停，不删除任何文件。"""
        root = self.settings.accounts_dir / username
        try:
            if not root.is_dir():
                return
            accounts = self.settings.accounts_dir.resolve()
            real = root.resolve()
        except OSError:
            log.warning("删除账号 %s 后未能清理文件夹", username)
            return
        if not fsutil.is_within(real, accounts):
            return
        for dirpath, dirnames, filenames in os.walk(real, topdown=False, followlinks=False):
            if filenames:
                log.warning("账号 %s 的文件夹在删除过程中出现了文件，已停止清理", username)
                return
            current = Path(dirpath)
            for name in dirnames:
                child = current / name
                if not child.is_symlink():
                    continue
                try:
                    child.unlink()
                except OSError:
                    log.warning("未能移除 %s", child)
                    return
            try:
                current.rmdir()
            except OSError:
                log.warning("未能移除空文件夹 %s", current)
                return

    def upsert_admin(self, username: str, password: str) -> User:
        """命令行重置管理员：账号存在则改密码并设为启用的管理员，不存在则创建。"""
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        if row is None:
            return self.create_user(username, password, is_admin=True)
        self.set_password(row["id"], password)
        with self.db.session() as conn:
            conn.execute("UPDATE users SET is_admin = 1, disabled = 0 WHERE id = ?", (row["id"],))
        return self.get_user(row["id"])

    # ---- 登录会话 -----------------------------------------------------

    def login(self, username: str, password: str) -> tuple[str, int, User] | None:
        global _DUMMY_HASH
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        if row is None:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = security.hash_password("phonecloud-dummy")
            security.verify_password(password, _DUMMY_HASH)
            return None
        if not security.verify_password(password, row["password_hash"]):
            return None
        user = _user_from_row(row)
        if user.disabled:
            raise AppError(403, "账号已被禁用，请联系管理员")
        token = security.new_token()
        now = int(time.time())
        expires_at = now + self.settings.session_seconds
        with self.db.session() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (user.id, security.hash_token(token), now, now, expires_at),
            )
        return token, expires_at, user

    def user_for_token(self, token: str) -> User | None:
        now = int(time.time())
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT s.id AS session_id, s.last_seen_at, s.expires_at,"
                " u.id, u.username, u.is_admin, u.disabled"
                " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                (security.hash_token(token),),
            ).fetchone()
            if row is None or row["expires_at"] <= now or row["disabled"]:
                return None
            if now - row["last_seen_at"] > _SESSION_TOUCH_INTERVAL:
                # 持续使用就自动续期；间隔更新，避免每个请求都写库
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                    (now, now + self.settings.session_seconds, row["session_id"]),
                )
        return _user_from_row(row)

    def logout(self, token: str) -> None:
        with self.db.session() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (security.hash_token(token),))

    # ---- 空间与授权 ---------------------------------------------------

    def _space_from_row(self, row: sqlite3.Row) -> Space:
        # 账号自带的个人空间没有 path，根目录是 accounts/<账号名>。
        # 用户另建的个人文件夹和共享相册都把真实路径记在 path 里。
        account_space = row["kind"] == "personal" and not (row["path"] or "").strip()
        if account_space:
            root = self.settings.accounts_dir / row["name"]
        else:
            root = Path(row["path"])
        return Space(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            root=root,
            owner_user_id=row["owner_user_id"],
            account_space=account_space,
        )

    def spaces_for(self, user: User) -> list[Space]:
        """账号可见的空间：自己的个人空间 + 对它可见的其它空间（规则见 _VISIBLE_TO_USER）。"""
        with self.db.session() as conn:
            rows = conn.execute(
                f"SELECT s.* FROM spaces s WHERE {_VISIBLE_TO_USER}"
                " ORDER BY (s.owner_user_id IS ?1) DESC, s.kind, s.name",
                (user.id,),
            ).fetchall()
        return [self._space_from_row(r) for r in rows]

    def space_for(self, user: User, space_id: int) -> Space:
        """取账号有权访问的空间。无权与不存在返回同样的 404，不泄露空间是否存在。"""
        with self.db.session() as conn:
            row = conn.execute(
                f"SELECT s.* FROM spaces s WHERE s.id = ?2 AND {_VISIBLE_TO_USER}",
                (user.id, space_id),
            ).fetchone()
        if row is None:
            raise AppError(404, "空间不存在或没有访问权限")
        return self._space_from_row(row)

    def personal_space(self, user: User) -> Space:
        """早期账号自带的个人空间（没有 path）。新账号没有这一项，上传时要指定文件夹。"""
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM spaces WHERE kind = 'personal' AND owner_user_id = ? AND COALESCE(path, '') = ''",
                (user.id,),
            ).fetchone()
        if row is None:
            raise AppError(404, "请先选择要保存的文件夹")
        return self._space_from_row(row)

    def resolve(self, space: Space, rel: str) -> tuple[Path, str]:
        """把空间内相对路径解析成磁盘真实路径；越界或指向程序数据目录一律拒绝。"""
        try:
            real, normalized = fsutil.resolve_in(space.root, rel)
        except fsutil.PathError as exc:
            raise AppError(400, str(exc)) from None
        except OSError:
            raise AppError(404, "路径不存在") from None
        if self.is_protected(real):
            raise AppError(404, "路径不存在")
        return real, normalized

    def is_protected(self, real: Path) -> bool:
        """data 目录存着数据库（含密码哈希），即使被某个空间包含也不允许经由接口读取。"""
        return fsutil.is_within(real, self.settings.data_dir.resolve())

    # ---- 管理端：空间 -------------------------------------------------

    def _create_space_folder(self, raw: Path) -> Path:
        """空间文件夹不存在时的处理：在主目录里（默认位置）就自动新建；其它位置多半是路径写错了，照旧报错。"""
        home = self.settings.home.resolve()
        target = raw.resolve()
        if target == home or not fsutil.is_within(target, home):
            raise AppError(400, "该路径在电脑上不存在")
        if self.is_protected(target):
            raise AppError(400, "不能把 PhoneCloud 的 data 目录作为空间")
        try:
            # 默认路径由空间名称拼出来，名称里可能有文件夹名不允许的写法（? * : 保留名称等）
            fsutil.split_rel(str(target.relative_to(home)))
            target.mkdir(parents=True, exist_ok=True)
            return target.resolve(strict=True)
        except (fsutil.PathError, OSError):
            raise AppError(400, f"无法新建文件夹 {target}，请换一个空间名称，或填写电脑上已有的文件夹路径") from None

    def create_space(self, name: str, path: str = "") -> int:
        """创建共享空间，默认所有登录账号可见。

        path 留空时使用默认位置：主目录（phonecloud.exe 所在文件夹）下与空间同名的文件夹。
        """
        name = name.strip()
        if not 1 <= len(name) <= 50:
            raise AppError(400, "空间名称需为 1～50 个字符")
        text = path.strip().strip('"').strip()
        if text:
            raw = Path(text)
        else:
            try:
                raw = self.settings.home.joinpath(*fsutil.split_rel(name))
            except fsutil.PathError:
                raise AppError(400, "这个空间名称不能直接用作文件夹名，请换一个名称，或手动填写文件夹路径") from None
        if not raw.is_absolute():
            raise AppError(400, "请填写电脑上的完整路径，例如 D:\\Photos")
        try:
            real = raw.resolve(strict=True)
        except OSError:
            real = self._create_space_folder(raw)
        if not real.is_dir():
            raise AppError(400, "该路径不是文件夹")
        if self.is_protected(real):
            raise AppError(400, "不能把 PhoneCloud 的 data 目录作为空间")
        try:
            with self.db.session() as conn:
                cur = conn.execute(
                    "INSERT INTO spaces (name, kind, path, created_at, visibility) VALUES (?, 'shared', ?, ?, ?)",
                    (name, str(real), int(time.time()), VISIBLE_TO_ALL),
                )
        except sqlite3.IntegrityError:
            raise AppError(409, "已存在同名空间") from None
        log.info("创建空间 %s -> %s", name, real)
        return cur.lastrowid

    def create_folder(self, user: User, name: str, kind: str) -> int:
        """上传时新建文件夹。共享：主目录下，能看就能上传。个人：建在自己的账号目录里，只有自己能上传。"""
        if kind == "shared":
            return self.create_space(name)
        if kind != "personal":
            raise AppError(400, "文件夹只能是个人或共享")
        name = name.strip()
        if not 1 <= len(name) <= 50:
            raise AppError(400, "空间名称需为 1～50 个字符")
        try:
            parts = fsutil.split_rel(name)
        except fsutil.PathError:
            raise AppError(400, "这个空间名称不能直接用作文件夹名，请换一个名称") from None
        if len(parts) != 1:
            raise AppError(400, "这个空间名称不能直接用作文件夹名，请换一个名称")
        raw = self.settings.accounts_dir / user.username / parts[0]
        try:
            real = raw.resolve(strict=True)
        except OSError:
            real = self._create_space_folder(raw)
        account_root = (self.settings.accounts_dir / user.username).resolve()
        if real == account_root or not fsutil.is_within(real, account_root):
            raise AppError(400, "这个空间名称不能直接用作文件夹名，请换一个名称")
        if not real.is_dir():
            raise AppError(400, "该路径不是文件夹")
        try:
            with self.db.session() as conn:
                cur = conn.execute(
                    "INSERT INTO spaces (name, kind, path, owner_user_id, created_at, visibility)"
                    " VALUES (?, 'personal', ?, ?, ?, ?)",
                    (name, str(real), user.id, int(time.time()), VISIBLE_TO_ALL),
                )
        except sqlite3.IntegrityError:
            raise AppError(409, "已存在同名的个人文件夹") from None
        log.info("创建个人文件夹 %s -> %s", name, real)
        return cur.lastrowid

    def delete_space(self, space_id: int) -> None:
        """删除空间记录及其授权、缩略图缓存；不会动电脑上的原始文件。"""
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if row is None:
                raise AppError(404, "空间不存在")
            if row["kind"] == "personal" and not (row["path"] or "").strip():
                raise AppError(400, "个人空间不能删除")
            thumbs = [r["thumb_file"] for r in conn.execute(
                "SELECT thumb_file FROM thumb_cache WHERE space_id = ?", (space_id,)
            )]
            conn.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
        for name in thumbs:
            try:
                (self.settings.thumbs_dir / name).unlink()
            except OSError:
                pass
        log.info("删除空间 %s", row["name"])

    def list_spaces_admin(self) -> list[dict[str, object]]:
        with self.db.session() as conn:
            spaces = conn.execute(
                "SELECT s.*, u.username AS owner FROM spaces s LEFT JOIN users u ON u.id = s.owner_user_id"
                " ORDER BY s.kind DESC, s.name"
            ).fetchall()
            grants = conn.execute(
                "SELECT g.id, g.space_id, g.user_id, u.username FROM space_grants g"
                " JOIN users u ON u.id = g.user_id ORDER BY u.username"
            ).fetchall()
        result = []
        for row in spaces:
            space = self._space_from_row(row)
            root = space.root
            result.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "path": str(root),
                    "owner": row["owner"],
                    "owner_user_id": row["owner_user_id"],
                    "exists": root.is_dir(),
                    "visibility": row["visibility"],
                    "grants": [
                        {"id": g["id"], "user_id": g["user_id"], "username": g["username"]}
                        for g in grants
                        if g["space_id"] == row["id"]
                    ],
                }
            )
        return result

    def set_visibility(self, space_id: int, visibility: str) -> None:
        """切换空间（共享空间或个人空间）的可见范围。

        名单的含义随之相反，所以切换时清空名单，避免"不可见的人"变成"可见的人"。
        个人空间的所有者不受可见范围影响，始终能看到并上传。
        """
        if visibility not in (VISIBLE_TO_ALL, VISIBLE_TO_LISTED):
            raise AppError(400, "可见范围不正确")
        with self.db.session() as conn:
            space = conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if space is None:
                raise AppError(404, "空间不存在")
            if space["visibility"] == visibility:
                return
            conn.execute("UPDATE spaces SET visibility = ? WHERE id = ?", (visibility, space_id))
            conn.execute("DELETE FROM space_grants WHERE space_id = ?", (space_id,))
        log.info("空间 %s 的可见范围改为 %s", space["name"], visibility)

    def grant(self, space_id: int, user_id: int, *, expected_visibility: str | None = None) -> int:
        """把账号加入空间的名单。名单含义取决于空间的可见范围：
        指定账号可见时是"可见的账号"，所有账号可见时是"不可见的账号"。

        expected_visibility：调用方以为的可见范围。和实际不一致时拒绝，
        防止在过期的页面上把"设为不可见"误做成"设为可见"。
        """
        self.get_user(user_id)
        with self.db.session() as conn:
            space = conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if space is None:
                raise AppError(404, "空间不存在")
            if expected_visibility is not None and expected_visibility != space["visibility"]:
                raise AppError(409, "这个空间的可见范围已经变了，请刷新页面后重试")
            if space["owner_user_id"] == user_id:
                raise AppError(400, "该账号是空间的所有者，始终可见")
            try:
                cur = conn.execute(
                    "INSERT INTO space_grants (space_id, user_id, created_at) VALUES (?, ?, ?)",
                    (space_id, user_id, int(time.time())),
                )
            except sqlite3.IntegrityError:
                raise AppError(409, "该账号已经在这个空间的名单里") from None
        return cur.lastrowid

    def revoke(self, grant_id: int) -> None:
        """把账号从空间的名单里移除。"""
        with self.db.session() as conn:
            cur = conn.execute("DELETE FROM space_grants WHERE id = ?", (grant_id,))
        if cur.rowcount == 0:
            raise AppError(404, "名单里没有这一项")

    # ---- 管理端：磁盘概览 ---------------------------------------------

    def disk_overview(self) -> list[dict[str, object]]:
        """按磁盘汇总容量，并标出每块盘上有哪些空间。"""
        disks: dict[str, list[str]] = {}

        def add(path: Path, label: str) -> None:
            try:
                anchor = path.resolve().anchor or str(path)
            except OSError:
                anchor = path.anchor or str(path)
            disks.setdefault(anchor, []).append(label)

        add(self.settings.home, "PhoneCloud 主目录（账号空间）")
        with self.db.session() as conn:
            for row in conn.execute("SELECT name, path FROM spaces WHERE kind = 'shared' ORDER BY name"):
                add(Path(row["path"]), f"空间：{row['name']}")

        result = []
        for anchor, labels in disks.items():
            item: dict[str, object] = {"disk": anchor, "uses": labels}
            try:
                usage = shutil.disk_usage(anchor)
                item.update(total=usage.total, used=usage.used, free=usage.free)
            except OSError:
                item["error"] = "无法访问该磁盘"
            result.append(item)
        return result
