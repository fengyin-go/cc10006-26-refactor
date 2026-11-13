"""批处理任务共用模型：执行状态、动作契约、任务定义与执行回执。

这个模块只描述"同一份执行数据长什么样"，不负责落库和状态推进。批量转换页面、
编排调度器以及批处理回执写入都从这里取同一套状态、动作和回执字段：

- 规范状态使用英文 code（draft/scheduled/running/succeeded/failed）；
- 对外的 ``status`` 仍输出中文标签（草稿/待调度/运行中/已完成/已失败），
  保证旧页面、旧查询和旧导出字段可读；
- 旧动作（提交调度/启动任务/终止任务/归档结果）保留原目标状态语义；
- 回执同时提供规范字段与中文别名，状态和回执可以逐字段对齐。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4


def clone_json(value: Any) -> Any:
    """复制一份纯 JSON 结构，切断回执/执行记录之间的对象复用。

    FastAPI 的序列化器会把同一对象在多处出现识别成循环引用，执行记录同时暴露
    ``receipts`` 和 ``latest_receipt`` 时必须在输出边界断开别名。
    """
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# 执行状态
# ---------------------------------------------------------------------------

STATUS_LABELS: dict[str, str] = {
    "draft": "草稿",
    "scheduled": "待调度",
    "running": "运行中",
    "succeeded": "已完成",
    "failed": "已失败",
}

TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
NEGATIVE_STATUSES = frozenset({"failed"})

LEGACY_STATUS_TO_CODE: dict[str, str] = {
    label: code for code, label in STATUS_LABELS.items()
}

_STATUS_ALIASES: dict[str, str] = {
    **{code: code for code in STATUS_LABELS},
    **LEGACY_STATUS_TO_CODE,
    "queued": "scheduled",
    "pending": "scheduled",
    "in_progress": "running",
    "processing": "running",
    "success": "succeeded",
    "completed": "succeeded",
    "complete": "succeeded",
    "failure": "failed",
    "error": "failed",
    "terminated": "failed",
    "timed_out": "failed",
    "timeout": "failed",
}


def utc_now() -> str:
    """统一时间写法，便于审计和回执排序。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_status(value: Any) -> str | None:
    """把中文状态、英文 code 或历史别名统一成规范状态 code。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _STATUS_ALIASES.get(text) or _STATUS_ALIASES.get(text.lower())


def status_label(value: Any) -> str | None:
    """把任意状态值转成页面可读的中文标签。"""
    code = normalize_status(value)
    if code is not None:
        return STATUS_LABELS[code]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# 触发来源
# ---------------------------------------------------------------------------

SOURCE_LABELS: dict[str, str] = {
    "page": "页面",
    "scheduler": "调度器",
    "batch": "批处理",
    "system": "系统",
}

_SOURCE_ALIASES: dict[str, str] = {
    "页面": "page",
    "前端": "page",
    "page": "page",
    "ui": "page",
    "web": "page",
    "manual": "page",
    "调度器": "scheduler",
    "调度": "scheduler",
    "编排调度": "scheduler",
    "scheduler": "scheduler",
    "schedule": "scheduler",
    "系统": "system",
    "system": "system",
    "批处理": "batch",
    "批处理回执": "batch",
    "执行器": "batch",
    "回执": "batch",
    "batch": "batch",
    "executor": "batch",
    "worker": "batch",
    "receipt": "batch",
}


def normalize_source(value: Any, default: str = "page") -> tuple[str, str]:
    """返回 (中文来源标签, 规范来源 code)。"""
    default_code = _SOURCE_ALIASES.get(str(default).strip().lower(), "page")
    if value is None or not str(value).strip():
        return SOURCE_LABELS.get(default_code, default_code), default_code
    text = str(value).strip()
    code = _SOURCE_ALIASES.get(text) or _SOURCE_ALIASES.get(text.lower())
    if code is None:
        return text, text.lower().replace(" ", "_")
    return SOURCE_LABELS[code], code


# ---------------------------------------------------------------------------
# 动作契约
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionContract:
    """一个批处理动作的完整契约。

    ``allowed_from`` 只限制起点状态，不改变旧动作的目标状态，因此旧记录中的
    动作含义保持不变；终态保护由 ``apply_action`` 统一处理。
    """

    code: str
    label: str
    target_status: str
    allowed_from: frozenset[str]
    default_source: str
    legacy: bool = True
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        target_label = STATUS_LABELS[self.target_status]
        return {
            "action": self.label,
            "action_code": self.code,
            "动作": self.label,
            "action_label": self.label,
            "target_status": self.target_status,
            "target_status_label": target_label,
            "目标状态": target_label,
            "allowed_from": sorted(self.allowed_from),
            "allowed_from_labels": [STATUS_LABELS[code] for code in sorted(self.allowed_from)],
            "允许起点状态": [STATUS_LABELS[code] for code in sorted(self.allowed_from)],
            "default_source": self.default_source,
            "default_source_label": SOURCE_LABELS.get(self.default_source, self.default_source),
            "legacy": self.legacy,
            "description": self.description,
        }


ACTION_CONTRACTS: tuple[ActionContract, ...] = (
    ActionContract(
        code="submit",
        label="提交调度",
        target_status="scheduled",
        # 旧动作对非终态均可执行，保留原语义；终态由执行服务统一拦截。
        allowed_from=frozenset({"draft", "scheduled", "running"}),
        default_source="page",
        legacy=True,
        description="把批处理任务提交到待调度状态。",
    ),
    ActionContract(
        code="start",
        label="启动任务",
        target_status="running",
        allowed_from=frozenset({"draft", "scheduled", "running"}),
        default_source="page",
        legacy=True,
        description="启动批处理任务，进入运行中状态。",
    ),
    ActionContract(
        code="terminate",
        label="终止任务",
        target_status="failed",
        allowed_from=frozenset({"draft", "scheduled", "running"}),
        default_source="page",
        legacy=True,
        description="页面发起终止，执行记录进入已失败终态。",
    ),
    ActionContract(
        code="archive",
        label="归档结果",
        target_status="succeeded",
        allowed_from=frozenset({"draft", "scheduled", "running"}),
        default_source="page",
        legacy=True,
        description="页面归档结果，执行记录进入已完成终态。",
    ),
    ActionContract(
        code="timeout",
        label="超时结束",
        target_status="failed",
        allowed_from=frozenset({"scheduled", "running"}),
        default_source="scheduler",
        legacy=False,
        description="调度器发现超时，结束同一条执行记录。",
    ),
    ActionContract(
        code="complete",
        label="写入完成",
        target_status="succeeded",
        allowed_from=frozenset({"draft", "scheduled", "running"}),
        default_source="batch",
        legacy=False,
        description="批处理回执写入完成结果。",
    ),
    ActionContract(
        code="fail",
        label="写入失败",
        target_status="failed",
        allowed_from=frozenset({"draft", "scheduled", "running"}),
        default_source="batch",
        legacy=False,
        description="批处理回执写入失败结果。",
    ),
)

_ACTION_BY_LABEL: dict[str, ActionContract] = {item.label: item for item in ACTION_CONTRACTS}
_ACTION_BY_CODE: dict[str, ActionContract] = {item.code: item for item in ACTION_CONTRACTS}

_ACTION_ALIASES: dict[str, str] = {
    "提交调度": "提交调度",
    "submit": "提交调度",
    "schedule": "提交调度",
    "启动任务": "启动任务",
    "启动": "启动任务",
    "start": "启动任务",
    "run": "启动任务",
    "终止任务": "终止任务",
    "终止": "终止任务",
    "停止任务": "终止任务",
    "terminate": "终止任务",
    "abort": "终止任务",
    "cancel": "终止任务",
    "归档结果": "归档结果",
    "归档": "归档结果",
    "archive": "归档结果",
    "超时结束": "超时结束",
    "超时终止": "超时结束",
    "超时": "超时结束",
    "timeout": "超时结束",
    "timed_out": "超时结束",
    "写入完成": "写入完成",
    "完成": "写入完成",
    "执行完成": "写入完成",
    "任务完成": "写入完成",
    "批次完成": "写入完成",
    "complete": "写入完成",
    "completed": "写入完成",
    "success": "写入完成",
    "succeeded": "写入完成",
    "写入失败": "写入失败",
    "执行失败": "写入失败",
    "失败": "写入失败",
    "fail": "写入失败",
    "failed": "写入失败",
    "error": "写入失败",
}


def resolve_action(action: Any) -> ActionContract | None:
    """按中文动作、英文 code 或常见别名解析动作契约。"""
    if action is None:
        return None
    text = str(action).strip()
    if not text:
        return None
    label = _ACTION_ALIASES.get(text) or _ACTION_ALIASES.get(text.lower())
    if label is not None:
        return _ACTION_BY_LABEL.get(label)
    return _ACTION_BY_CODE.get(text) or _ACTION_BY_CODE.get(text.lower())


def action_contracts() -> list[dict[str, Any]]:
    return [item.to_dict() for item in ACTION_CONTRACTS]


# ---------------------------------------------------------------------------
# 任务定义
# ---------------------------------------------------------------------------

TASK_FIELD_MAP: dict[str, str] = {
    "task_no": "任务编号",
    "input_path": "输入路径",
    "processing_script": "处理脚本",
    "output_path": "输出路径",
    "partition_override": "分区覆盖",
    "upstream_dependencies": "依赖上游",
    "retry_policy": "重试策略",
    "timeout_terminate": "超时终止",
}

# 任务行/任务定义里由执行记录维护的字段，不参与 "extra" 透传，
# 否则执行视图写回任务行后会被当成原任务字段无限嵌套。
_EXECUTION_METADATA_KEYS: frozenset[str] = frozenset({
    "id", "status", "status_code", "execution_status", "执行状态", "pending", "abnormal",
    "execution", "execution_id", "执行编号", "task_definition", "任务定义",
    "receipts", "回执列表", "receipt", "回执", "last_receipt", "最后回执",
    "latest_receipt", "terminal_status", "终态", "是否终态", "final_status", "最终状态",
    "terminal", "revision", "序号", "trigger_source", "触发来源", "source", "source_code",
    "created_at", "updated_at", "创建时间", "更新时间", "started_at", "finished_at",
    "开始时间", "结束时间", "last_action", "最后动作", "last_receipt_id", "最后回执编号",
    "legacy_status", "state",
})


@dataclass(frozen=True)
class BatchTaskDefinition:
    """批处理任务定义：处理脚本、依赖上游等字段只在这里解释一次。"""

    task_id: int
    task_no: str = ""
    input_path: str = ""
    processing_script: str = ""
    output_path: str = ""
    partition_override: str = ""
    upstream_dependencies: str = ""
    retry_policy: str = ""
    timeout_terminate: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "BatchTaskDefinition":
        def value(key: str) -> str:
            raw = row.get(TASK_FIELD_MAP[key])
            return "" if raw is None else str(raw)

        known_chinese = set(TASK_FIELD_MAP.values())
        extra = {
            key: value
            for key, value in row.items()
            if key not in known_chinese and key not in _EXECUTION_METADATA_KEYS
        }
        return cls(
            task_id=int(row.get("id", 0) or 0),
            task_no=value("task_no"),
            input_path=value("input_path"),
            processing_script=value("processing_script"),
            output_path=value("output_path"),
            partition_override=value("partition_override"),
            upstream_dependencies=value("upstream_dependencies"),
            retry_policy=value("retry_policy"),
            timeout_terminate=value("timeout_terminate"),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """同时输出规范字段和原中文字段，旧页面/导出无需改字段名。"""
        return {
            "task_id": self.task_id,
            "task_no": self.task_no,
            "input_path": self.input_path,
            "processing_script": self.processing_script,
            "output_path": self.output_path,
            "partition_override": self.partition_override,
            "upstream_dependencies": self.upstream_dependencies,
            "retry_policy": self.retry_policy,
            "timeout_terminate": self.timeout_terminate,
            "任务编号": self.task_no,
            "输入路径": self.input_path,
            "处理脚本": self.processing_script,
            "输出路径": self.output_path,
            "分区覆盖": self.partition_override,
            "依赖上游": self.upstream_dependencies,
            "重试策略": self.retry_policy,
            "超时终止": self.timeout_terminate,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# 执行回执
# ---------------------------------------------------------------------------

@dataclass
class ExecutionReceipt:
    """执行回执：三条路径写出来的都是这一个模型。"""

    receipt_id: str
    execution_id: str
    task_id: int
    task_no: str
    action: str
    action_label: str
    status: str
    trigger_source: str
    source: str
    message: str
    created_at: str
    revision: int
    idempotency_key: str | None = None
    valid: bool = True
    terminal: bool = False
    task_snapshot: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    audit_id: int | None = None
    external_receipt_id: str | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @classmethod
    def create(
        cls,
        *,
        execution_id: str,
        task_id: int,
        task_no: str,
        action_label: str,
        action_code: str,
        status: str,
        trigger_source: str,
        source: str,
        message: str,
        revision: int,
        created_at: str | None = None,
        idempotency_key: str | None = None,
        task_snapshot: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        audit_id: int | None = None,
        external_receipt_id: str | None = None,
    ) -> "ExecutionReceipt":
        return cls(
            receipt_id=f"rcp-{uuid4().hex[:16]}",
            execution_id=execution_id,
            task_id=task_id,
            task_no=task_no,
            action=action_code,
            action_label=action_label,
            status=status,
            trigger_source=trigger_source,
            source=source,
            message=message,
            created_at=created_at or utc_now(),
            revision=revision,
            idempotency_key=idempotency_key,
            valid=True,
            terminal=status in TERMINAL_STATUSES,
            task_snapshot=task_snapshot or {},
            detail=detail or {},
            audit_id=audit_id,
            external_receipt_id=external_receipt_id,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionReceipt":
        status = normalize_status(data.get("status_code") or data.get("state") or data.get("status")) or "draft"
        source_code = str(data.get("source_code") or data.get("source") or "page")
        source_label = str(data.get("trigger_source") or data.get("触发来源") or SOURCE_LABELS.get(source_code, source_code))
        action_code = str(data.get("action_code") or data.get("action") or "")
        action_label = str(data.get("action_label") or data.get("动作") or action_code)
        return cls(
            receipt_id=str(data.get("receipt_id") or data.get("回执编号") or f"rcp-{uuid4().hex[:16]}"),
            execution_id=str(data.get("execution_id") or data.get("执行编号") or ""),
            task_id=int(data.get("task_id") or data.get("任务ID") or 0),
            task_no=str(data.get("task_no") or data.get("任务编号") or ""),
            action=action_code,
            action_label=action_label,
            status=status,
            trigger_source=source_label,
            source=source_code,
            message=str(data.get("message") or data.get("消息") or ""),
            created_at=str(data.get("created_at") or data.get("时间") or utc_now()),
            revision=int(data.get("revision") or data.get("序号") or 0),
            idempotency_key=data.get("idempotency_key"),
            valid=bool(data.get("valid", True)),
            terminal=bool(data.get("terminal", status in TERMINAL_STATUSES)),
            task_snapshot=dict(data.get("task_snapshot") or data.get("任务快照") or {}),
            detail=dict(data.get("detail") or {}),
            audit_id=data.get("audit_id"),
            external_receipt_id=data.get("external_receipt_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        status = STATUS_LABELS.get(self.status, self.status)
        snapshot = clone_json(self.task_snapshot)
        return {
            # 规范字段
            "receipt_id": self.receipt_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "task_no": self.task_no,
            "action": self.action_label,
            "action_code": self.action,
            "status": status,
            "status_code": self.status,
            "state": self.status,
            "status_label": status,
            "trigger_source": self.trigger_source,
            "source": self.source,
            "source_code": self.source,
            "message": self.message,
            "created_at": self.created_at,
            "revision": self.revision,
            "idempotency_key": self.idempotency_key,
            "valid": self.valid,
            "terminal": self.terminal,
            "audit_id": self.audit_id,
            "external_receipt_id": self.external_receipt_id,
            "task_snapshot": snapshot,
            "detail": dict(self.detail),
            # 中文/旧字段别名
            "回执编号": self.receipt_id,
            "执行编号": self.execution_id,
            "任务ID": self.task_id,
            "任务编号": self.task_no,
            "动作": self.action_label,
            "最终状态": status,
            "执行状态": status,
            "触发来源": self.trigger_source,
            "来源": self.trigger_source,
            "消息": self.message,
            "时间": self.created_at,
            "序号": self.revision,
            "任务快照": snapshot,
            "处理脚本": snapshot.get("处理脚本") or snapshot.get("processing_script") or "",
            "依赖上游": snapshot.get("依赖上游") or snapshot.get("upstream_dependencies") or "",
        }


# ---------------------------------------------------------------------------
# 执行记录
# ---------------------------------------------------------------------------

@dataclass
class BatchExecution:
    """一条批处理任务对应的唯一执行记录。"""

    execution_id: str
    task_id: int
    task_no: str
    status: str
    created_at: str
    updated_at: str
    revision: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    trigger_source: str | None = None
    source: str | None = None
    last_action: str | None = None
    last_receipt_id: str | None = None
    task_definition: dict[str, Any] = field(default_factory=dict)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    legacy_status: str | None = None

    @classmethod
    def create(cls, task_id: int, task_no: str, task_definition: dict[str, Any]) -> "BatchExecution":
        now = utc_now()
        return cls(
            execution_id=f"exec-{task_id}-{uuid4().hex[:8]}",
            task_id=task_id,
            task_no=task_no,
            status="draft",
            created_at=now,
            updated_at=now,
            task_definition=task_definition,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, fallback_task_id: int = 0, fallback_task_no: str = "") -> "BatchExecution":
        raw_status = data.get("status_code") or data.get("state") or data.get("status") or data.get("执行状态")
        code = normalize_status(raw_status)
        legacy_status = None if code else status_label(raw_status)
        receipts_raw = data.get("receipts") or data.get("回执列表") or data.get("执行回执") or []
        receipts = [ExecutionReceipt.from_dict(item).to_dict() for item in receipts_raw if isinstance(item, Mapping)]
        return cls(
            execution_id=str(data.get("execution_id") or data.get("执行编号") or f"exec-{fallback_task_id}"),
            task_id=int(data.get("task_id") or data.get("任务ID") or fallback_task_id or 0),
            task_no=str(data.get("task_no") or data.get("任务编号") or fallback_task_no or ""),
            status=code or "draft",
            created_at=str(data.get("created_at") or data.get("创建时间") or utc_now()),
            updated_at=str(data.get("updated_at") or data.get("更新时间") or utc_now()),
            revision=int(data.get("revision") or data.get("序号") or len(receipts)),
            started_at=data.get("started_at") or data.get("开始时间"),
            finished_at=data.get("finished_at") or data.get("结束时间"),
            trigger_source=data.get("trigger_source") or data.get("触发来源"),
            source=data.get("source_code") or data.get("source"),
            last_action=data.get("last_action") or data.get("最后动作"),
            last_receipt_id=data.get("last_receipt_id") or data.get("最后回执编号"),
            task_definition=dict(data.get("task_definition") or data.get("任务定义") or {}),
            receipts=receipts,
            legacy_status=legacy_status or data.get("legacy_status"),
        )

    @property
    def status_label(self) -> str:
        # 没有回执的旧记录保留原状态文案；一旦有回执，以回执的规范状态为准。
        if self.legacy_status and not self.receipts:
            return self.legacy_status
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def latest_receipt(self) -> dict[str, Any] | None:
        return self.receipts[-1] if self.receipts else None

    def to_record(self) -> dict[str, Any]:
        """持久化用的规范结构，不带中文字段别名。"""
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "task_no": self.task_no,
            "status_code": self.status,
            "status_label": self.status_label,
            "legacy_status": self.legacy_status,
            "revision": self.revision,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "trigger_source": self.trigger_source,
            "source_code": self.source,
            "last_action": self.last_action,
            "last_receipt_id": self.last_receipt_id,
            "task_definition": clone_json(self.task_definition),
            "receipts": clone_json(self.receipts),
        }

    def to_dict(self) -> dict[str, Any]:
        label = self.status_label
        receipts = clone_json(self.receipts)
        latest = clone_json(receipts[-1]) if receipts else None
        task_definition = clone_json(self.task_definition)
        return {
            # 规范字段
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "task_no": self.task_no,
            "status": label,
            "status_code": self.status,
            "state": self.status,
            "status_label": label,
            "legacy_status": self.legacy_status,
            "terminal": self.terminal,
            "pending": not self.terminal,
            "revision": self.revision,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "trigger_source": self.trigger_source,
            "source": self.source,
            "source_code": self.source,
            "last_action": self.last_action,
            "last_receipt_id": self.last_receipt_id,
            "task_definition": task_definition,
            "receipts": receipts,
            "latest_receipt": latest,
            # 中文/旧字段别名
            "执行编号": self.execution_id,
            "任务ID": self.task_id,
            "任务编号": self.task_no,
            "执行状态": label,
            "状态": label,
            "终态": label if self.terminal else None,
            "是否终态": self.terminal,
            "序号": self.revision,
            "开始时间": self.started_at,
            "结束时间": self.finished_at,
            "创建时间": self.created_at,
            "更新时间": self.updated_at,
            "触发来源": self.trigger_source,
            "最后动作": self.last_action,
            "最后回执编号": self.last_receipt_id,
            "任务定义": clone_json(task_definition),
            "回执列表": clone_json(receipts),
            "最后回执": clone_json(latest) if latest else None,
        }


# ---------------------------------------------------------------------------
# 旧记录兼容
# ---------------------------------------------------------------------------

def _existing_receipts(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("receipts") or row.get("回执列表") or row.get("执行回执") or []
    if not isinstance(raw, list):
        return []
    return [ExecutionReceipt.from_dict(item).to_dict() for item in raw if isinstance(item, Mapping)]


def new_execution_record(row: Mapping[str, Any], definition: BatchTaskDefinition) -> dict[str, Any]:
    """为一条任务创建执行记录，同时写入可被旧查询直接读取的别名。"""
    raw_status = row.get("status_code") or row.get("执行状态") or row.get("status")
    code = normalize_status(raw_status) or "draft"
    now = utc_now()
    execution = BatchExecution(
        execution_id=str(row.get("execution_id") or row.get("执行编号") or f"exec-{int(row.get('id', 0) or 0)}"),
        task_id=int(row.get("id", 0) or 0),
        task_no=definition.task_no,
        status=code,
        created_at=str(row.get("created_at") or row.get("创建时间") or now),
        updated_at=str(row.get("updated_at") or row.get("更新时间") or now),
        revision=0,
        task_definition=definition.to_dict(),
        receipts=_existing_receipts(row),
        legacy_status=None if normalize_status(raw_status) else status_label(raw_status),
    )
    if execution.receipts:
        execution.revision = len(execution.receipts)
        last = execution.receipts[-1]
        execution.last_receipt_id = str(last.get("receipt_id") or "")
        execution.last_action = str(last.get("action") or "")
        execution.status = normalize_status(last.get("status_code") or last.get("status")) or execution.status
        execution.trigger_source = last.get("trigger_source")
        execution.source = last.get("source_code") or last.get("source")
    if execution.status in TERMINAL_STATUSES:
        execution.finished_at = execution.updated_at
    elif execution.status == "running":
        execution.started_at = execution.started_at or execution.updated_at
    return execution.to_dict()


def ensure_execution(row: Mapping[str, Any]) -> dict[str, Any]:
    """读取或补齐执行记录；不覆盖旧动作和旧状态字段。"""
    definition = BatchTaskDefinition.from_row(row)
    current = row.get("execution")
    if isinstance(current, Mapping):
        execution = BatchExecution.from_dict(current, fallback_task_id=int(row.get("id", 0) or 0), fallback_task_no=definition.task_no)
        execution.task_definition = definition.to_dict()
        if not execution.receipts:
            execution.receipts = _existing_receipts(row)
            execution.revision = max(execution.revision, len(execution.receipts))
        view = execution.to_dict()
    else:
        view = new_execution_record(row, definition)
    return view


def apply_execution_view(row: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    """把执行记录同步到任务行，保留原中文字段并补充执行字段。

    没有回执的旧记录保留原来的 ``abnormal``/``pending`` 标记，避免因为补齐
    执行记录而改写旧页面的异常统计口径。
    """
    label = str(execution.get("status_label") or STATUS_LABELS.get(str(execution.get("status_code")), ""))
    code = str(execution.get("status_code") or "")
    terminal = code in TERMINAL_STATUSES
    latest = execution.get("latest_receipt")
    has_receipts = bool(execution.get("receipts"))
    row["execution_id"] = execution.get("execution_id")
    # 输出边界复制一份，避免 entry 与 execution 指向同一个对象被序列化器判成循环引用。
    row["execution"] = clone_json(execution)
    row["status"] = label or row.get("status")
    row["status_code"] = code or None
    row["execution_status"] = label
    row["执行状态"] = label
    row["trigger_source"] = execution.get("trigger_source")
    row["触发来源"] = execution.get("trigger_source")
    row["terminal_status"] = label if terminal else None
    row["终态"] = label if terminal else None
    row["final_status"] = label if terminal else None
    row["最终状态"] = label if terminal else None
    if not has_receipts and "pending" in row:
        row["pending"] = bool(row.get("pending"))
    else:
        row["pending"] = not terminal
    if code in NEGATIVE_STATUSES:
        row["abnormal"] = True
    elif has_receipts:
        row["abnormal"] = False
    else:
        row["abnormal"] = bool(row.get("abnormal", False))
    row["task_definition"] = clone_json(execution.get("task_definition")) if execution.get("task_definition") else None
    row["任务定义"] = clone_json(row["task_definition"]) if row["task_definition"] else None
    row["receipts"] = clone_json(execution.get("receipts") or [])
    row["回执列表"] = clone_json(row["receipts"])
    row["last_receipt"] = clone_json(latest) if latest else None
    row["最后回执"] = clone_json(row["last_receipt"]) if row["last_receipt"] else None
    return row


