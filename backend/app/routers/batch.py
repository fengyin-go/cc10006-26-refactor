"""批量转换接口：页面动作、调度器动作与批处理回执都落到同一执行记录。

旧接口保持原路径和原返回字段：
- ``GET /api/batch``、``GET /api/batch/export`` 仍返回中文任务字段；
- ``POST /api/batch/{id}/actions`` 既接受 ``{values: {action}}``，也接受
  ``{action, source, idempotency_key}``；
- 返回值在 ``entry`` 之外补充 ``execution`` 和 ``receipt``。

新增接口：
- ``GET /api/batch/{id}/execution``：读取任务的执行记录；
- ``GET /api/batch/executions``：列出全部执行记录；
- ``POST /api/batch/receipts``：批处理回执写入完成/失败；
- ``POST /api/batch/{id}/timeout``：调度器超时结束；
- ``GET /api/batch/actions``：动作契约与状态映射。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    BatchActionPayload,
    BatchReceiptPayload,
    BatchTimeoutPayload,
    EntryPayload,
    PageResult,
)
from app.services.batch import BatchService
from app.services.batch_execution import BatchExecutionError, execution_service
from app.services.batch_models import (
    STATUS_LABELS,
    action_contracts,
    normalize_source,
)

router = APIRouter(prefix="/api/batch", tags=["批量转换"])

service = BatchService()

LIST_FIELDS = ["任务编号", "输入路径", "处理脚本", "输出路径", "分区覆盖", "依赖上游", "重试策略", "超时终止"]
STATUSES = list(STATUS_LABELS.values())


def _action_result(
    execution: dict[str, Any],
    receipt: dict[str, Any],
    *,
    idempotent: bool,
    message: str | None = None,
) -> ActionResult:
    entry = service.get_entry(int(execution.get("task_id", 0) or 0))
    status = str(execution.get("status_label") or STATUS_LABELS.get(str(execution.get("status_code")), ""))
    return ActionResult(
        ok=True,
        message=message or str(receipt.get("message") or f"批处理任务已{receipt.get('action') or ''}"),
        entry=entry,
        execution=execution,
        receipt=receipt,
        status=status,
        final_status=status if execution.get("terminal") else None,
        trigger_source=receipt.get("trigger_source"),
        idempotent=idempotent,
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、待调度、运行中、已完成、已失败"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤批量转换列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 注意：固定路径必须放在 /{entry_id} 之前，避免被当成 id 解析。
@router.get("/export")
def export_entries(
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """导出批量转换清单：保留原中文字段，同时带出执行状态与回执。"""
    items, total = service.list_entries(keyword=keyword, status=status, page=1, size=10000)
    return {"module": "batch", "total": total, "items": items}


@router.get("/actions")
def list_actions() -> dict[str, Any]:
    """动作契约：页面、调度器和回执写入共用同一份动作定义。"""
    return {
        "module": "batch",
        "statuses": STATUS_LABELS,
        "status_order": list(STATUS_LABELS.values()),
        "actions": action_contracts(),
    }


@router.get("/executions")
def list_executions() -> dict[str, Any]:
    """列出批处理执行记录，供页面和调度侧核对终态。"""
    executions = execution_service.list_executions()
    return {"module": "batch", "total": len(executions), "items": executions}


@router.get("/executions/{execution_id}")
def get_execution_by_id(execution_id: str) -> dict[str, Any]:
    execution = execution_service.find_by_execution_id(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"执行记录 {execution_id} 不存在")
    return execution


@router.post("/receipts", response_model=ActionResult)
def write_receipt(payload: BatchReceiptPayload) -> ActionResult:
    """批处理回执写入完成或失败；重复回执按幂等键返回同一条记录。"""
    data = payload.merged()
    source_label, source_code = normalize_source(data.source or "batch", default="batch")
    try:
        execution, receipt, idempotent, _plans = execution_service.apply_receipt(
            task_id=data.task_id,
            task_no=data.task_no,
            execution_id=data.execution_id,
            action=data.action,
            status=data.status,
            source=source_code,
            idempotency_key=data.idempotency_key,
            receipt_id=data.receipt_id,
            message=data.message,
            detail=data.detail,
        )
    except BatchExecutionError as error:
        return ActionResult(
            ok=False,
            message=error.message,
            entry=error.entry,
            receipt=error.receipt,
            trigger_source=source_label,
        )
    return _action_result(execution, receipt, idempotent=idempotent)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: BatchActionPayload) -> ActionResult:
    """对单条批处理任务执行动作；终态不可跳级覆盖，重复请求幂等返回。"""
    action = payload.resolved_action()
    source_label, source_code = normalize_source(payload.resolved_source(), default="page")
    try:
        execution, receipt, idempotent, _plans = execution_service.apply_action(
            entry_id,
            action,
            source=source_code,
            idempotency_key=payload.resolved_idempotency_key(),
        )
    except BatchExecutionError as error:
        return ActionResult(
            ok=False,
            message=error.message,
            entry=error.entry,
            receipt=error.receipt,
            trigger_source=source_label,
        )
    return _action_result(execution, receipt, idempotent=idempotent)


@router.post("/{entry_id}/timeout", response_model=ActionResult)
def timeout_entry(entry_id: int, payload: BatchTimeoutPayload) -> ActionResult:
    """调度器发起超时结束：与页面终止写同一个执行记录和同一种终态。"""
    try:
        execution, receipt, idempotent, _plans = execution_service.timeout_from_scheduler(
            entry_id,
            plan_id=payload.plan_id,
            idempotency_key=payload.idempotency_key or payload.values.get("idempotency_key"),
            message=payload.message,
            reason=payload.reason,
        )
    except BatchExecutionError as error:
        return ActionResult(
            ok=False,
            message=error.message,
            entry=error.entry,
            receipt=error.receipt,
            trigger_source="调度器",
        )
    return _action_result(execution, receipt, idempotent=idempotent)


@router.get("/{entry_id}/execution", response_model=dict)
def get_execution(entry_id: int) -> dict:
    """读取任务对应的唯一执行记录，包含完整回执链。"""
    try:
        return execution_service.get_execution(entry_id)
    except BatchExecutionError as error:
        raise HTTPException(status_code=404, detail=error.message) from error


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条批处理任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"批处理任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条批处理任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="批处理任务已登记", entry=entry, status=entry.get("status"))
