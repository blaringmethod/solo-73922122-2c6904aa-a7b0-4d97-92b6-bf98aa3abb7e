"""关键功能自测：以 TestClient 驱动应用，使用独立临时数据库。

运行：python tests/selftest.py
覆盖：导入校验、导入后不可更换、预览、安全/不安全发布、
两窗口各自安全但合并不安全的重核拒绝、公开页不泄露格内检出数、
公开记录落盘持久化、超过 JS 安全整数/有符号 64 位上限的大整数发布链路、
旧版 INTEGER 总数字段的库自动迁移（保留编号/总数/顺序）与重启后重核。
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="cdc_guard_test_")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")  # 须在导入 app 前设置

from fastapi.testclient import TestClient  # noqa: E402

import app.database as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.wire import INT64_MAX, JS_SAFE_MAX  # noqa: E402

Q = 6_000_000_000_000_000_000          # 单格计数：< 2^63-1，> 2^53-1
BIG_TOTAL_ALL = 3 * Q - 1             # 四格合计：超过有符号 64 位上限
BIG_TOTAL_OLD = 2 * Q - 3             # 两格「老年」合计：超过有符号 64 位上限

SAMPLE = (
    "分区,年龄组,人数,检出数\n"
    "城东,青年,10,2\n"
    "城东,老年,10,8\n"
    "城西,青年,10,8\n"
    "城西,老年,10,8\n"
)

BIG_SAMPLE = (
    "分区,年龄组,人数,检出数\n"
    f"城东,青年,{Q},2\n"
    f"城东,老年,{Q},{Q - 3}\n"
    f"城西,青年,{Q},{Q}\n"
    f"城西,老年,{Q},{Q}\n"
)

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond and extra:
        print("       " + extra.replace("\n", "\n       ")[:800])


def run():
    with TestClient(app) as client:
        def import_csv(text):
            return client.post(
                "/api/import",
                files={"file": ("data.csv", text.encode("utf-8"), "text/csv")},
            )

        # ---- 1. 导入校验 ----
        r = import_csv("分区,年龄组,人数,检出数\nA,青,4,1\n")
        check("人数<5 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数,检出数\nA,青,10,11\n")
        check("检出数>人数 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数,检出数\nA,青,10,-1\n")
        check("检出数<0 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数,检出数\nA,青,10,1.5\n")
        check("检出数非整数 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数,检出数\nA,青,10,1\nA,青,10,2\n")
        check("组合重复 被拒绝", r.status_code == 400, r.text)
        many = "分区,年龄组,人数,检出数\n" + "\n".join(
            f"区{i},青,10,1" for i in range(17)
        )
        r = import_csv(many)
        check("超过16格 被拒绝", r.status_code == 400, r.text)
        r = import_csv("分区,年龄组,人数\nA,青,10\n")
        check("缺少检出数列 被拒绝", r.status_code == 400, r.text)

        # ---- 2. 正常导入 + 不可更换 ----
        r = import_csv(SAMPLE)
        check("示例导入成功", r.status_code == 201 and r.json()["imported"] == 4, r.text)
        r = import_csv(SAMPLE)
        check("重复导入被拒绝（导入后不可更换）", r.status_code == 409, r.text)

        # ---- 3. 预览 ----
        r = client.post("/api/preview", json={"districts": ["城东"], "age_groups": ["青年", "老年"]})
        j = r.json()
        check("预览「城东」总数=10 且安全",
              r.status_code == 200 and j["total"] == 10 and j["safe"], r.text)

        # ---- 4. 两窗口各自安全、合并不安全 ----
        r = client.post("/api/preview", json={"districts": ["城东", "城西"], "age_groups": ["老年"]})
        j = r.json()
        check("窗口「老年」单独预览安全（总数=16）",
              r.status_code == 200 and j["total"] == 16 and j["safe"], r.text)

        r = client.post("/api/publish", json={"districts": ["城东"], "age_groups": ["青年", "老年"]})
        check("发布「城东」成功（总数=10）",
              r.status_code == 201 and r.json()["total"] == 10, r.text)

        r = client.post("/api/preview", json={"districts": ["城东", "城西"], "age_groups": ["老年"]})
        j = r.json()
        aff = j.get("affected", [])
        check("合并后预览「老年」不安全", r.status_code == 200 and not j["safe"], r.text)
        check("受影响格为 城东/青年，可行范围[0,4]，不可取≥5",
              len(aff) == 1
              and aff[0]["district"] == "城东" and aff[0]["age_group"] == "青年"
              and aff[0]["min"] == 0 and aff[0]["max"] == 4
              and aff[0]["zero_feasible"] and not aff[0]["ge5_feasible"], r.text)

        r = client.post("/api/publish", json={"districts": ["城东", "城西"], "age_groups": ["老年"]})
        check("确认发布「老年」被按最新公开记录重核拒绝(409)", r.status_code == 409, r.text)
        d = r.json().get("detail", {})
        check("409 响应含受影响格与可行范围",
              isinstance(d, dict) and d.get("affected")
              and d["affected"][0]["max"] == 4, r.text)

        # ---- 5. 与已公开记录合并仍安全的发布可继续 ----
        r = client.post("/api/publish", json={"districts": ["城东", "城西"], "age_groups": ["青年"]})
        check("发布「青年」（与「城东」合并仍安全）成功",
              r.status_code == 201 and r.json()["total"] == 10, r.text)
        r = client.post("/api/publish", json={"districts": ["城东", "城西"], "age_groups": ["老年"]})
        check("「老年」仍被拒绝", r.status_code == 409, r.text)

        # ---- 6. 公开页不泄露格内检出数 ----
        r = client.get("/api/public")
        j = r.json()
        check("公开 API 不含 detected 字段",
              r.status_code == 200 and all("detected" not in c for c in j["cells"]), r.text)
        check("公开 API 含人数与 2 条获准汇总",
              len(j["cells"]) == 4 and all("population" in c for c in j["cells"])
              and len(j["publications"]) == 2, r.text)
        check("公开汇总不含格值字段",
              all(set(p) == {"id", "districts", "age_groups", "total", "created_at"}
                  for p in j["publications"]), r.text)
        r = client.get("/public")
        check("公开页可访问", r.status_code == 200 and "公开" in r.text)
        r = client.get("/")
        check("内部审查台可访问", r.status_code == 200 and "审查台" in r.text)

        # ---- 7. 持久化（直接读库文件，等价于重启后再读） ----
        conn = sqlite3.connect(os.environ["DB_PATH"])
        n_pub = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        n_cell = conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
        conn.close()
        check("公开记录与格值已落盘（重启保留）", n_pub == 2 and n_cell == 4)

        # ---- 8. 其他边界 ----
        r = client.post("/api/preview", json={"districts": ["不存在"], "age_groups": ["青年"]})
        check("未知分区被拒绝", r.status_code == 400, r.text)
        r = client.post("/api/publish", json={"districts": [], "age_groups": ["青年"]})
        check("空选择被拒绝", r.status_code == 400, r.text)
        r = client.post("/api/preview", json={"districts": ["城东"], "age_groups": []})
        check("空年龄组被拒绝", r.status_code == 400, r.text)

        # ---- 9. 保存失败不留半次发布 ----
        import app.database as dbmod
        import app.main as mainmod

        before = len(client.get("/api/public").json()["publications"])

        # 9a. 接口层：插入时发生异常 → 500 且无记录产生
        orig_insert = mainmod.insert_publication
        def _boom(*a, **k):
            raise RuntimeError("模拟保存失败")
        mainmod.insert_publication = _boom
        try:
            r = client.post(
                "/api/publish",
                json={"districts": ["城东", "城西"], "age_groups": ["青年"]},
            )
            check("保存时异常返回 500", r.status_code == 500, r.text)
        finally:
            mainmod.insert_publication = orig_insert
        after = len(client.get("/api/public").json()["publications"])
        check("保存失败未留下半次发布", after == before, f"before={before} after={after}")

        # 9b. 存储层：库不可打开时写入抛错，且库内容不变
        orig_path = dbmod.DB_PATH
        dbmod.DB_PATH = _TMP  # 指向目录：SQLite 无法打开
        try:
            dbmod.insert_publication(["x"], ["y"], 1)
            check("库不可打开时写入抛错", False, "未抛错")
        except Exception:
            check("库不可打开时写入抛错", True)
        finally:
            dbmod.DB_PATH = orig_path
        after = len(client.get("/api/public").json()["publications"])
        check("库写失败后记录数不变", after == before, f"before={before} after={after}")

        # ---- 10. 求解器并发安全（z3 每评估独立 Context） ----
        from concurrent.futures import ThreadPoolExecutor

        from app.solver import evaluate

        cells = dbmod.get_cells()
        pubs = dbmod.get_publications()
        cand = {"districts": ["城东", "城西"], "age_groups": ["老年"], "total": 16}
        with ThreadPoolExecutor(max_workers=8) as ex:
            outs = list(ex.map(lambda _: evaluate(cells, pubs, cand), range(40)))
        check(
            "40 路并发求解结果一致且正确",
            all(
                (not o["safe"])
                and len(o["affected"]) == 1
                and o["affected"][0]["max"] == 4
                for o in outs
            ),
            str(outs[:2]),
        )


def run_big_integer():
    """第 11 组：超过 JS 安全整数 / 有符号 64 位上限的发布链路（独立临时库）。"""
    big_dir = tempfile.mkdtemp(prefix="cdc_guard_big_")
    big_db = Path(big_dir) / "big.db"
    orig_db_path = dbmod.DB_PATH
    dbmod.DB_PATH = str(big_db)
    try:
        with TestClient(app) as client:
            def import_csv(text):
                return client.post(
                    "/api/import",
                    files={"file": ("big.csv", text.encode("utf-8"), "text/csv")},
                )

            # 11.1 导入校验：单格超过有符号 64 位上限被拒
            over = INT64_MAX + 1
            r = import_csv(
                "分区,年龄组,人数,检出数\n"
                f"A,青,{over},1\n"
            )
            check("人数超 64 位上限被拒绝", r.status_code == 400, r.text)
            r = import_csv(
                "分区,年龄组,人数,检出数\n"
                f"A,青,{INT64_MAX},{INT64_MAX + 1}\n"
            )
            check("检出数超 64 位上限被拒绝", r.status_code == 400, r.text)

            # 11.2 合法大整数数据导入成功，线格式：安全范围内仍是 JSON 数字
            r = import_csv(BIG_SAMPLE)
            check("大整数示例导入成功", r.status_code == 201 and r.json()["imported"] == 4, r.text)

            r = client.get("/api/admin/state")
            j = r.json()
            by_key = {(c["district"], c["age_group"]): c for c in j["cells"]}
            young = by_key[("城东", "青年")]
            check("单格人数/检出数超 JS 安全上限时以十进制字符串返回",
                  isinstance(young["population"], str) and young["population"] == str(Q)
                  and young["detected"] == 2 and isinstance(young["detected"], int), r.text)
            # 原始 JSON 文本里必须是带引号的字符串，证明未走 JSON number 丢精度
            raw = json.loads(r.content)
            raw_young = {(c["district"], c["age_group"]): c for c in raw["cells"]}[
                ("城东", "青年")
            ]
            check("原始 JSON 中人数为字符串字面量（不丢精度）",
                  raw_young["population"] == str(Q), r.text[:300])

            # 11.3 全部格预览：合计超 64 位，仍按精确整数审查且通过
            all_sel = {"districts": ["城东", "城西"], "age_groups": ["青年", "老年"]}
            r = client.post("/api/preview", json=all_sel)
            j = r.json()
            check("全格预览合计精确且为字符串（超 JS 安全上限）",
                  r.status_code == 200 and j["total"] == str(BIG_TOTAL_ALL)
                  and isinstance(j["total"], str), r.text)
            check("超 64 位合计仍按原小样本规则审查通过", j["safe"] is True, r.text)

            # 11.4 确认发布：曾经会因 INTEGER 列溢出而保存失败，现在成功
            r = client.post("/api/publish", json=all_sel)
            j = r.json()
            check("超 64 位合计确认发布成功（不再保存失败）",
                  r.status_code == 201 and j["total"] == str(BIG_TOTAL_ALL), r.text)
            check("发布编号仍为 JSON 数字（小整数字段保持原类型）",
                  isinstance(j["id"], int) and j["id"] == 1, r.text)

            # 11.5 落盘为精确十进制文本，读回仍是精确 Python int
            conn = sqlite3.connect(big_db)
            stored = conn.execute("SELECT total FROM publications WHERE id = 1").fetchone()[0]
            conn.close()
            check("库中以 TEXT 精确保存超 64 位总数",
                  isinstance(stored, str) and int(stored) == BIG_TOTAL_ALL, repr(stored))
            check("存储层读回总数精确无损",
                  dbmod.get_publications()[0]["total"] == BIG_TOTAL_ALL)

            # 11.6 受大总数约束的小样本格：未通过时仍给出受影响格与精确可行范围
            old_sel = {"districts": ["城东", "城西"], "age_groups": ["老年"]}
            r = client.post("/api/preview", json=old_sel)
            j = r.json()
            check("「老年」预览合计精确（超 64 位）",
                  j["total"] == str(BIG_TOTAL_OLD), r.text)
            check("与已发布全格记录合并后审查不通过", j["safe"] is False, r.text)
            aff = j["affected"]
            check("受影响格 城东/青年 可行范围 [2, Q] 且端点精确",
                  len(aff) == 1 and aff[0]["district"] == "城东"
                  and aff[0]["age_group"] == "青年"
                  and aff[0]["min"] == 2 and aff[0]["max"] == str(Q)
                  and aff[0]["zero_feasible"] is False
                  and aff[0]["ge5_feasible"] is True, r.text)
            r = client.post("/api/publish", json=old_sel)
            check("确认发布被重核拒绝(409)，详情含精确可行范围",
                  r.status_code == 409
                  and r.json()["detail"]["affected"][0]["max"] == str(Q), r.text)

            # 11.7 另一安全窗口（城西，合计 2Q 超 int64）可发布
            west_sel = {"districts": ["城西"], "age_groups": ["青年", "老年"]}
            r = client.post("/api/publish", json=west_sel)
            check("「城西」大总数发布成功（2Q 超 int64）",
                  r.status_code == 201 and r.json()["total"] == str(2 * Q), r.text)

            # 11.8 公开页：人数与各发布总数均精确，且不含格内检出数
            r = client.get("/api/public")
            j = r.json()
            check("公开 API 人数精确为字符串且无 detected",
                  all(isinstance(c["population"], str) and c["population"] == str(Q)
                      and "detected" not in c for c in j["cells"]), r.text[:400])
            check("公开 API 两条汇总总数精确、字段保持原样",
                  [p["total"] for p in j["publications"]]
                  == [str(BIG_TOTAL_ALL), str(2 * Q)]
                  and all(set(p) == {"id", "districts", "age_groups", "total", "created_at"}
                          for p in j["publications"]), r.text[:400])

            # 11.9 安全范围内的小整数计数仍返回 JSON 数字（字段不变、类型不变）
            r = client.post(
                "/api/preview",
                json={"districts": ["城东"], "age_groups": ["青年"]},
            )
            j = r.json()
            check("小整数计数仍为 JSON 数字（城东/青年检出=2）",
                  j["total"] == 2 and isinstance(j["total"], int)
                  and isinstance(j["cells_matched"], int), r.text)

        # 11.10 重启模拟：新进程视角重新打开同一库，精确总数参与后续重核
        with TestClient(app) as client2:
            pubs = dbmod.get_publications()
            check("重启后发布记录数与编号保留",
                  [p["id"] for p in pubs] == [1, 2]
                  and [p["total"] for p in pubs] == [BIG_TOTAL_ALL, 2 * Q])
            # 已存在全格(3Q-1)+城西(2Q)两条精确大总数记录，城东青年被钉死为 2：
            # 重核须仍判不安全，并给出精确的小整数可行范围 [2, 2]。
            r = client2.post("/api/publish", json=old_sel)
            check("重启后按精确总数重核仍拒绝不安全发布(409)",
                  r.status_code == 409
                  and r.json()["detail"]["affected"][0]["min"] == 2
                  and r.json()["detail"]["affected"][0]["max"] == 2, r.text)
            r = client2.post("/api/publish", json=west_sel)
            check("重启后重复安全窗口按原规则仍可发布，编号接续为 3",
                  r.status_code == 201 and r.json()["id"] == 3
                  and r.json()["total"] == str(2 * Q), r.text)
    finally:
        dbmod.DB_PATH = orig_db_path


def run_legacy_migration():
    """第 12 组：旧版 INTEGER total 库自动迁移，保留编号、总数、顺序。"""
    mig_dir = tempfile.mkdtemp(prefix="cdc_guard_mig_")
    mig_db = Path(mig_dir) / "legacy.db"
    # 手工构造旧版库（total 为 INTEGER，CHECK total>=0），并写入两条记录。
    conn = sqlite3.connect(mig_db)
    conn.executescript(
        """
        CREATE TABLE cells (
            district TEXT NOT NULL, age_group TEXT NOT NULL,
            population INTEGER NOT NULL CHECK (population >= 5),
            detected INTEGER NOT NULL CHECK (detected >= 0 AND detected <= population),
            PRIMARY KEY (district, age_group)
        );
        CREATE TABLE publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            districts TEXT NOT NULL, age_groups TEXT NOT NULL,
            total INTEGER NOT NULL CHECK (total >= 0),
            created_at TEXT NOT NULL
        );
        INSERT INTO cells (district, age_group, population, detected)
        VALUES ('城东', '青年', 10, 8),
               ('城东', '老年', 10, 2),
               ('城西', '青年', 10, 7),
               ('城西', '老年', 10, 3);
        INSERT INTO publications (districts, age_groups, total, created_at)
        VALUES ('["城东"]', '["青年"]', 8, '2026-01-01T00:00:00+00:00'),
               ('["城西"]', '["青年"]', 7, '2026-01-02T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    orig_db_path = dbmod.DB_PATH
    dbmod.DB_PATH = str(mig_db)
    try:
        with TestClient(app) as client:  # lifespan 触发 init_db 迁移
            pubs = dbmod.get_publications()
            check("迁移后发布编号/顺序/总数保留",
                  [p["id"] for p in pubs] == [1, 2]
                  and [p["total"] for p in pubs] == [8, 7]
                  and pubs[1]["created_at"] == "2026-01-02T00:00:00+00:00",
                  str(pubs))
            cells = dbmod.get_cells()
            check("迁移后格值完整保留",
                  len(cells) == 4
                  and {(c["district"], c["age_group"]): c["detected"] for c in cells}
                  == {("城东", "青年"): 8, ("城东", "老年"): 2,
                      ("城西", "青年"): 7, ("城西", "老年"): 3},
                  str(cells))
            conn = sqlite3.connect(mig_db)
            col_type = conn.execute(
                "SELECT type FROM pragma_table_info('publications') WHERE name='total'"
            ).fetchone()[0]
            seq = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='publications'"
            ).fetchone()[0]
            conn.close()
            check("迁移后 total 列为 TEXT 且 AUTOINCREMENT 序列保留为 2",
                  col_type.upper() == "TEXT" and seq == 2, f"{col_type=} {seq=}")

            # 新窗口「全部格合计 20」不与历史窄发布冲突：小样本格仍保留 0 与 ≥5 解
            r = client.post(
                "/api/publish",
                json={"districts": ["城东", "城西"],
                      "age_groups": ["青年", "老年"]},
            )
            check("迁移库上可继续发布且编号接续为 3",
                  r.status_code == 201 and r.json()["id"] == 3
                  and r.json()["total"] == 20, r.text)
            r = client.get("/api/public")
            check("迁移库公开 API 编号与总数准确（小整数保持 JSON 数字）",
                  [p["id"] for p in r.json()["publications"]] == [1, 2, 3]
                  and [p["total"] for p in r.json()["publications"]] == [8, 7, 20]
                  and all(isinstance(p["total"], int)
                          for p in r.json()["publications"]), r.text[:400])
    finally:
        dbmod.DB_PATH = orig_db_path


run()
run_big_integer()
run_legacy_migration()
print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
