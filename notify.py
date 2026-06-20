from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class NotifyEngine:
    def __init__(self, config: dict):
        self.config = config.get("notification", {})
        self.channels = self.config.get("channels", [])

    def send(
        self,
        title: str,
        content: str,
        level: str = "info",
        channels: Optional[list[str]] = None,
    ):
        target_channels = channels or [c["type"] for c in self.channels]
        results = {}
        for ch in self.channels:
            if ch["type"] in target_channels:
                result = self._dispatch(ch, title, content, level)
                results[ch["type"]] = result
        logger.info("通知发送结果: %s", results)
        return results

    def send_circuit_break_alert(
        self,
        release_id: str,
        reason: str,
        trigger_value: float,
        threshold: float,
        affected_ports: list[str],
        rollback_version: str,
    ):
        title = f"【熔断告警】发布 {release_id} 触发自动回滚"
        content = (
            f"发布ID: {release_id}\n"
            f"熔断原因: {reason}\n"
            f"触发值: {trigger_value:.4f}\n"
            f"安全阈值: {threshold:.4f}\n"
            f"影响口岸: {', '.join(affected_ports)}\n"
            f"回滚版本: {rollback_version}\n"
            f"请立即关注并确认回滚结果"
        )
        return self.send(title, content, level="critical")

    def send_approval_notification(
        self,
        release_id: str,
        pending_roles: list[str],
        release_type: str,
    ):
        title = f"【审批通知】发布 {release_id} 待审批"
        content = (
            f"发布ID: {release_id}\n"
            f"发布类型: {release_type}\n"
            f"待审批角色: {', '.join(pending_roles)}\n"
            f"请尽快完成审批"
        )
        return self.send(title, content, level="warning")

    def send_pre_check_failed(
        self,
        release_id: str,
        failed_items: list[dict],
    ):
        title = f"【校验阻断】发布 {release_id} 前置校验未通过"
        items_text = "\n".join(
            f"  - {item['name']}: {item['message']}"
            for item in failed_items
        )
        suggestions_text = "\n".join(
            f"  - {item['name']}: {item['suggestion']}"
            for item in failed_items
            if item.get('suggestion')
        )
        content = (
            f"发布ID: {release_id}\n"
            f"未通过项:\n{items_text}\n"
            f"修复建议:\n{suggestions_text}"
        )
        return self.send(title, content, level="error")

    def send_drill_result(
        self,
        drill_id: str,
        status: str,
        duration: float,
        detail: str,
    ):
        title = f"【演练报告】回滚演练 {drill_id} {'成功' if status == 'success' else '失败'}"
        content = (
            f"演练ID: {drill_id}\n"
            f"状态: {status}\n"
            f"耗时: {duration:.2f}s\n"
            f"详情: {detail}"
        )
        return self.send(title, content, level="info")

    def _dispatch(self, channel: dict, title: str, content: str, level: str) -> dict:
        ch_type = channel["type"]
        if ch_type == "wecom":
            return self._send_wecom(channel, title, content, level)
        elif ch_type == "dingtalk":
            return self._send_dingtalk(channel, title, content, level)
        elif ch_type == "email":
            return self._send_email(channel, title, content, level)
        else:
            logger.warning("未知通知渠道: %s", ch_type)
            return {"channel": ch_type, "status": "unsupported"}

    def _send_wecom(self, channel: dict, title: str, content: str, level: str) -> dict:
        webhook = channel.get("webhook", "")
        if not webhook:
            logger.info("[企微] 模拟发送 - %s: %s", title, content[:100])
            return {"channel": "wecom", "status": "simulated"}
        return {"channel": "wecom", "status": "sent"}

    def _send_dingtalk(self, channel: dict, title: str, content: str, level: str) -> dict:
        webhook = channel.get("webhook", "")
        if not webhook:
            logger.info("[钉钉] 模拟发送 - %s: %s", title, content[:100])
            return {"channel": "dingtalk", "status": "simulated"}
        return {"channel": "dingtalk", "status": "sent"}

    def _send_email(self, channel: dict, title: str, content: str, level: str) -> dict:
        smtp_host = channel.get("smtp_host", "")
        if not smtp_host:
            logger.info("[邮件] 模拟发送 - %s: %s", title, content[:100])
            return {"channel": "email", "status": "simulated"}
        return {"channel": "email", "status": "sent"}
