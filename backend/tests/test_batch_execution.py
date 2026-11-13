"""批处理执行契约回归测试：三条触发路径必须落在同一条执行记录上。

覆盖：
- 页面终止、调度器超时、批处理回执写入统一状态与回执模型；
- 已完成/已失败不可被新动作跳级覆盖；
- 重复请求幂等；
- 旧记录字段、旧动作语义、旧状态文案保持可读；
- 冲突状态以最新有效回执为准；
- 编排计划看到相同终态，审计记录触发来源和最终状态。
"""
from __future__ import annotations

import unittest

from app.schemas import BatchActionPayload, BatchReceiptPayload, BatchTimeoutPayload
from app.services.audit import AuditService
from app.services.batch import BatchService
from app.services.batch_execution import BatchExecutionError, execution_service
from app.services.batch_models import (
    STATUS_LABELS,
    BatchTaskDefinition,
    normalize_status,
    resolve_action,
)
from app.services.schedule import ScheduleService
from app.store import store


class BatchExecutionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.batch = BatchService()
        self.schedule = ScheduleService()

    def _create_plan(self, task_ids: list[int]) -> dict:
        entry, missing = self.schedule.create_entry(
            {
                "计划编号": "SCHE-TEST",
                "任务列表": [{"task_id": task_id} for task_id in task_ids],
                "依赖图定义": "batch -> gold",
                "batch_task_ids": task_ids,
            }
        )
        self.assertEqual(missing, [])
        return entry

    # ------------------------------------------------------------------
    # 三条路径与同一执行记录
    # ------------------------------------------------------------------

    def test_page_terminate_scheduler_timeout_and_receipt_share_one_execution(self) -> None:
        view, message, receipt = self.batch.run_action_detail(3, "启动任务")
        self.assertEqual(view["status_code"], "running")
        execution_id = view["execution_id"]

        # 调度器超时结束同一条执行记录。
        execution, receipt, idempotent, _ = execution_service.timeout_from_scheduler(3)
        self.assertFalse(idempotent)
        self.assertEqual(execution["execution_id"], execution_id)
        self.assertEqual(execution["status_code"], "failed")
        self.assertEqual(receipt["trigger_source"], "调度器")
        self.assertEqual(receipt["source_code"], "scheduler")

        # 已失败终态不能再被回执写成完成。
        with self.assertRaises(BatchExecutionError) as ctx:
            execution_service.apply_receipt(task_id=3, status="已完成")
        self.assertIn("终态", str(ctx.exception))

    def test_receipt_completion_and_page_terminate_agree_on_status_model(self) -> None:
        execution, receipt, _idempotent, _ = execution_service.apply_receipt(
            task_id=3, status="已完成", idempotency_key="batch-job-1"
        )
        self.assertEqual(execution["status_code"], "succeeded")
        self.assertEqual(receipt["action_code"], "complete")
        self.assertEqual(receipt["status_code"], "succeeded")
        self.assertEqual(receipt["最终状态"], STATUS_LABELS["succeeded"])
        self.assertEqual(receipt["触发来源"], "批处理")

        view, message, page_receipt = self.batch.run_action_detail(2, "终止任务")
        self.assertEqual(view["status_code"], "failed")
        self.assertEqual(page_receipt["source_code"], "page")
        self.assertEqual(page_receipt["status_code"], "failed")

    def test_each_task_has_exactly_one_execution_record(self) -> None:
        self.batch.run_action_detail(1, "提交调度")
        self.batch.run_action_detail(1, "启动任务")
        execution = execution_service.get_execution(1)
        self.assertEqual(
            sum(1 for row in store.executions() if row["task_id"] == 1),
            1,
        )
        self.assertEqual([item["action_code"] for item in execution["receipts"]], ["submit", "start"])

    # ------------------------------------------------------------------
    # 终态保护与幂等
    # ------------------------------------------------------------------

    def test_failed_task_cannot_be_completed(self) -> None:
        self.batch.run_action_detail(2, "终止任务")
        view, message, receipt = self.batch.run_action_detail(2, "归档结果")
        self.assertIsNone(view)
        self.assertIn("终态", message)
        self.assertEqual(self.batch.get_entry(2)["status"], "已失败")

    def test_completed_task_cannot_be_failed_by_timeout(self) -> None:
        execution_service.apply_receipt(task_id=3, status="已完成")
        with self.assertRaises(BatchExecutionError):
            execution_service.timeout_from_scheduler(3)
        self.assertEqual(self.batch.get_entry(3)["status"], "已完成")

    def test_repeated_page_action_is_idempotent(self) -> None:
        _view, _message, first = self.batch.run_action_detail(2, "终止任务")
        _view, message, second = self.batch.run_action_detail(2, "终止任务")
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertIn("已执行过", message)
        self.assertEqual(len(execution_service.get_execution(2)["receipts"]), 1)
        audits = [row for row in store.rows("audit") if row.get("receipt_id") == first["receipt_id"]]
        self.assertEqual(len(audits), 1)

    def test_repeated_receipt_with_idempotency_key_only_writes_once(self) -> None:
        _execution, first, _idem, _ = execution_service.apply_receipt(
            task_id=3, status="已完成", idempotency_key="job-42"
        )
        _execution, second, idempotent, _ = execution_service.apply_receipt(
            task_id=3, status="已完成", idempotency_key="job-42"
        )
        self.assertTrue(idempotent)
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(len(execution_service.get_execution(3)["receipts"]), 1)

    def test_same_terminal_action_from_another_source_is_idempotent_not_override(self) -> None:
        self.batch.run_action_detail(2, "终止任务")
        execution, receipt, idempotent, _ = execution_service.timeout_from_scheduler(2)
        self.assertTrue(idempotent)
        self.assertEqual(execution["status_code"], "failed")
        self.assertEqual(len(execution["receipts"]), 1)

    def test_conflicting_status_follows_latest_valid_receipt(self) -> None:
        execution_service.apply_receipt(task_id=3, status="已完成", idempotency_key="ok-1")
        record = store.execution_for_task(3)
        record["status_code"] = "failed"
        record["status_label"] = "已失败"
        task = store.find("batch", 3)
        task["status"] = "已失败"
        task["status_code"] = "failed"

        execution = execution_service.get_execution(3)
        self.assertEqual(execution["status_code"], "succeeded")
        self.assertEqual(self.batch.get_entry(3)["status"], "已完成")

        # 最新回执被标记无效且记录状态已被改写时，不再以「已完成」为准。
        record["receipts"][-1]["valid"] = False
        record["status_code"] = "failed"
        record["status_label"] = "已失败"
        execution = execution_service.get_execution(3)
        self.assertEqual(execution["status_code"], "failed")

    # ------------------------------------------------------------------
    # 旧记录兼容
    # ------------------------------------------------------------------

    def test_legacy_record_keeps_status_fields_and_action_semantics(self) -> None:
        for row in store.rows("batch"):
            for key in list(row):
                if key in {
                    "status_code", "execution_status", "执行状态", "execution",
                    "execution_id", "receipts", "回执列表", "last_receipt", "最后回执",
                    "task_definition", "任务定义", "terminal_status", "终态",
                }:
                    row.pop(key, None)
        store._executions.clear()
        # 旧记录：草稿，没有回执，原有异常/待处理标记原样保留。
        legacy = self.batch.get_entry(1)
        self.assertEqual(legacy["status"], "草稿")
        self.assertTrue(legacy["pending"])
        self.assertFalse(legacy["abnormal"])

        # 旧动作「归档结果」仍直接落到已完成。
        view, _message, receipt = self.batch.run_action_detail(1, "归档结果")
        self.assertEqual(view["status"], "已完成")
        self.assertEqual(receipt["action_code"], "archive")

    def test_legacy_terminal_record_cannot_be_reopened(self) -> None:
        task = store.find("batch", 2)
        task["status"] = "已失败"
        task["pending"] = False
        task["abnormal"] = True
        store._executions.clear()

        view, _message, receipt = self.batch.run_action_detail(2, "终止任务")
        self.assertTrue(receipt["detail"].get("synthetic"))
        self.assertEqual(view["status"], "已失败")

        view, message, _receipt = self.batch.run_action_detail(2, "归档结果")
        self.assertIsNone(view)
        self.assertIn("终态", message)

    def test_task_definition_and_receipt_reuse_shared_fields(self) -> None:
        definition = BatchTaskDefinition.from_row(store.find("batch", 1))
        self.assertEqual(definition.processing_script, "批量转换样例1")
        self.assertEqual(definition.upstream_dependencies, "批量转换样例1")
        view, _message, receipt = self.batch.run_action_detail(1, "启动任务")
        self.assertEqual(receipt["task_snapshot"]["处理脚本"], "批量转换样例1")
        self.assertEqual(receipt["任务快照"]["依赖上游"], "批量转换样例1")
        for key in ("任务编号", "输入路径", "处理脚本", "输出路径", "依赖上游", "重试策略", "超时终止"):
            self.assertIn(key, view)

    def test_export_keeps_original_fields(self) -> None:
        items, total = self.batch.list_entries(page=1, size=10)
        self.assertEqual(total, 3)
        for item in items:
            for key in ("任务编号", "输入路径", "处理脚本", "输出路径", "分区覆盖", "依赖上游", "重试策略", "超时终止"):
                self.assertIn(key, item)
            self.assertIn("execution_status", item)

    # ------------------------------------------------------------------
    # 编排计划与审计
    # ------------------------------------------------------------------

    def test_plan_sees_same_terminal_state_and_trigger_source(self) -> None:
        plan = self._create_plan([2])
        self.batch.run_action_detail(2, "终止任务")
        refresh = self.schedule.get_entry(plan["id"])
        self.assertEqual(refresh["final_status"], "已失败")
        self.assertEqual(refresh["execution_status"], "已失败")
        self.assertTrue(refresh["terminal"])
        self.assertEqual(refresh["trigger_source"], "页面")
        self.assertEqual(refresh["batch_execution"]["status_code"], "failed")

    def test_scheduler_timeout_from_plan_updates_plan_and_audit(self) -> None:
        plan = self._create_plan([1])
        self.batch.run_action_detail(1, "启动任务")
        execution, receipt, _idem, _plans = execution_service.timeout_from_scheduler(
            1, plan_id=plan["id"], idempotency_key="plan-timeout-1"
        )
        self.assertEqual(execution["status_code"], "failed")
        refresh = self.schedule.get_entry(plan["id"])
        self.assertEqual(refresh["final_status"], "已失败")
        self.assertEqual(refresh["trigger_source"], "调度器")

        audits = [
            row for row in store.rows("audit")
            if row.get("receipt_id") == receipt["receipt_id"]
        ]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["操作来源"], "调度器")
        self.assertEqual(audits[0]["最终状态"], "已失败")
        self.assertEqual(audits[0]["source"], "scheduler")

    def test_plan_with_unfinished_task_has_no_final_status(self) -> None:
        plan = self._create_plan([1])
        self.batch.run_action_detail(1, "启动任务")
        refresh = self.schedule.get_entry(plan["id"])
        self.assertEqual(refresh["execution_status"], "运行中")
        self.assertIsNone(refresh["final_status"])
        self.assertFalse(refresh["terminal"])

    def test_audit_records_every_trigger_source(self) -> None:
        self.batch.run_action_detail(2, "启动任务")
        execution_service.timeout_from_scheduler(2)
        execution_service.apply_receipt(task_id=3, status="已完成")
        rows = [row for row in store.rows("audit") if row.get("操作类型") == "批处理执行"]
        sources = {row["操作来源"] for row in rows}
        self.assertEqual(sources, {"页面", "调度器", "批处理"})
        for row in rows:
            self.assertTrue(row["最终状态"] in STATUS_LABELS.values())
            self.assertTrue(row["回执编号"])
            self.assertTrue(row["execution_id"])

    # ------------------------------------------------------------------
    # 契约辅助
    # ------------------------------------------------------------------

    def test_action_aliases_resolve_to_one_contract(self) -> None:
        timeout_contract = resolve_action("超时结束")
        self.assertEqual(timeout_contract.code, "timeout")
        self.assertEqual(resolve_action("timeout").code, "timeout")
        self.assertEqual(resolve_action("终止").code, "terminate")
        self.assertEqual(normalize_status("已完成"), "succeeded")
        self.assertEqual(normalize_status("运行中"), "running")


class PayloadTestCase(unittest.TestCase):
    def test_receipt_payload_accepts_legacy_values_wrapper(self) -> None:
        payload = BatchReceiptPayload(values={"task_id": 2, "status": "已完成"}).merged()
        self.assertEqual(payload.task_id, 2)
        self.assertEqual(payload.status, "已完成")

    def test_action_payload_accepts_both_shapes(self) -> None:
        legacy = BatchActionPayload(values={"action": "终止任务"})
        self.assertEqual(legacy.resolved_action(), "终止任务")
        modern = BatchActionPayload(action="timeout", source="scheduler", idempotency_key="k")
        self.assertEqual(modern.resolved_action(), "timeout")
        self.assertEqual(modern.resolved_source(), "scheduler")
        self.assertEqual(modern.resolved_idempotency_key(), "k")

    def test_timeout_payload_keeps_plan_reference(self) -> None:
        payload = BatchTimeoutPayload(plan_id=3, idempotency_key="k")
        self.assertEqual(payload.plan_id, 3)


if __name__ == "__main__":
    unittest.main()
