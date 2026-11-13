"""批处理执行服务：三条触发路径围绕同一条执行记录推进状态。

页面动作、调度器超时、批处理回执都调用 :class:`BatchExecutionService`，
状态推进、终态保护、幂等和回执落库只在这里做一次。

约定：
- 每个批处理任务只有一条 ``BatchExecution``，存放在 ``batch_execution`` 表；
- 批处理任务行只保留执行记录视图，旧字段（任务编号、输入路径、处理脚本、
  输出路径、分区覆盖、依赖上游、重试策略、超时终止）原样可读；
- 回执写入后，任务行、执行记录、关联编排计划和审计都由同一个回执驱动；
- 已完成/已失败是终态：新动作不能跳级覆盖，只有完全相同的重复请求按幂等返回；
- 若旧记录状态与回执不一致，以最新一条 ``valid=True`` 的回执为准。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.services.audit import AuditService
from app.services.batch_models import (
    ACTION_CONTRACTS,
    STATUS_LABELS,
    TERMINAL_STATUSES,
    BatchExecution,
    BatchTaskDefinition,
    ExecutionReceipt,
    apply_execution_view,
    new_execution_record,
    normalize_source,
    normalize_status,
    resolve_action,
)
from app.store import store

MODULE = "batch"
EXECUTION_MODULE = "batch_execution"
PLAN_MODULE = "schedule"

# 计划里的任务引用只认显式写法（如 "batch:1"、"task_id=2"、纯数字列表），
# 避免把描述性文本里的数字误当成任务 id。
_PLAN_TASK_REFERENCE = re.compile(r"(?:batch|task|任务|id)\s*[:=#：]?\s*(\d+)", re.IGNORECASE)

audit_service = AuditService()


class BatchExecutionError(Exception):
    """执行记录层面的可读错误。"""

    def __init__(self, message: str, *, entry: dict[str, Any] | None = None, receipt: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.entry = entry
        self.receipt = receipt


def _latest_valid_receipt(receipts: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    for receipt in reversed(list(receipts)):
        if receipt.get("valid", True):
            return receipt
    return None


class BatchExecutionService:
    # ------------------------------------------------------------------
    # 执行记录存取
    # ------------------------------------------------------------------

    def _task_row(self, task_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, task_id)

    def _execution_rows(self) -> list[dict[str, Any]]:
        return store.executions()

    def _find_execution(self, task_id: int) -> dict[str, Any] | None:
        return store.execution_for_task(task_id)

    def _find_execution_by_id(self, execution_id: str) -> dict[str, Any] | None:
        return store.find_execution(execution_id)

    def _store_execution(self, execution: dict[str, Any]) -> dict[str, Any]:
        """执行记录只保存规范结构，中文别名在输出时再生成。"""
        record = BatchExecution.from_dict(
            execution,
            fallback_task_id=int(execution.get("task_id", 0) or 0),
            fallback_task_no=str(execution.get("task_no") or ""),
        ).to_record()
        rows = self._execution_rows()
        existing = self._find_execution(int(record.get("task_id", 0) or 0))
        if existing is not None:
            existing.clear()
            existing.update(record)
            return existing
        rows.append(record)
        return record

    def _reconcile(self, execution: dict[str, Any]) -> dict[str, Any]:
        """以最新有效回执为准，修正执行记录与任务行的冲突状态。"""
        model = BatchExecution.from_dict(execution)
        latest = _latest_valid_receipt(model.receipts)
        if latest is not None:
            receipt_status = normalize_status(latest.get("status_code") or latest.get("status"))
            if receipt_status is not None:
                model.status = receipt_status
            model.last_receipt_id = str(latest.get("receipt_id") or "")
            model.last_action = str(latest.get("action") or latest.get("action_label") or "")
            model.trigger_source = latest.get("trigger_source")
            model.source = latest.get("source_code") or latest.get("source")
        model.revision = max(model.revision, len(model.receipts))
        if model.status in TERMINAL_STATUSES:
            model.finished_at = model.finished_at or model.updated_at
        elif model.status == "running":
            model.started_at = model.started_at or model.updated_at
        return model.to_dict()

    def ensure_for_task(self, task_id: int) -> dict[str, Any]:
        """读取或补齐批处理任务的执行记录；不覆盖旧动作语义。"""
        task = self._task_row(task_id)
        if task is None:
            raise BatchExecutionError(f"批处理任务 {task_id} 不存在或已归档")
        existing = self._find_execution(task_id)
        if existing is not None:
            execution = self._reconcile(existing)
            definition = BatchTaskDefinition.from_row(task)
            execution["task_definition"] = definition.to_dict()
            self._store_execution(execution)
            apply_execution_view(task, execution)
            return execution

        mirrored = task.get("execution")
        if isinstance(mirrored, dict) and mirrored:
            execution = BatchExecution.from_dict(
                mirrored,
                fallback_task_id=task_id,
                fallback_task_no=BatchTaskDefinition.from_row(task).task_no,
            ).to_dict()
            execution["task_id"] = task_id
            execution = self._reconcile(execution)
        else:
            # 旧记录没有执行记录：按原任务字段补一条，状态和动作语义原样保留。
            execution = new_execution_record(task, BatchTaskDefinition.from_row(task))
            execution = self._reconcile(execution)
        self._store_execution(execution)
        apply_execution_view(task, execution)
        return execution

    def get_execution(self, task_id: int) -> dict[str, Any]:
        return self.ensure_for_task(task_id)

    def find_by_execution_id(self, execution_id: str) -> dict[str, Any] | None:
        execution = self._find_execution_by_id(execution_id)
        if execution is None:
            return None
        task_id = int(execution.get("task_id", 0) or 0)
        return self.ensure_for_task(task_id)

    def list_executions(self) -> list[dict[str, Any]]:
        for task in store.rows(MODULE):
            self.ensure_for_task(int(task.get("id", 0) or 0))
        return [self._reconcile(row) for row in self._execution_rows()]
    # ------------------------------------------------------------------
    # 动作推进
    # ------------------------------------------------------------------

    def apply_action(
        self,
        task_id: int,
        action: str,
        *,
        source: str = "page",
        idempotency_key: str | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool, list[dict[str, Any]]]:
        """执行一次动作，返回 (执行记录, 回执, 是否幂等, 受影响的计划列表)。"""
        contract = resolve_action(action)
        if contract is None:
            raise BatchExecutionError(f"动作「{action}」不属于批量转换可执行范围")
        execution = self.ensure_for_task(task_id)
        source_label, source_code = normalize_source(source, default=contract.default_source)
        task = self._task_row(task_id)
        if task is None:
            raise BatchExecutionError(f"批处理任务 {task_id} 不存在或已归档")

        current = str(execution.get("status_code") or "draft")

        # 显式幂等键：同一个键只落一条回执。
        if idempotency_key:
            for receipt in execution.get("receipts") or []:
                if receipt.get("idempotency_key") == idempotency_key:
                    return execution, receipt, True, []

        terminal = current in TERMINAL_STATUSES
        last = execution.get("latest_receipt") or {}
        last_action_code = str(last.get("action_code") or "")
        same_action = (
            last_action_code == contract.code
            or (not last_action_code and last.get("action") == contract.label)
        ) and normalize_status(last.get("status_code")) == contract.target_status
        if terminal:
            # 已经处于同一终态：再来的同向动作按幂等处理，不再写回执和审计。
            if contract.target_status == current:
                return execution, last or self._legacy_terminal_receipt(execution, contract, source_label, source_code), True, []
            label = STATUS_LABELS.get(current, current)
            raise BatchExecutionError(
                f"批处理任务已处于终态「{label}」，动作「{contract.label}」不能覆盖最新有效回执",
                entry=apply_execution_view(dict(task), execution),
                receipt=last or None,
            )
        if same_action and current == contract.target_status:
            # 同一动作重复提交：不重复推进，也不再刷审计。
            return execution, last, True, []
        if current not in contract.allowed_from:
            raise BatchExecutionError(
                f"当前状态「{STATUS_LABELS.get(current, current)}」不允许执行动作「{contract.label}」",
                entry=apply_execution_view(dict(task), execution),
            )

        receipt = ExecutionReceipt.create(
            execution_id=str(execution.get("execution_id") or ""),
            task_id=task_id,
            task_no=str(execution.get("task_no") or ""),
            action_label=contract.label,
            action_code=contract.code,
            status=contract.target_status,
            trigger_source=source_label,
            source=source_code,
            message=message or f"批处理任务已{contract.label}",
            revision=int(execution.get("revision") or 0) + 1,
            idempotency_key=idempotency_key,
            task_snapshot=dict(execution.get("task_definition") or {}),
            detail={
                "target_status": contract.target_status,
                "target_status_label": STATUS_LABELS[contract.target_status],
                "previous_status": current,
                "previous_status_label": STATUS_LABELS.get(current, current),
                "allowed_from": sorted(contract.allowed_from),
                **(extra or {}),
            },
        ).to_dict()

        # 先落审计，再把审计编号写进回执，保证同一条回执可以反查审计。
        audit = audit_service.record_execution(
            task_id=task_id,
            task_no=str(execution.get("task_no") or ""),
            action=contract.label,
            trigger_source=source_label,
            source=source_code,
            final_status=STATUS_LABELS[contract.target_status],
            message=receipt["message"],
            execution_id=str(execution.get("execution_id") or ""),
            receipt_id=receipt["receipt_id"],
            detail={
                "action_code": contract.code,
                "previous_status": STATUS_LABELS.get(current, current),
                "final_status": STATUS_LABELS[contract.target_status],
                "source": source_code,
            },
        )
        receipt["audit_id"] = audit.get("id")

        execution["receipts"] = list(execution.get("receipts") or []) + [receipt]
        execution["revision"] = receipt["revision"]
        execution["status_code"] = contract.target_status
        execution["status_label"] = STATUS_LABELS[contract.target_status]
        execution["last_action"] = contract.code
        execution["last_receipt_id"] = receipt["receipt_id"]
        execution["trigger_source"] = source_label
        execution["source"] = source_code
        execution["updated_at"] = receipt["created_at"]
        execution["latest_receipt"] = receipt
        if contract.target_status == "running":
            execution["started_at"] = execution.get("started_at") or receipt["created_at"]
        if contract.target_status in TERMINAL_STATUSES:
            execution["finished_at"] = receipt["created_at"]
        execution["terminal"] = contract.target_status in TERMINAL_STATUSES
        execution["pending"] = not execution["terminal"]

        execution = self._reconcile(execution)
        self._store_execution(execution)
        apply_execution_view(task, execution)

        plans = self._sync_plans(task_id, execution, receipt)
        return execution, receipt, False, plans

    def _legacy_terminal_receipt(
        self,
        execution: dict[str, Any],
        contract: Any,
        source_label: str,
        source_code: str,
    ) -> dict[str, Any]:
        """旧记录本身已是终态时，给页面返回一条只读的合成回执。

        这条回执不写入执行记录，也不落审计，只用于说明"当前终态已经由历史
        记录确定"，避免重复请求被误判为一次新的状态推进。
        """
        return ExecutionReceipt.create(
            execution_id=str(execution.get("execution_id") or ""),
            task_id=int(execution.get("task_id") or 0),
            task_no=str(execution.get("task_no") or ""),
            action_label=contract.label,
            action_code=contract.code,
            status=str(execution.get("status_code") or contract.target_status),
            trigger_source=source_label,
            source=source_code,
            message=f"批处理任务已处于终态「{STATUS_LABELS.get(str(execution.get('status_code')), '')}」，未重复推进",
            revision=int(execution.get("revision") or 0),
            task_snapshot=dict(execution.get("task_definition") or {}),
            detail={"terminal": True, "synthetic": True},
        ).to_dict()

    def apply_receipt(
        self,
        *,
        task_id: int | None = None,
        task_no: str | None = None,
        execution_id: str | None = None,
        action: str | None = None,
        status: str | None = None,
        source: str = "batch",
        idempotency_key: str | None = None,
        receipt_id: str | None = None,
        message: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool, list[dict[str, Any]]]:
        """批处理回执写入入口：完成/失败都落到同一条执行记录。"""
        resolved_id = task_id
        if resolved_id is None and execution_id:
            execution = self.find_by_execution_id(execution_id)
            if execution is not None:
                resolved_id = int(execution.get("task_id", 0) or 0)
        if resolved_id is None and task_no:
            for task in store.rows(MODULE):
                definition = BatchTaskDefinition.from_row(task)
                if definition.task_no == str(task_no).strip():
                    resolved_id = int(task.get("id", 0) or 0)
                    break
        if resolved_id is None:
            raise BatchExecutionError("回执缺少可定位的任务：task_id、task_no 或 execution_id 至少给一个")

        resolved_action = action
        if resolved_action is None and status is not None:
            code = normalize_status(status)
            resolved_action = "complete" if code == "succeeded" else "fail" if code == "failed" else None
        if resolved_action is None:
            resolved_action = "complete"
        contract = resolve_action(resolved_action)
        if contract is None:
            raise BatchExecutionError(f"回执动作「{resolved_action}」不属于批量转换可执行范围")

        resolved_key = idempotency_key or receipt_id
        execution, receipt, idempotent, plans = self.apply_action(
            resolved_id,
            contract.label,
            source=source,
            idempotency_key=resolved_key,
            message=message or f"批处理回执已{contract.label}",
            extra={"receipt_origin": "batch", "batch_detail": detail or {}},
        )
        if receipt_id and receipt.get("receipt_id") != receipt_id:
            receipt = self._record_alias_receipt(execution, contract, receipt, receipt_id)
        return execution, receipt, idempotent, plans

    def _record_alias_receipt(
        self,
        execution: dict[str, Any],
        contract: Any,
        receipt: dict[str, Any],
        receipt_id: str,
    ) -> dict[str, Any]:
        """回执方自带编号时，保留原编号作为可追溯别名。"""
        target = receipt.get("receipt_id")
        patched = []
        for item in execution.get("receipts") or []:
            if item.get("receipt_id") == target:
                item = {**item, "external_receipt_id": receipt_id}
            patched.append(item)
        execution["receipts"] = patched
        if (execution.get("latest_receipt") or {}).get("receipt_id") == target:
            execution["latest_receipt"] = {**execution["latest_receipt"], "external_receipt_id": receipt_id}
        self._store_execution(execution)
        return {**receipt, "external_receipt_id": receipt_id}

    # ------------------------------------------------------------------
    # 编排计划同步
    # ------------------------------------------------------------------

    def _plans_for_task(self, task_id: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for plan in store.rows(PLAN_MODULE):
            if task_id in self.plan_task_ids(plan):
                result.append(plan)
        return result

    def plan_task_ids(self, plan: dict[str, Any]) -> list[int]:
        """从计划中读取关联的批处理任务 id；兼容旧字段和旧文本写法。"""
        ids: list[int] = []

        def add(raw: Any) -> None:
            if isinstance(raw, bool):
                return
            if isinstance(raw, int):
                if raw > 0:
                    ids.append(raw)
                return
            if isinstance(raw, dict):
                for candidate in (raw.get("task_id"), raw.get("id"), raw.get("任务ID")):
                    if candidate is not None:
                        add(candidate)
                return
            if isinstance(raw, str) and raw.strip().isdigit():
                value = int(raw.strip())
                if value > 0:
                    ids.append(value)

        for key in ("batch_task_ids", "关联批处理任务", "关联任务"):
            raw = plan.get(key)
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    add(item)
            else:
                add(raw)

        for key in ("任务列表", "task_list"):
            raw = plan.get(key)
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    add(item)
            elif isinstance(raw, str):
                # 旧计划常用 "1,2,3" 或 "batch:1;batch:2" 记任务，按显式前缀解析；
                # 纯描述文本（如"编排调度样例1"）没有明确的 id 前缀，保持原样不猜测。
                for match in _PLAN_TASK_REFERENCE.finditer(raw):
                    value = int(match.group(1))
                    if value > 0:
                        ids.append(value)
        return sorted(set(ids))

    def _sync_plans(
        self, task_id: int, execution: dict[str, Any], receipt: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """把同一执行记录的终态同步到引用它的编排计划。

        计划自身的生命周期状态（草稿/待校验/运行中/已停止）是旧动作语义，
        不在这里改写；计划通过 ``execution_status``、``final_status`` 和
        ``batch_executions`` 看到与批处理完全相同的终态。
        """
        plans = self._plans_for_task(task_id)
        for plan in plans:
            plan["batch_execution"] = self._plan_execution_view_of(execution)
            plan["last_receipt"] = receipt
            plan["最后回执"] = receipt
            self._refresh_plan_execution(plan)
        return plans

    def _plan_execution_view_of(self, execution: dict[str, Any]) -> dict[str, Any]:
        latest = execution.get("latest_receipt") or {}
        return {
            "task_id": int(execution.get("task_id", 0) or 0),
            "task_no": execution.get("task_no"),
            "execution_id": execution.get("execution_id"),
            "status": execution.get("status_label") or STATUS_LABELS.get(str(execution.get("status_code")), ""),
            "status_code": execution.get("status_code"),
            "最终状态": execution.get("status_label") if execution.get("terminal") else None,
            "trigger_source": execution.get("trigger_source"),
            "source": execution.get("source"),
            "last_receipt": latest,
            "receipts": list(execution.get("receipts") or []),
        }

    def _refresh_plan_execution(self, plan: dict[str, Any]) -> None:
        ids = self.plan_task_ids(plan)
        if not ids:
            plan["execution_status"] = None
            plan["执行状态"] = None
            plan["final_status"] = None
            plan["最终状态"] = None
            plan["terminal"] = str(plan.get("status") or "") == "已停止"
            return
        executions = [self.ensure_for_task(task_id) for task_id in ids]
        status_codes = [str(item.get("status_code") or "") for item in executions]
        terminal = all(code in TERMINAL_STATUSES for code in status_codes)
        if terminal:
            final = "failed" if "failed" in status_codes else "succeeded"
            final_label = STATUS_LABELS[final]
        else:
            final = None
            final_label = None
        current_label = STATUS_LABELS.get(status_codes[0], status_codes[0]) if status_codes else None
        plan["execution_status"] = current_label
        plan["执行状态"] = current_label
        plan["final_status"] = final_label
        plan["最终状态"] = final_label
        # 计划自身的待处理口径仍按计划生命周期走；执行终态另由 final_status 表达。
        plan["terminal"] = terminal or str(plan.get("status") or "") == "已停止"
        plan["pending"] = str(plan.get("status") or "") != "已停止"
        plan["batch_executions"] = {
            str(item.get("task_id")): self._plan_execution_view_of(item) for item in executions
        }
        plan["batch_execution"] = self._plan_execution_view_of(executions[0]) if executions else None
        plan["batch_task_ids"] = ids
        plan["trigger_source"] = executions[0].get("trigger_source") if executions else None
        plan["触发来源"] = plan["trigger_source"]

    def plan_execution_summaries(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """读取计划关联的批处理执行记录，不主动改写计划状态。"""
        return [self.ensure_for_task(task_id) for task_id in self.plan_task_ids(plan)]

    def sync_plan_view(self, plan_id: int) -> dict[str, Any] | None:
        plan = store.find(PLAN_MODULE, plan_id)
        if plan is None:
            return None
        self._refresh_plan_execution(plan)
        return plan


    # ------------------------------------------------------------------
    # 调度器入口
    # ------------------------------------------------------------------

    def timeout_from_scheduler(
        self,
        task_id: int,
        *,
        plan_id: int | None = None,
        idempotency_key: str | None = None,
        message: str | None = None,
        reason: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool, list[dict[str, Any]]]:
        """调度器发起超时结束：与页面终止、回执失败写同一个执行记录。"""
        execution, receipt, idempotent, plans = self.apply_action(
            task_id,
            "超时结束",
            source="scheduler",
            idempotency_key=idempotency_key,
            message=message or "调度器判超时，批处理任务已结束",
            extra={"plan_id": plan_id, "reason": reason or "timeout"},
        )
        if plan_id is not None:
            plan = store.find(PLAN_MODULE, plan_id)
            if plan is not None:
                plan.setdefault("batch_task_ids", [])
                if task_id not in self.plan_task_ids(plan):
                    plan["batch_task_ids"] = list(plan.get("batch_task_ids") or []) + [task_id]
                self._sync_plans(task_id, execution, receipt)
        return execution, receipt, idempotent, plans


execution_service = BatchExecutionService()

__all__ = [
    "ACTION_CONTRACTS",
    "BatchExecutionError",
    "BatchExecutionService",
    "EXECUTION_MODULE",
    "execution_service",
]
