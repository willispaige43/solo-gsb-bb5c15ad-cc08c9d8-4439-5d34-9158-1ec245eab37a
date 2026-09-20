"""已编译场景的进程内缓存（单 worker 部署即权威缓存）。

场景定义按**不可变版本**管理：每次发布产生递增 revision，旧版本定义
永不覆盖。缓存键为 ``(场景名, revision)``，另维护 ``name -> 当前 revision``
指针；既有会话绑定发布时刻的 revision，因此重新发布不影响其行为。
"""
from __future__ import annotations

import threading
from typing import Iterable

from .db import get_conn, write_tx
from .models import Scenario
from .scenario import parse_scenario


class ScenarioStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._scenarios: dict[tuple[str, int], Scenario] = {}
        self._current: dict[str, int] = {}

    # -- 载入 ------------------------------------------------------------

    def load_all_from_db(self) -> int:
        conn = get_conn()
        revisions = conn.execute(
            "SELECT name, revision, definition FROM scenario_revisions"
        ).fetchall()
        currents = conn.execute(
            "SELECT name, current_revision FROM scenarios"
        ).fetchall()
        with self._lock:
            self._scenarios.clear()
            self._current.clear()
            for row in revisions:
                scenario = parse_scenario(row["definition"], name_override=row["name"])
                self._scenarios[(row["name"], row["revision"])] = scenario
            for row in currents:
                self._current[row["name"]] = row["current_revision"]
            return len(self._current)

    # -- 发布 / 激活 / 删除 -------------------------------------------------

    def publish(self, name: str, definition: str) -> tuple[Scenario, int, bool]:
        """校验并发布一个版本，返回 (编译结果, revision, 是否新建版本)。

        内容与当前版本完全相同（逐字节一致）时不新建版本，幂等返回当前
        revision；否则产生递增 revision 并设为当前版本，旧定义保留不变。
        """
        scenario = parse_scenario(definition, name_override=name)
        with write_tx() as conn:
            row = conn.execute(
                "SELECT current_revision FROM scenarios WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                revision = 1
                conn.execute(
                    "INSERT INTO scenarios(name, current_revision) VALUES (?, ?)",
                    (name, revision),
                )
                conn.execute(
                    "INSERT INTO scenario_revisions(name, revision, definition)"
                    " VALUES (?, ?, ?)",
                    (name, revision, definition),
                )
                created = True
            else:
                current = row["current_revision"]
                cur = conn.execute(
                    "SELECT definition FROM scenario_revisions"
                    " WHERE name = ? AND revision = ?",
                    (name, current),
                ).fetchone()
                if cur is not None and cur["definition"] == definition:
                    revision, created = current, False
                else:
                    revision = current + 1
                    conn.execute(
                        "INSERT INTO scenario_revisions(name, revision, definition)"
                        " VALUES (?, ?, ?)",
                        (name, revision, definition),
                    )
                    conn.execute(
                        "UPDATE scenarios SET current_revision = ?,"
                        " updated_at = datetime('now') WHERE name = ?",
                        (revision, name),
                    )
                    created = True
        with self._lock:
            self._scenarios[(name, revision)] = scenario
            self._current[name] = revision
        return scenario, revision, created

    def activate(self, name: str, revision: int) -> bool:
        """把当前指针切到指定 revision；目标不存在返回 False。

        只影响此后新建会话的默认绑定，既有会话仍按原绑定版本执行。
        """
        with write_tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM scenario_revisions WHERE name = ? AND revision = ?",
                (name, revision),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE scenarios SET current_revision = ?,"
                " updated_at = datetime('now') WHERE name = ?",
                (revision, name),
            )
        with self._lock:
            self._current[name] = revision
        return True

    def delete(self, name: str) -> bool:
        conn = get_conn()
        conn.execute("DELETE FROM scenario_revisions WHERE name = ?", (name,))
        cur = conn.execute("DELETE FROM scenarios WHERE name = ?", (name,))
        conn.commit()
        with self._lock:
            existed = self._current.pop(name, None) is not None
            self._scenarios = {
                key: value for key, value in self._scenarios.items() if key[0] != name
            }
        return cur.rowcount > 0 or existed

    # -- 查询 --------------------------------------------------------------

    def get(self, name: str) -> Scenario | None:
        """当前版本的编译结果。"""
        current = self.get_current(name)
        return current[0] if current else None

    def get_current(self, name: str) -> tuple[Scenario, int] | None:
        """原子地取 (当前版本编译结果, 当前 revision)。"""
        with self._lock:
            revision = self._current.get(name)
            if revision is None:
                return None
            scenario = self._scenarios.get((name, revision))
            if scenario is None:
                return None
            return scenario, revision

    def get_revision(self, name: str, revision: int) -> Scenario | None:
        """指定版本的编译结果；缓存未命中时回源数据库编译。"""
        with self._lock:
            scenario = self._scenarios.get((name, revision))
        if scenario is not None:
            return scenario
        row = get_conn().execute(
            "SELECT definition FROM scenario_revisions WHERE name = ? AND revision = ?",
            (name, revision),
        ).fetchone()
        if row is None:
            return None
        scenario = parse_scenario(row["definition"], name_override=name)
        with self._lock:
            self._scenarios[(name, revision)] = scenario
        return scenario

    def current_revision(self, name: str) -> int | None:
        with self._lock:
            return self._current.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._current)

    def all(self) -> Iterable[Scenario]:
        """各场景当前版本的编译结果。"""
        with self._lock:
            return [
                scenario
                for (name, revision), scenario in self._scenarios.items()
                if self._current.get(name) == revision
            ]


store = ScenarioStore()
