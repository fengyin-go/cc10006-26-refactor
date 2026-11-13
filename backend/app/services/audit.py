"""操作审计业务规则：状态流转、字段校验与筛选口径都收在这里。

除了审计日志自身的动作流转，这里还提供写执行审计的统一入口：批处理任务无论
来自页面、调度器还是批处理回执，落审计时都带触发来源和最终状态。
"""
from __future__ import annotations

from typing import Any

from app.services.batch_models import utc_now
from app.store import store

MODULE = "audit"
REQUIRED_FIELDS = ["日志编号", "操作类型", "操作对象"]
STATUS_ORDER = ["草稿", "待审核", "已归档", "已忽略"]
ACTION_RULES = {"提交审核": "待审核", "归档日志": "已归档", "忽略日志": "已忽略"}
NEGATIVE_ACTIONS = ["忽略日志"]


class AuditService:
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
            rows = [row for row in rows if keyword in str(row.get("日志编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"审计日志 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于操作审计可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"审计日志已{action}"

    def record_execution(
        self,
        *,
        task_id: int,
        task_no: str,
        action: str,
        trigger_source: str,
        source: str,
        final_status: str,
        message: str,
        execution_id: str,
        receipt_id: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把一次批处理执行动作写成审计日志，触发来源和最终状态都在里面。

        审计日志自身仍是一个独立业务记录，所以给它一个稳定的初始状态「已归档」。
        同一回执只落一次审计，重复提交不会刷出多条。
        """
        rows = store.rows(MODULE)
        for row in rows:
            if row.get("receipt_id") == receipt_id:
                return row
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        entry: dict[str, Any] = {
            "id": next_id,
            "status": "已归档",
            "pending": False,
            # 异常口径只看最终状态，不因为来自调度器或回执就把正常完成算成异常。
            "abnormal": final_status == "已失败",
            "日志编号": f"BATCH-AUD-{next_id:04d}",
            "操作类型": "批处理执行",
            "操作对象": f"批处理任务 {task_no or task_id}",
            "操作详情": message,
            "操作来源": trigger_source,
            "触发来源": trigger_source,
            "source": source,
            "执行时间": utc_now(),
            "影响行数": 1,
            "回滚能力": "否",
            "execution_id": execution_id,
            "receipt_id": receipt_id,
            "回执编号": receipt_id,
            "task_id": task_id,
            "任务编号": task_no,
            "action": action,
            "动作": action,
            "trigger_source": trigger_source,
            "source_code": source,
            "最终状态": final_status,
            "final_status": final_status,
            "detail": detail or {},
        }
        rows.append(entry)
        return entry
