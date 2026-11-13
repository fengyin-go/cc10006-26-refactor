"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 批处理执行记录是跨模块的共用对象，不单独算一个业务模块，
        # 避免运营概览和健康检查把内部表也统计进去。
        self._executions: list[dict[str, Any]] = []

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def executions(self) -> list[dict[str, Any]]:
        return self._executions

    def find_execution(self, execution_id: str) -> dict[str, Any] | None:
        for row in self._executions:
            if row.get("execution_id") == execution_id:
                return row
        return None

    def execution_for_task(self, task_id: int) -> dict[str, Any] | None:
        for row in self._executions:
            if int(row.get("task_id", 0) or 0) == task_id:
                return row
        return None

    def reset(self) -> None:
        """恢复示例数据；测试和本地调试时可以重新拿到干净状态。"""
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._executions = []
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
