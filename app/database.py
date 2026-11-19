"""SQLite 持久化层。

- cells：导入的调查格值（导入后不可更换，无更新/删除接口）。
- publications：已获准公开的汇总记录（不可撤销，只插入，无更新/删除接口）。

大整数说明：
- 单格人数/检出数限定在有符号 64 位整数内，沿用 SQLite INTEGER 存储；
- 一次发布的检出"合计"可能超过有符号 64 位上限（多格求和），
  SQLite INTEGER 无法存放，故 publications.total 采用 TEXT 保存精确十进制值。
- init_db 会识别旧版 INTEGER 总数字段并自动重建表迁移，
  完整保留发布编号(id)、总数与发布顺序（含 AUTOINCREMENT 序列）。

所有写操作均在单事务中完成：失败即整体回滚，不会留下半次发布。
连接按操作创建，启用 WAL，重启后数据保留在 DB_PATH 指定的库文件中。
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "app.db"
DB_PATH = os.environ.get("DB_PATH", str(_DEFAULT_DB))

# 新库结构：total 为 TEXT，存放非负整数的精确十进制表示。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    district   TEXT NOT NULL,
    age_group  TEXT NOT NULL,
    population INTEGER NOT NULL CHECK (population >= 5),
    detected   INTEGER NOT NULL CHECK (detected >= 0 AND detected <= population),
    PRIMARY KEY (district, age_group)
);
CREATE TABLE IF NOT EXISTS publications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    districts  TEXT NOT NULL,          -- JSON 数组
    age_groups TEXT NOT NULL,          -- JSON 数组
    total      TEXT NOT NULL CHECK (typeof(total) = 'text' AND total != ''),
    created_at TEXT NOT NULL           -- ISO8601 UTC
);
"""


def connect() -> sqlite3.Connection:
    path = DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> dict:
    return {
        r["name"]: r
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _migrate_legacy_publications(conn: sqlite3.Connection) -> None:
    """把旧版 INTEGER total 的 publications 表重建为 TEXT total。

    在单事务内完成：建临时新表 -> 逐行复制（CAST 为 TEXT 保留精确值）->
    删旧表 -> 改名 -> 恢复 AUTOINCREMENT 序列。
    发布编号、总数、顺序全部保留。
    """
    cols = _table_columns(conn, "publications")
    if "total" in cols and str(cols["total"]["type"]).upper() == "TEXT":
        return  # 已是新结构
    conn.execute("ALTER TABLE publications RENAME TO publications_legacy")
    conn.execute(
        "CREATE TABLE publications ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "districts TEXT NOT NULL, age_groups TEXT NOT NULL, "
        "total TEXT NOT NULL CHECK (typeof(total) = 'text' AND total != ''), "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO publications (id, districts, age_groups, total, created_at) "
        "SELECT id, districts, age_groups, CAST(total AS TEXT), created_at "
        "FROM publications_legacy ORDER BY id"
    )
    conn.execute("DROP TABLE publications_legacy")
    # 恢复 AUTOINCREMENT 序列，使后续编号在旧最大 id 之后继续。
    # sqlite_sequence 无主键，INSERT OR REPLACE 不会去重，先删后插保持单行。
    max_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM publications"
    ).fetchone()["m"]
    if max_id:
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'publications'")
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES('publications', ?)",
            (max_id,),
        )


def init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.executescript(_SCHEMA)
        # 旧库可能由更早版本创建：存在 publications 表但 total 仍为 INTEGER。
        if _table_columns(conn, "publications"):
            _migrate_legacy_publications(conn)


def get_cells() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT district, age_group, population, detected FROM cells "
            "ORDER BY district, age_group"
        ).fetchall()
    return [dict(r) for r in rows]


def insert_cells(rows: list[dict]) -> None:
    """单事务写入全部格值；任一失败则整体回滚。组合重复抛 IntegrityError。"""
    with closing(connect()) as conn, conn:
        conn.executemany(
            "INSERT INTO cells (district, age_group, population, detected) "
            "VALUES (:district, :age_group, :population, :detected)",
            rows,
        )


def get_publications() -> list[dict]:
    """读取全部公开记录，按编号升序；total 还原为精确 Python int（任意精度）。"""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, districts, age_groups, total, created_at "
            "FROM publications ORDER BY id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["districts"] = json.loads(d["districts"])
        d["age_groups"] = json.loads(d["age_groups"])
        # 新库存 TEXT；兼容旧库可能残留的 INTEGER：统一经 str() 再精确解析。
        d["total"] = int(str(d["total"]))
        out.append(d)
    return out


def insert_publication(districts: list[str], age_groups: list[str], total: int) -> dict:
    """单事务插入一条公开记录；失败回滚，不会留下半次发布。

    total 以精确十进制字符串保存，可承载超过有符号 64 位上限的合计。
    """
    total = int(total)  # 拒绝 bool/浮点等非整数输入
    if total < 0:
        raise ValueError("公开总数不能为负")
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO publications (districts, age_groups, total, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                json.dumps(list(districts), ensure_ascii=False),
                json.dumps(list(age_groups), ensure_ascii=False),
                str(total),
                created,
            ),
        )
        pid = cur.lastrowid
    return {
        "id": pid,
        "districts": list(districts),
        "age_groups": list(age_groups),
        "total": total,
        "created_at": created,
    }
