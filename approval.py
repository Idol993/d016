from __future__ import annotations

import json
import os
import logging
from datetime import timedelta
from typing import Optional, Tuple

from models import (
    ApprovalFlow,
    ApprovalRecord,
    ApprovalRole,
    ApprovalStatus,
    HotfixMode,
    ReleaseType,
    now_iso,
    parse_iso,
)

logger = logging.getLogger(__name__)


class ApprovalError(Exception):
    def __init__(self, message: str, current_role: Optional[str] = None):
        super().__init__(message)
        self.current_role = current_role


class ApprovalEngine:
    def __init__(self, config: dict):
        self.config = config.get("approval", {})
        self.hotfix_config = self.config.get("hotfix_flow", {})
        self.data_dir = os.path.join(config.get("system", {}).get("data_dir", "./data"), "approval")
        os.makedirs(self.data_dir, exist_ok=True)

    def create_flow(
        self,
        release_id: str,
        release_type: ReleaseType,
        reason: str = "",
        hotfix_mode: Optional[str] = None,
    ) -> ApprovalFlow:
        flow = ApprovalFlow(
            release_id=release_id,
            release_type=release_type,
            reason=reason,
        )

        if release_type == ReleaseType.REGULAR:
            for step in self.config.get("regular_flow", []):
                flow.records.append(ApprovalRecord(
                    role=ApprovalRole(step["role"]),
                    is_critical=step.get("is_critical", False),
                ))
            flow.current_step = 0
            logger.info(
                "[%s] 创建常规审批流: 步骤数=%d, 顺序=%s",
                release_id, len(flow.records),
                " → ".join([r.role.value for r in flow.records]),
            )

        elif release_type == ReleaseType.HOTFIX:
            if not reason:
                raise ApprovalError("紧急热修复必须填写紧急原因")

            default_mode = self.hotfix_config.get("default_mode", "parallel")
            mode_str = hotfix_mode or default_mode
            flow.hotfix_mode = HotfixMode(mode_str)

            critical_roles_cfg = self.hotfix_config.get("critical_roles", [])
            flow.hotfix_critical_roles = [ApprovalRole(r) for r in critical_roles_cfg]

            deadline_hours = self.hotfix_config.get("post_sign_deadline_hours", 24)
            deadline_dt = parse_iso(now_iso()) + timedelta(hours=deadline_hours)
            flow.post_sign_deadline = deadline_dt.strftime("%Y-%m-%dT%H:%M:%S")

            for step in self.config.get("regular_flow", []):
                is_critical = ApprovalRole(step["role"]) in flow.hotfix_critical_roles
                record = ApprovalRecord(
                    role=ApprovalRole(step["role"]),
                    is_critical=is_critical,
                )
                if flow.hotfix_mode == HotfixMode.POST_SIGN:
                    record.status = ApprovalStatus.SKIPPED
                flow.records.append(record)

            mode_text = "并行审批" if flow.hotfix_mode == HotfixMode.PARALLEL else "事后补签"
            critical_text = ",".join([r.value for r in flow.hotfix_critical_roles])
            logger.info(
                "[%s] 创建紧急审批流: 模式=%s, 关键角色=[%s], 补签截止=%s",
                release_id, mode_text, critical_text, flow.post_sign_deadline,
            )

        self._save_flow(flow)
        return flow

    def approve(
        self,
        release_id: str,
        role: ApprovalRole,
        approver: str,
        comment: str = "",
    ) -> ApprovalFlow:
        flow = self.load_flow(release_id)
        if flow is None:
            raise ValueError(f"审批流不存在: {release_id}")

        self._validate_approve_role(flow, role)

        for record in flow.records:
            if record.role == role and record.status == ApprovalStatus.PENDING:
                record.status = ApprovalStatus.APPROVED
                record.approver = approver
                record.comment = comment
                record.approved_at = now_iso()
                logger.info("[%s] %s(%s) 审批通过: %s", release_id, role.value, approver, comment)
                break

        if flow.release_type == ReleaseType.REGULAR:
            flow.current_step = self._advance_serial_step(flow)

        self._save_flow(flow)
        return flow

    def reject(
        self,
        release_id: str,
        role: ApprovalRole,
        approver: str,
        comment: str = "",
    ) -> ApprovalFlow:
        flow = self.load_flow(release_id)
        if flow is None:
            raise ValueError(f"审批流不存在: {release_id}")

        self._validate_approve_role(flow, role)

        for record in flow.records:
            if record.role == role and record.status == ApprovalStatus.PENDING:
                record.status = ApprovalStatus.REJECTED
                record.approver = approver
                record.comment = comment
                record.approved_at = now_iso()
                logger.info("[%s] %s(%s) 审批驳回: %s", release_id, role.value, approver, comment)
                break

        for record in flow.records:
            if record.status == ApprovalStatus.PENDING:
                record.status = ApprovalStatus.SKIPPED

        self._save_flow(flow)
        return flow

    def post_sign(
        self,
        release_id: str,
        role: ApprovalRole,
        approver: str,
        comment: str = "",
    ) -> ApprovalFlow:
        flow = self.load_flow(release_id)
        if flow is None:
            raise ValueError(f"审批流不存在: {release_id}")

        if flow.release_type != ReleaseType.HOTFIX:
            raise ApprovalError("只有紧急热修复发布才支持事后补签")

        found = False
        for record in flow.records:
            if record.role == role and record.status == ApprovalStatus.SKIPPED:
                record.status = ApprovalStatus.POST_SIGNED
                record.approver = approver
                record.comment = f"[事后补签] {comment}"
                record.approved_at = now_iso()
                found = True
                logger.info("[%s] %s(%s) 事后补签完成", release_id, role.value, approver)
                break

        if not found:
            for record in flow.records:
                if record.role == role:
                    if record.status == ApprovalStatus.APPROVED:
                        raise ApprovalError(f"该角色[{role.value}]已审批通过，无需补签")
                    elif record.status == ApprovalStatus.POST_SIGNED:
                        raise ApprovalError(f"该角色[{role.value}]已补签完成")
                    elif record.status == ApprovalStatus.REJECTED:
                        raise ApprovalError(f"该角色[{role.value}]已驳回，无法补签")
                    elif record.status == ApprovalStatus.PENDING:
                        raise ApprovalError(f"该角色[{role.value}]尚未审批，请使用approve而非post_sign")
            raise ApprovalError(f"未找到角色[{role.value}]的审批记录")

        self._save_flow(flow)
        return flow

    def _validate_approve_role(self, flow: ApprovalFlow, role: ApprovalRole):
        if flow.release_type == ReleaseType.REGULAR:
            current_role = flow.get_current_pending_role()
            if current_role is None:
                raise ApprovalError("所有审批已完成，无需继续审批")
            if current_role != role:
                role_map = {
                    "customs": "关务审批",
                    "operations": "运营审批",
                    "finance": "财务审批",
                    "tech": "技术审批",
                }
                current_name = role_map.get(current_role.value, current_role.value)
                requested_name = role_map.get(role.value, role.value)
                raise ApprovalError(
                    f"审批顺序错误！当前应由【{current_name}({current_role.value})】审批，"
                    f"您尝试以【{requested_name}({role.value})】审批，请按顺序执行。",
                    current_role=current_role.value,
                )
        else:
            role_record = None
            for r in flow.records:
                if r.role == role:
                    role_record = r
                    break
            if role_record is None:
                raise ApprovalError(f"角色[{role.value}]不在审批流中")
            if role_record.status != ApprovalStatus.PENDING:
                status_map = {
                    ApprovalStatus.APPROVED: "已审批通过",
                    ApprovalStatus.REJECTED: "已驳回",
                    ApprovalStatus.SKIPPED: "已跳过，请使用post_sign补签",
                    ApprovalStatus.POST_SIGNED: "已补签",
                }
                raise ApprovalError(
                    f"角色[{role.value}]当前状态为{status_map.get(role_record.status, role_record.status.value)}，不能审批"
                )

    def get_next_pending_roles(self, release_id: str) -> list[ApprovalRole]:
        flow = self.load_flow(release_id)
        if flow is None:
            return []

        if flow.release_type == ReleaseType.HOTFIX and flow.hotfix_mode == HotfixMode.PARALLEL:
            return flow.get_pending_roles()

        current = flow.get_current_pending_role()
        return [current] if current else []

    def get_approval_status_summary(self, release_id: str) -> dict:
        flow = self.load_flow(release_id)
        if flow is None:
            return {}

        pending = flow.get_pending_roles()
        need_post_sign = flow.get_roles_needing_post_sign()

        return {
            "release_id": release_id,
            "release_type": flow.release_type.value,
            "hotfix_mode": flow.hotfix_mode.value if flow.hotfix_mode else None,
            "emergency_reason": flow.reason,
            "can_enter_gray": flow.can_enter_gray(),
            "is_fully_approved": flow.is_fully_approved(),
            "pending_roles": [r.value for r in pending],
            "needing_post_sign": [r.value for r in need_post_sign],
            "post_sign_deadline": flow.post_sign_deadline,
            "current_role": flow.get_current_pending_role().value if flow.get_current_pending_role() else None,
            "records": [
                {
                    "role": r.role.value,
                    "status": r.status.value,
                    "approver": r.approver,
                    "comment": r.comment,
                    "approved_at": r.approved_at,
                    "is_critical": r.is_critical,
                }
                for r in flow.records
            ],
        }

    def _advance_serial_step(self, flow: ApprovalFlow) -> int:
        for i, record in enumerate(flow.records):
            if record.status == ApprovalStatus.PENDING:
                return i
        return len(flow.records)

    def load_flow(self, release_id: str) -> Optional[ApprovalFlow]:
        path = os.path.join(self.data_dir, f"{release_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        flow = ApprovalFlow(
            release_id=data["release_id"],
            release_type=ReleaseType(data["release_type"]),
            current_step=data.get("current_step", 0),
            reason=data.get("reason", ""),
            post_sign_deadline=data.get("post_sign_deadline", ""),
        )
        hotfix_mode = data.get("hotfix_mode")
        if hotfix_mode:
            flow.hotfix_mode = HotfixMode(hotfix_mode)
        flow.hotfix_critical_roles = [ApprovalRole(r) for r in data.get("hotfix_critical_roles", [])]

        for r in data.get("records", []):
            flow.records.append(ApprovalRecord(
                role=ApprovalRole(r["role"]),
                approver=r.get("approver", ""),
                status=ApprovalStatus(r["status"]),
                comment=r.get("comment", ""),
                approved_at=r.get("approved_at", ""),
                is_critical=r.get("is_critical", False),
            ))
        return flow

    def _save_flow(self, flow: ApprovalFlow):
        path = os.path.join(self.data_dir, f"{flow.release_id}.json")
        data = {
            "release_id": flow.release_id,
            "release_type": flow.release_type.value,
            "current_step": flow.current_step,
            "reason": flow.reason,
            "hotfix_mode": flow.hotfix_mode.value if flow.hotfix_mode else None,
            "hotfix_critical_roles": [r.value for r in flow.hotfix_critical_roles],
            "post_sign_deadline": flow.post_sign_deadline,
            "records": [
                {
                    "role": r.role.value,
                    "approver": r.approver,
                    "status": r.status.value,
                    "comment": r.comment,
                    "approved_at": r.approved_at,
                    "is_critical": r.is_critical,
                }
                for r in flow.records
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
