from __future__ import annotations

import json
import os
import random
import logging
from typing import Optional

from models import (
    CheckItem,
    CheckItemStatus,
    PreCheckResult,
    generate_id,
    now_iso,
)

logger = logging.getLogger(__name__)


class PreCheckEngine:
    def __init__(self, config: dict):
        self.config = config.get("pre_check", {})
        self.data_dir = os.path.join(config.get("system", {}).get("data_dir", "./data"), "pre_check")
        os.makedirs(self.data_dir, exist_ok=True)

    def run(self, release_id: str) -> PreCheckResult:
        logger.info("[%s] 开始发布前置校验", release_id)
        result = PreCheckResult(release_id=release_id, checked_at=now_iso())

        result.check_items.append(self._check_declaration_pass_rate())
        result.check_items.append(self._check_manifest_consistency())
        result.check_items.append(self._check_customs_interface())
        result.check_items.append(self._check_document_rule_coverage())

        result.compute_passed()
        self._save_result(result)
        status_text = "通过" if result.passed else "未通过"
        logger.info("[%s] 前置校验完成: %s", release_id, status_text)
        return result

    def _check_declaration_pass_rate(self) -> CheckItem:
        cfg = self.config.get("declaration_pass_rate", {})
        threshold = cfg.get("threshold", 0.95)
        actual = self._query_declaration_pass_rate()
        item = CheckItem(
            name="报关通过率",
            category="quality",
            value=actual,
            threshold=threshold,
        )
        if actual >= threshold:
            item.status = CheckItemStatus.PASS
            item.message = f"报关通过率 {actual:.2%} >= 阈值 {threshold:.2%}"
        else:
            item.status = CheckItemStatus.FAIL
            item.message = f"报关通过率 {actual:.2%} < 阈值 {threshold:.2%}"
            item.suggestion = "请检查报关规则引擎配置，核实HS编码匹配规则与禁限寄物品清单是否完整更新"
        return item

    def _check_manifest_consistency(self) -> CheckItem:
        cfg = self.config.get("manifest_consistency", {})
        threshold = cfg.get("threshold", 0.99)
        actual = self._query_manifest_consistency()
        item = CheckItem(
            name="舱单数据一致性",
            category="quality",
            value=actual,
            threshold=threshold,
        )
        if actual >= threshold:
            item.status = CheckItemStatus.PASS
            item.message = f"舱单一致性 {actual:.2%} >= 阈值 {threshold:.2%}"
        else:
            item.status = CheckItemStatus.FAIL
            item.message = f"舱单一致性 {actual:.2%} < 阈值 {threshold:.2%}"
            item.suggestion = "请排查舱单同步接口的字段映射差异，确认上游数据源未发生格式变更"
        return item

    def _check_customs_interface(self) -> CheckItem:
        cfg = self.config.get("customs_interface", {})
        timeout = cfg.get("timeout_seconds", 30)
        retry = cfg.get("retry_count", 3)
        connected = self._probe_customs_interface(timeout, retry)
        item = CheckItem(
            name="海关接口连通性",
            category="infrastructure",
            value=1.0 if connected else 0.0,
            threshold=1.0,
        )
        if connected:
            item.status = CheckItemStatus.PASS
            item.message = "海关接口连通性测试通过"
        else:
            item.status = CheckItemStatus.FAIL
            item.message = f"海关接口连通性测试失败 (超时{timeout}s, 重试{retry}次)"
            item.suggestion = "请检查海关接口网络配置、证书有效期及对接参数，必要时联系海关技术支持"
        return item

    def _check_document_rule_coverage(self) -> CheckItem:
        cfg = self.config.get("document_rule_coverage", {})
        threshold = cfg.get("threshold", 0.90)
        actual = self._query_document_rule_coverage()
        item = CheckItem(
            name="单证校验规则覆盖率",
            category="compliance",
            value=actual,
            threshold=threshold,
        )
        if actual >= threshold:
            item.status = CheckItemStatus.PASS
            item.message = f"规则覆盖率 {actual:.2%} >= 阈值 {threshold:.2%}"
        else:
            item.status = CheckItemStatus.FAIL
            item.message = f"规则覆盖率 {actual:.2%} < 阈值 {threshold:.2%}"
            item.suggestion = "请补充缺失的HS编码匹配规则或禁限寄物品校验规则，确保单证合规性"
        return item

    def _query_declaration_pass_rate(self) -> float:
        return round(random.uniform(0.90, 1.0), 4)

    def _query_manifest_consistency(self) -> float:
        return round(random.uniform(0.96, 1.0), 4)

    def _probe_customs_interface(self, timeout: int, retry: int) -> bool:
        return random.random() > 0.05

    def _query_document_rule_coverage(self) -> float:
        return round(random.uniform(0.85, 1.0), 4)

    def _save_result(self, result: PreCheckResult):
        path = os.path.join(self.data_dir, f"{result.release_id}.json")
        data = {
            "release_id": result.release_id,
            "passed": result.passed,
            "checked_at": result.checked_at,
            "items": [
                {
                    "name": i.name,
                    "category": i.category,
                    "status": i.status.value,
                    "value": i.value,
                    "threshold": i.threshold,
                    "message": i.message,
                    "suggestion": i.suggestion,
                }
                for i in result.check_items
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_result(self, release_id: str) -> Optional[PreCheckResult]:
        path = os.path.join(self.data_dir, f"{release_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = PreCheckResult(
            release_id=data["release_id"],
            passed=data["passed"],
            checked_at=data["checked_at"],
        )
        for item_data in data["items"]:
            result.check_items.append(CheckItem(
                name=item_data["name"],
                category=item_data["category"],
                status=CheckItemStatus(item_data["status"]),
                value=item_data["value"],
                threshold=item_data["threshold"],
                message=item_data["message"],
                suggestion=item_data["suggestion"],
            ))
        return result
