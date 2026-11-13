"""批量转换业务规则：任务登记、筛选口径与执行记录的对外视图。

状态推进统一交给 :mod:`app.services.batch_execution`，这里只做字段校验、
列表筛选和旧页面字段兼容。列表行既保留原中文字段（任务编号、输入路径、
处理脚本、输出路径、分区覆盖、依赖上游、重试策略、超时终止），也补充
执行记录、状态 code 和最后回执，导出与审计无需换字段名。
"""
from __future__ import annotations

from typing import Any

from app.services.batch_execution import BatchExecutionError, execution_service
from app.services.batch_models import (
    ACTION_CONTRACTS,
    STATUS_LABELS,
    BatchTaskDefinition,
    apply_execution_view,
    normalize_status,
    status_label,
)
from app.store import store

MODULE = "batch"
REQUIRED_FIELDS = ["任务编号", "输入路径", "处理脚本"]
STATUS_ORDER = [STATUS_LABELS[code] for code in ("draft", "scheduled", "running", "succeeded", "failed")]
ACTION_RULES = {item.label: STATUS_LABELS[item.target_status] for item in ACTION_CONTRACTS if item.legacy}
NEGATIVE_ACTIONS: list[str] = []


class BatchService:
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
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            wanted = normalize_status(status) or status
            rows = [
                row for row in rows
                if normalize_status(row.get("status_code") or row.get("status")) == wanted
                or row.get("status") == status
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        return [self._view(row) for row in page_rows], total

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        task_id = int(row.get("id", 0) or 0)
        try:
            execution = execution_service.ensure_for_task(task_id)
        except BatchExecutionError:
            execution = row.get("execution") or {}
        view = dict(row)
        if execution:
            view = apply_execution_view(view, execution)
        definition = BatchTaskDefinition.from_row(row)
        view["task_definition"] = definition.to_dict()
        view["任务定义"] = definition.to_dict()
        view["receipt"] = execution.get("latest_receipt")
        view["回执"] = execution.get("latest_receipt")
        view["latest_receipt"] = execution.get("latest_receipt")
        view["最后回执"] = execution.get("latest_receipt")
        return view

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        try:
            execution_service.ensure_for_task(entry_id)
        except BatchExecutionError:
            pass
        return self._view(row)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 登记时允许直接带上其余任务定义字段，旧页面只传三个必填字段也照常工作。
        for field in ("输出路径", "分区覆盖", "依赖上游", "重试策略", "超时终止"):
            if field in values:
                entry[field] = values.get(field)
        entry["status"] = STATUS_LABELS["draft"]
        entry["status_code"] = "draft"
        entry["execution_status"] = STATUS_LABELS["draft"]
        entry["执行状态"] = STATUS_LABELS["draft"]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        execution = execution_service.ensure_for_task(int(entry["id"]))
        return apply_execution_view(entry, execution), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        source: str = "page",
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """兼容旧调用：执行动作并返回 (任务视图, 消息)。"""
        view, message, _receipt = self.run_action_detail(
            entry_id, action, source=source, idempotency_key=idempotency_key
        )
        return view, message

    def run_action_detail(
        self,
        entry_id: int,
        action: str,
        *,
        source: str = "page",
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """执行页面动作，返回 (执行记录视图, 消息, 回执)。

        终态保护与幂等由执行服务统一处理：已完成/已失败不会被跳级覆盖，
        重复提交返回同一条回执。
        """
        try:
            execution, receipt, idempotent, _plans = execution_service.apply_action(
                entry_id,
                action,
                source=source,
                idempotency_key=idempotency_key,
            )
        except BatchExecutionError as error:
            return None, error.message, error.receipt
        task = store.find(MODULE, entry_id) or {}
        view = apply_execution_view(dict(task), execution)
        message = None
        if idempotent:
            message = f"动作「{receipt.get('action') or action}」已执行过，返回最新回执"
        return view, message or str(receipt.get("message") or f"批处理任务已{action}"), receipt


__all__ = [
    "ACTION_RULES",
    "BatchService",
    "MODULE",
    "NEGATIVE_ACTIONS",
    "REQUIRED_FIELDS",
    "STATUS_ORDER",
    "status_label",
]
