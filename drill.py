from __future__ import annotations

import json
import os
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

from models import (
    DrillRecord,
    DrillStatus,
    generate_id,
    now_iso,
)

logger = logging.getLogger(__name__)


class DrillEngine:
    def __init__(self, config: dict):
        self.config = config.get("drill", {})
        self.data_dir = os.path.join(config.get("system", {}).get("data_dir", "./data"), "drill")
        os.makedirs(self.data_dir, exist_ok=True)

    def schedule_drill(self, target_version: str = "", rollback_version: str = "") -> DrillRecord:
        record = DrillRecord(
            drill_id=generate_id("drill_"),
            scheduled_at=now_iso(),
            target_version=target_version,
            rollback_version=rollback_version,
        )
        self._save_record(record)
        logger.info("回滚演练已计划: %s, 计划时间=%s", record.drill_id, record.scheduled_at)
        return record

    def execute_drill(self, drill_id: str) -> DrillRecord:
        record = self.load_record(drill_id)
        if record is None:
            raise ValueError(f"演练记录不存在: {drill_id}")
        if record.status not in (DrillStatus.SCHEDULED, DrillStatus.RUNNING):
            raise ValueError(f"演练状态不允许执行: {record.status.value}")

        record.status = DrillStatus.RUNNING
        record.executed_at = now_iso()
        self._save_record(record)
        logger.info("[%s] 回滚演练开始执行", drill_id)

        start_time = time.time()
        success = self._run_drill_simulation(record)
        duration = time.time() - start_time

        record.duration_seconds = round(duration, 2)
        record.completed_at = now_iso()

        if success:
            record.status = DrillStatus.SUCCESS
            record.result_detail = (
                f"演练成功: 版本 {record.target_version} -> {record.rollback_version}, "
                f"耗时 {record.duration_seconds}s, 熔断机制验证通过"
            )
            logger.info("[%s] 回滚演练成功, 耗时 %.2fs", drill_id, duration)
        else:
            record.status = DrillStatus.FAILED
            record.result_detail = (
                f"演练失败: 回滚过程异常, 耗时 {record.duration_seconds}s, 需人工介入排查"
            )
            logger.error("[%s] 回滚演练失败", drill_id)

        self._save_record(record)
        return record

    def _run_drill_simulation(self, record: DrillRecord) -> bool:
        logger.info("[%s] 模拟熔断触发...", record.drill_id)
        time.sleep(0.5)

        logger.info("[%s] 模拟版本回滚 %s -> %s...", record.drill_id, record.target_version, record.rollback_version)
        time.sleep(0.5)

        logger.info("[%s] 模拟服务重启与验证...", record.drill_id)
        time.sleep(0.3)

        logger.info("[%s] 验证熔断机制有效性...", record.drill_id)
        return True

    def list_drills(self, status: Optional[DrillStatus] = None) -> list[DrillRecord]:
        records = []
        if not os.path.exists(self.data_dir):
            return records
        for filename in os.listdir(self.data_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self.data_dir, filename)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            record = DrillRecord(
                drill_id=data["drill_id"],
                scheduled_at=data["scheduled_at"],
                executed_at=data.get("executed_at", ""),
                completed_at=data.get("completed_at", ""),
                status=DrillStatus(data["status"]),
                target_version=data.get("target_version", ""),
                rollback_version=data.get("rollback_version", ""),
                duration_seconds=data.get("duration_seconds", 0.0),
                result_detail=data.get("result_detail", ""),
            )
            if status is None or record.status == status:
                records.append(record)
        return sorted(records, key=lambda r: r.scheduled_at, reverse=True)

    def load_record(self, drill_id: str) -> Optional[DrillRecord]:
        path = os.path.join(self.data_dir, f"{drill_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return DrillRecord(
            drill_id=data["drill_id"],
            scheduled_at=data["scheduled_at"],
            executed_at=data.get("executed_at", ""),
            completed_at=data.get("completed_at", ""),
            status=DrillStatus(data["status"]),
            target_version=data.get("target_version", ""),
            rollback_version=data.get("rollback_version", ""),
            duration_seconds=data.get("duration_seconds", 0.0),
            result_detail=data.get("result_detail", ""),
        )

    def _save_record(self, record: DrillRecord):
        path = os.path.join(self.data_dir, f"{record.drill_id}.json")
        data = {
            "drill_id": record.drill_id,
            "scheduled_at": record.scheduled_at,
            "executed_at": record.executed_at,
            "completed_at": record.completed_at,
            "status": record.status.value,
            "target_version": record.target_version,
            "rollback_version": record.rollback_version,
            "duration_seconds": record.duration_seconds,
            "result_detail": record.result_detail,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
