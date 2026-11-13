"""编排调度接口：维护编排计划，覆盖提交校验、启动编排、停止编排等动作。

计划视图除原字段外，还会带出关联批处理任务的执行状态和最终状态；调度器对
超时任务的动作走 ``POST /api/schedule/{plan_id}/timeout``，与页面终止、批处理
回执写入的是同一条执行记录。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, BatchTimeoutPayload, EntryPayload, PageResult
from app.services.batch_execution import BatchExecutionError, execution_service
from app.services.batch_models import status_label
from app.services.schedule import ScheduleService, plan_view

router = APIRouter(prefix="/api/schedule", tags=["编排调度"])

service = ScheduleService()

LIST_FIELDS = ["计划编号", "任务列表", "依赖图定义", "调度表达式", "并发上限", "失败处理", "超时秒数", "通知策略"]
STATUSES = ["草稿", "待校验", "运行中", "已停止"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、运行中、已停止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按计划编号与状态过滤编排调度列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 固定路径放在 /{entry_id} 之前，避免 export 被当作 id 解析。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出编排调度清单：保留原字段，同时带出关联执行状态与最终状态。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "schedule", "total": total, "items": items}


@router.get("/{entry_id}/executions", response_model=dict)
def get_plan_executions(entry_id: int) -> dict[str, Any]:
    """读取计划关联的全部批处理执行记录，与批处理页看到同一份状态和回执。"""
    plan = service.get_entry(entry_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"编排计划 {entry_id} 不存在或已归档")
    executions = execution_service.plan_execution_summaries(plan)
    return {
        "module": "schedule",
        "plan_id": entry_id,
        "task_ids": execution_service.plan_task_ids(plan),
        "execution_status": plan.get("execution_status"),
        "final_status": plan.get("final_status"),
        "items": executions,
    }


@router.post("/{entry_id}/timeout", response_model=ActionResult)
def timeout_plan(entry_id: int, payload: BatchTimeoutPayload) -> ActionResult:
    """调度器发起超时结束：把计划内仍在执行的批处理任务统一推进到同一终态。"""
    plan = service.get_entry(entry_id)
    if plan is None:
        return ActionResult(ok=False, message=f"编排计划 {entry_id} 不存在或已归档", trigger_source="调度器")
    task_ids = execution_service.plan_task_ids(plan)
    if payload.task_id is not None:
        task_ids = [payload.task_id]
    if not task_ids:
        return ActionResult(ok=False, message="编排计划未关联可结束的批处理任务", trigger_source="调度器")

    last_receipt: dict[str, Any] | None = None
    last_execution: dict[str, Any] | None = None
    idempotent_all = True
    errors: list[str] = []
    for task_id in task_ids:
        try:
            execution, receipt, idempotent, _plans = execution_service.timeout_from_scheduler(
                task_id,
                plan_id=entry_id,
                idempotency_key=payload.idempotency_key,
                message=payload.message,
                reason=payload.reason or "plan_timeout",
            )
        except BatchExecutionError as error:
            if "终态" in error.message:
                # 已完成/已失败的任务不会被超时动作覆盖，这是保护而不是失败，
                # 仍然把同一条执行记录的最新状态作为计划终态返回。
                current = execution_service.get_execution(task_id)
                last_execution = current
                last_receipt = current.get("latest_receipt")
                continue
            errors.append(error.message)
            continue
        last_receipt = receipt
        last_execution = execution
        idempotent_all = idempotent_all and idempotent

    updated = service.get_entry(entry_id) or {}
    if errors and last_execution is None:
        return ActionResult(ok=False, message="；".join(errors), entry=updated, trigger_source="调度器")
    final_status = updated.get("final_status") or (last_execution or {}).get("status_label")
    return ActionResult(
        ok=True,
        message=payload.message or f"调度器已结束计划内任务，最终状态：{status_label(final_status)}",
        entry=updated,
        execution=last_execution,
        receipt=last_receipt,
        status=status_label(final_status),
        final_status=status_label(final_status),
        trigger_source="调度器",
        idempotent=idempotent_all,
    )


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条编排计划明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"编排计划 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条编排计划，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    refreshed = plan_view(entry)
    return ActionResult(ok=True, message="编排计划已登记", entry=refreshed)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条编排计划执行提交校验、启动编排、停止编排；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=plan_view(entry))
