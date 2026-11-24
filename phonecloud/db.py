"""SQLite 元数据库：表结构与连接管理。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT    NOT NULL UNIQUE,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- kind = 'personal'：个人文件夹，path 为 accounts/<账号>/<名字>。
--   更早的库里 path 为空，根目录是 accounts/<账号名>。
-- kind = 'shared'  ：共享文件夹，能看到就能上传，path 为电脑上的绝对路径。
-- visibility 决定除所有者之外谁能看到这个空间，以及 space_grants 名单的含义：
--   'all'  ：所有登录账号可见，名单里的账号除外（指定人不可见）；
--   'only' ：只有名单里的账号可见（指定人可见）。
-- 列的默认值取更保守的 'only'：漏填时宁可看不到，也不能多给权限。
CREATE TABLE IF NOT EXISTS spaces (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL COLLATE NOCASE,
    kind          TEXT    NOT NULL CHECK (kind IN ('personal', 'shared')),
    path          TEXT,
    owner_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at    INTEGER NOT NULL,
    visibility    TEXT    NOT NULL DEFAULT 'only' CHECK (visibility IN ('all', 'only')),
    UNIQUE (kind, name)
);

-- 空间的账号名单，含义见 spaces.visibility。
CREATE TABLE IF NOT EXISTS space_grants (
    id         INTEGER PRIMARY KEY,
    space_id   INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission TEXT    NOT NULL DEFAULT 'ro' CHECK (permission IN ('ro')),
    created_at INTEGER NOT NULL,
    UNIQUE (space_id, user_id)
);

CREATE TABLE IF NOT EXISTS thumb_cache (
    id           INTEGER PRIMARY KEY,
    space_id     INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    rel_path     TEXT    NOT NULL,
    src_size     INTEGER NOT NULL,
    src_mtime_ns INTEGER NOT NULL,
    thumb_file   TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE (space_id, rel_path)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}，请使用更新的程序版本"
                )
            conn.executescript(SCHEMA)
            self._migrate(conn, version)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _migrate(conn: sqlite3.Connection, version: int) -> None:
        """把旧版本建的库升级到当前结构。各步骤和最后写入的版本号在同一个事务里，不会只做一半，也不会重复执行。

        升级规则：从未指定过账号的空间按新的默认值对所有登录账号可见；
        已经授权过账号的空间保持原样（仍然只有这些账号可见）。
        """
        no_grants = "NOT EXISTS (SELECT 1 FROM space_grants g WHERE g.space_id = spaces.id)"
        conn.execute("BEGIN")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(spaces)")}
        if "visibility" not in columns:
            # 版本 1 → 2：共享空间从"授权后才可见"改为"默认所有登录账号可见"。
            conn.execute(
                "ALTER TABLE spaces ADD COLUMN visibility TEXT NOT NULL DEFAULT 'only'"
                " CHECK (visibility IN ('all', 'only'))"
            )
            conn.execute(f"UPDATE spaces SET visibility = 'all' WHERE kind = 'shared' AND {no_grants}")
        if version < 3:
            # 版本 2 → 3：个人空间也改为默认所有登录账号可见。
            # 只在升级时执行这一次：之后管理员改成"仅指定账号可见"且名单为空的个人空间不会再被改回去。
            conn.execute(f"UPDATE spaces SET visibility = 'all' WHERE kind = 'personal' AND {no_grants}")

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """打开一个连接；正常退出时提交，异常时回滚。"""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()
