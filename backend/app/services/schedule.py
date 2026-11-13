"""编排调度业务规则：状态流转、字段校验与筛选口径都收在这里。

编排计划自身仍按旧动作流转；当计划关联了批处理任务时，计划视图会额外带出
同一条批处理执行记录的 ``execution_status`` / ``final_status``，调度器和页面
看到的终态因此与批处理一致。
"""
from __future__ import annotations

from typing import Any

from app.services.batch_execution import BatchExecutionError, execution_service
from app.store import store

MODULE = "schedule"
REQUIRED_FIELDS = ["计划编号", "任务列表", "依赖图定义"]
STATUS_ORDER = ["草稿", "待校验", "运行中", "已停止"]
ACTION_RULES = {"提交校验": "待校验", "启动编排": "运行中", "停止编排": "已停止"}
NEGATIVE_ACTIONS = []


def plan_view(plan: dict[str, Any]) -> dict[str, Any]:
    """读取计划时附上批处理执行视图，不改变计划原有字段。"""
    view = dict(plan)
    try:
        execution_service.sync_plan_view(int(plan.get("id", 0) or 0))
    except BatchExecutionError:
        pass
    for key in (
        "execution_status",
        "执行状态",
        "final_status",
        "最终状态",
        "trigger_source",
        "触发来源",
        "terminal",
        "batch_execution",
        "batch_executions",
        "batch_task_ids",
        "last_receipt",
        "最后回执",
    ):
        if key in plan:
            view[key] = plan[key]
    return view


class ScheduleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [plan_view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return plan_view(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 计划可以显式登记关联的批处理任务，页面/调度侧据此看到同一执行记录。
        if "batch_task_ids" in values:
            entry["batch_task_ids"] = self._normalize_task_ids(values.get("batch_task_ids"))
        if "关联批处理任务" in values:
            entry["batch_task_ids"] = self._normalize_task_ids(values.get("关联批处理任务"))
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"编排计划 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于编排调度可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"编排计划已{action}"

    @staticmethod
    def _normalize_task_ids(raw: Any) -> list[int]:
        if isinstance(raw, (int, str)):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        result: list[int] = []
        for item in raw:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                result.append(value)
        return sorted(set(result))
