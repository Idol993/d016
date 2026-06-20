from __future__ import annotations

import argparse
import json
import os
import sys
import logging
import yaml
from datetime import datetime

from models import (
    ApprovalRole,
    ApprovalStatus,
    DrillStatus,
    ReleaseRecord,
    ReleaseStatus,
    ReleaseType,
    generate_id,
    now_iso,
)
from pre_check import PreCheckEngine
from approval import ApprovalEngine
from gray_release import GrayReleaseEngine
from drill import DrillEngine
from report import ReportEngine
from audit import AuditLogger
from notify import NotifyEngine


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class ReleasePlatform:
    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)
        self.pre_check = PreCheckEngine(self.config)
        self.approval = ApprovalEngine(self.config)
        self.gray_release = GrayReleaseEngine(self.config)
        self.drill = DrillEngine(self.config)
        self.report = ReportEngine(self.config)
        self.audit = AuditLogger(self.config)
        self.notify = NotifyEngine(self.config)
        self.releases_dir = os.path.join(
            self.config.get("system", {}).get("data_dir", "./data"), "releases"
        )
        os.makedirs(self.releases_dir, exist_ok=True)

    def submit_release(
        self,
        version: str,
        previous_version: str,
        release_type: str,
        applicant: str,
        reason: str = "",
    ) -> ReleaseRecord:
        rel_type = ReleaseType(release_type)
        release_id = generate_id("rel_")
        record = ReleaseRecord(
            release_id=release_id,
            version=version,
            previous_version=previous_version,
            release_type=rel_type,
            status=ReleaseStatus.PENDING_CHECK,
            applicant=applicant,
            apply_time=now_iso(),
        )

        self.audit.log("submit_release", applicant, release_id, f"版本={version}, 类型={release_type}")
        self._save_release(record)
        logger.info("=" * 60)
        logger.info("发布申请已提交: %s, 版本: %s, 类型: %s", release_id, version, release_type)

        logger.info("-" * 40)
        logger.info("阶段1: 发布前置校验")
        pre_check_result = self.pre_check.run(release_id)
        record.pre_check_result = pre_check_result
        self._save_release(record)

        if not pre_check_result.passed:
            record.status = ReleaseStatus.CHECK_FAILED
            self._save_release(record)
            failed_items = [
                {"name": i.name, "message": i.message, "suggestion": i.suggestion}
                for i in pre_check_result.check_items
                if i.status.value == "fail"
            ]
            self.notify.send_pre_check_failed(release_id, failed_items)
            self.audit.log("pre_check_failed", "system", release_id, f"未通过项: {len(failed_items)}")
            logger.info("前置校验未通过，发布已阻断")
            self._print_pre_check_result(pre_check_result)
            return record

        record.status = ReleaseStatus.CHECK_PASSED
        self._save_release(record)
        self.audit.log("pre_check_passed", "system", release_id)
        logger.info("前置校验全部通过")
        self._print_pre_check_result(pre_check_result)

        logger.info("-" * 40)
        logger.info("阶段2: 分级审批流转")
        record.status = ReleaseStatus.PENDING_APPROVAL
        flow = self.approval.create_flow(release_id, rel_type, reason)
        record.approval_flow = flow
        self._save_release(record)

        pending_roles = self.approval.get_next_pending_roles(release_id)
        if pending_roles:
            self.notify.send_approval_notification(
                release_id,
                [r.value for r in pending_roles],
                rel_type.value,
            )

        logger.info("审批流已创建, 待审批角色: %s", [r.value for r in pending_roles])
        self._print_approval_flow(flow)
        return record

    def approve_release(
        self,
        release_id: str,
        role: str,
        approver: str,
        comment: str = "",
    ) -> ReleaseRecord:
        record = self._load_release(release_id)
        if record is None:
            raise ValueError(f"发布记录不存在: {release_id}")

        flow = self.approval.approve(release_id, ApprovalRole(role), approver, comment)
        record.approval_flow = flow
        self.audit.log("approve", approver, release_id, f"角色={role}, 意见={comment}")
        self._save_release(record)

        if flow.is_fully_approved():
            record.status = ReleaseStatus.APPROVAL_PASSED
            self._save_release(record)
            self.audit.log("approval_completed", "system", release_id)
            logger.info("[%s] 全部审批通过", release_id)
        else:
            pending = self.approval.get_next_pending_roles(release_id)
            self.notify.send_approval_notification(
                release_id,
                [r.value for r in pending],
                record.release_type.value,
            )
            logger.info("[%s] 待继续审批: %s", release_id, [r.value for r in pending])

        self._print_approval_flow(flow)
        return record

    def reject_release(
        self,
        release_id: str,
        role: str,
        approver: str,
        comment: str = "",
    ) -> ReleaseRecord:
        record = self._load_release(release_id)
        if record is None:
            raise ValueError(f"发布记录不存在: {release_id}")

        flow = self.approval.reject(release_id, ApprovalRole(role), approver, comment)
        record.approval_flow = flow
        record.status = ReleaseStatus.APPROVAL_REJECTED
        self.audit.log("reject", approver, release_id, f"角色={role}, 原因={comment}")
        self._save_release(record)
        logger.info("[%s] 审批已驳回", release_id)
        return record

    def execute_gray_release(
        self,
        release_id: str,
        simulate_failure_tier: int = 0,
    ) -> dict:
        record = self._load_release(release_id)
        if record is None:
            raise ValueError(f"发布记录不存在: {release_id}")
        if record.status not in (ReleaseStatus.APPROVAL_PASSED, ReleaseStatus.GRAY_DEPLOYING):
            raise ValueError(f"发布状态不允许灰度: {record.status.value}")

        record.status = ReleaseStatus.GRAY_DEPLOYING
        self._save_release(record)
        self.audit.log("gray_release_start", "system", release_id, f"版本={record.version}")

        logger.info("-" * 40)
        logger.info("阶段3: 口岸灰度发布与监控")
        result = self.gray_release.run_full_gray_release(
            release_id=release_id,
            version=record.version,
            previous_version=record.previous_version,
            simulate_failure_tier=simulate_failure_tier if simulate_failure_tier > 0 else None,
        )

        if result.get("circuit_break"):
            record.status = ReleaseStatus.ROLLED_BACK
            self._save_release(record)
            cb_data = result.get("results", [])[-1].get("monitor", {}).get("circuit_break_event", {})
            affected_ports = cb_data.get("affected_ports", [])
            self.notify.send_circuit_break_alert(
                release_id,
                cb_data.get("reason", ""),
                cb_data.get("trigger_value", 0),
                cb_data.get("threshold", 0),
                affected_ports,
                record.previous_version,
            )
            self.audit.log(
                "circuit_break_rollback",
                "system",
                release_id,
                f"熔断原因={cb_data.get('reason')}, 影响口岸={affected_ports}",
            )
            logger.warning("[%s] 熔断触发，已自动回滚至 %s", release_id, record.previous_version)
        else:
            record.status = ReleaseStatus.FULL_RELEASED
            record.finish_time = now_iso()
            self._save_release(record)
            self.audit.log("release_completed", "system", release_id, f"版本={record.version}")
            logger.info("[%s] 灰度发布全量完成，版本 %s 已上线", release_id, record.version)

        self._print_gray_result(result)
        return result

    def run_drill(self, target_version: str = "v2.0.0", rollback_version: str = "v1.9.0") -> dict:
        logger.info("-" * 40)
        logger.info("阶段4: 回滚演练")
        drill_record = self.drill.schedule_drill(target_version, rollback_version)
        self.audit.log("drill_scheduled", "system", drill_record.drill_id, f"目标={target_version}")

        drill_record = self.drill.execute_drill(drill_record.drill_id)
        self.audit.log(
            "drill_completed",
            "system",
            drill_record.drill_id,
            f"状态={drill_record.status.value}, 耗时={drill_record.duration_seconds}s",
        )
        self.notify.send_drill_result(
            drill_record.drill_id,
            drill_record.status.value,
            drill_record.duration_seconds,
            drill_record.result_detail,
        )
        return {
            "drill_id": drill_record.drill_id,
            "status": drill_record.status.value,
            "duration_seconds": drill_record.duration_seconds,
            "detail": drill_record.result_detail,
        }

    def generate_report(self, week_offset: int = 0) -> dict:
        logger.info("-" * 40)
        logger.info("生成周报")
        report = self.report.generate_weekly_report(week_offset)
        self.audit.log("report_generated", "system", report.report_id)
        return {
            "report_id": report.report_id,
            "period": f"{report.period_start} ~ {report.period_end}",
            "total_releases": report.total_releases,
            "success_releases": report.success_releases,
            "rollback_count": report.rollback_count,
            "avg_approval_duration_minutes": report.avg_approval_duration_minutes,
            "failure_rate_by_port": report.failure_rate_by_port,
        }

    def query_audit(
        self,
        start_date: str = "",
        end_date: str = "",
        action: str = "",
        operator: str = "",
        target: str = "",
    ) -> list[dict]:
        return self.audit.query(
            start_date=start_date or None,
            end_date=end_date or None,
            action=action or None,
            operator=operator or None,
            target=target or None,
        )

    def verify_audit_integrity(self, date_str: str = "") -> dict:
        return self.audit.verify_integrity(date_str or None)

    def _save_release(self, record: ReleaseRecord):
        data = {
            "release_id": record.release_id,
            "version": record.version,
            "previous_version": record.previous_version,
            "release_type": record.release_type.value,
            "status": record.status.value,
            "applicant": record.applicant,
            "apply_time": record.apply_time,
            "finish_time": record.finish_time,
        }
        path = os.path.join(self.releases_dir, f"{record.release_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_release(self, release_id: str):
        path = os.path.join(self.releases_dir, f"{release_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ReleaseRecord(
            release_id=data["release_id"],
            version=data["version"],
            previous_version=data["previous_version"],
            release_type=ReleaseType(data["release_type"]),
            status=ReleaseStatus(data["status"]),
            applicant=data["applicant"],
            apply_time=data["apply_time"],
            finish_time=data.get("finish_time", ""),
        )

    def _print_pre_check_result(self, result):
        logger.info("┌──────────────────────────────────────────────┐")
        logger.info("│           发布前置校验结果                    │")
        logger.info("├──────────────────────────────────────────────┤")
        for item in result.check_items:
            status_icon = "✓" if item.status.value == "pass" else "✗"
            logger.info("│ %s %-12s %-20s %s", status_icon, item.category, item.name, item.message[:30])
            if item.suggestion:
                logger.info("│   建议: %s", item.suggestion[:40])
        logger.info("├──────────────────────────────────────────────┤")
        overall = "全部通过 ✓" if result.passed else "存在未通过项 ✗"
        logger.info("│ 结论: %s", overall)
        logger.info("└──────────────────────────────────────────────┘")

    def _print_approval_flow(self, flow):
        logger.info("┌──────────────────────────────────────────────┐")
        logger.info("│           审批流转状态                        │")
        logger.info("├──────────────────────────────────────────────┤")
        for i, record in enumerate(flow.records):
            status_map = {
                "pending": "⏳ 待审批",
                "approved": "✓ 已通过",
                "rejected": "✗ 已驳回",
                "skipped": "○ 已跳过",
            }
            status_text = status_map.get(record.status.value, record.status.value)
            logger.info("│ %d. %-12s %s %s", i + 1, record.role.value, status_text, record.approver)
            if record.comment:
                logger.info("│   意见: %s", record.comment[:40])
        logger.info("├──────────────────────────────────────────────┤")
        overall = "全部通过 ✓" if flow.is_fully_approved() else "审批中..."
        logger.info("│ 结论: %s", overall)
        logger.info("└──────────────────────────────────────────────┘")

    def _print_gray_result(self, result: dict):
        logger.info("┌──────────────────────────────────────────────┐")
        logger.info("│           灰度发布结果                        │")
        logger.info("├──────────────────────────────────────────────┤")
        for r in result.get("results", []):
            tier = r.get("tier", "?")
            status = r.get("status", "?")
            status_text = "✓ 通过" if status == "passed" else "✗ 熔断回滚"
            logger.info("│ 第 %d 层: %s", tier, status_text)
        logger.info("├──────────────────────────────────────────────┤")
        if result.get("circuit_break"):
            logger.info("│ 结论: 熔断触发，已自动回滚 ✗")
        else:
            logger.info("│ 结论: 全量发布成功 ✓")
        logger.info("└──────────────────────────────────────────────┘")


def cmd_submit(args, platform: ReleasePlatform):
    record = platform.submit_release(
        version=args.version,
        previous_version=args.previous_version,
        release_type=args.type,
        applicant=args.applicant,
        reason=args.reason or "",
    )
    print(f"\n发布ID: {record.release_id}")
    print(f"状态: {record.status.value}")
    if record.status == ReleaseStatus.CHECK_FAILED:
        print("前置校验未通过，发布已阻断。请修复后重新提交。")
    elif record.status == ReleaseStatus.PENDING_APPROVAL:
        print("前置校验通过，等待审批。使用 approve 命令进行审批。")


def cmd_approve(args, platform: ReleasePlatform):
    record = platform.approve_release(
        release_id=args.release_id,
        role=args.role,
        approver=args.approver,
        comment=args.comment or "",
    )
    print(f"\n发布ID: {record.release_id}")
    print(f"状态: {record.status.value}")


def cmd_reject(args, platform: ReleasePlatform):
    record = platform.reject_release(
        release_id=args.release_id,
        role=args.role,
        approver=args.approver,
        comment=args.comment or "",
    )
    print(f"\n发布ID: {record.release_id}")
    print(f"状态: {record.status.value}")


def cmd_release(args, platform: ReleasePlatform):
    result = platform.execute_gray_release(
        release_id=args.release_id,
        simulate_failure_tier=args.simulate_failure or 0,
    )
    print(f"\n灰度发布结果: {'成功' if result.get('success') else '熔断回滚'}")


def cmd_drill(args, platform: ReleasePlatform):
    result = platform.run_drill(
        target_version=args.target_version,
        rollback_version=args.rollback_version,
    )
    print(f"\n演练ID: {result['drill_id']}")
    print(f"状态: {result['status']}")
    print(f"耗时: {result['duration_seconds']}s")
    print(f"详情: {result['detail']}")


def cmd_report(args, platform: ReleasePlatform):
    report = platform.generate_report(week_offset=args.week_offset or 0)
    print(f"\n报告ID: {report['report_id']}")
    print(f"周期: {report['period']}")
    print(f"总发布数: {report['total_releases']}")
    print(f"成功数: {report['success_releases']}")
    print(f"回滚数: {report['rollback_count']}")
    print(f"平均审批时长: {report['avg_approval_duration_minutes']} 分钟")


def cmd_audit(args, platform: ReleasePlatform):
    records = platform.query_audit(
        start_date=args.start_date or "",
        end_date=args.end_date or "",
        action=args.action or "",
        operator=args.operator or "",
        target=args.target or "",
    )
    print(f"\n审计记录数: {len(records)}")
    for r in records[:20]:
        print(f"  [{r['timestamp']}] {r['action']} by {r['operator']} -> {r['target']}: {r.get('detail', '')[:50]}")


def cmd_verify(args, platform: ReleasePlatform):
    result = platform.verify_audit_integrity(date_str=args.date or "")
    print(f"\n审计完整性校验:")
    print(f"  检查文件数: {result['files_checked']}")
    print(f"  总条目数: {result['total_entries']}")
    print(f"  篡改条目数: {result['tampered']}")
    print(f"  结论: {'完整性校验通过 ✓' if result['tampered'] == 0 else '存在篡改记录 ✗'}")


def cmd_full_flow(args, platform: ReleasePlatform):
    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║   跨境物流清关系统 - 完整发布流程演示         ║")
    logger.info("╚══════════════════════════════════════════════╝")

    logger.info("\n>>> 场景A: 常规发布 - 正常流程")
    record = platform.submit_release(
        version=args.version,
        previous_version=args.previous_version,
        release_type="regular",
        applicant="demo_user",
        reason="季度功能迭代",
    )

    if record.status == ReleaseStatus.CHECK_PASSED:
        for role in ["customs", "operations", "finance", "tech"]:
            approver = f"{role}_reviewer"
            record = platform.approve_release(
                release_id=record.release_id,
                role=role,
                approver=approver,
                comment=f"{role}审批通过",
            )
            if record.status == ReleaseStatus.APPROVAL_REJECTED:
                break

        if record.status == ReleaseStatus.APPROVAL_PASSED:
            result = platform.execute_gray_release(record.release_id)
            print(f"\n场景A完成: {'全量发布成功' if result.get('success') else '已熔断回滚'}")
        else:
            print("\n场景A: 审批未通过")
    else:
        print("\n场景A: 前置校验未通过")

    logger.info("\n>>> 场景B: 紧急热修复 - 模拟熔断回滚")
    record_b = platform.submit_release(
        version=f"{args.version}-hotfix",
        previous_version=args.previous_version,
        release_type="hotfix",
        applicant="admin",
        reason="紧急修复报关接口异常",
    )

    if record_b.status in (ReleaseStatus.CHECK_PASSED, ReleaseStatus.PENDING_APPROVAL):
        for role in ["customs", "tech"]:
            record_b = platform.approve_release(
                release_id=record_b.release_id,
                role=role,
                approver=f"{role}_reviewer",
                comment=f"[紧急] {role}审批通过",
            )

        if record_b.status == ReleaseStatus.APPROVAL_PASSED:
            result_b = platform.execute_gray_release(
                record_b.release_id,
                simulate_failure_tier=2,
            )
            print(f"\n场景B完成: {'全量发布成功' if result_b.get('success') else '熔断触发，已自动回滚'}")

    logger.info("\n>>> 场景C: 回滚演练")
    drill_result = platform.run_drill(
        target_version=args.version,
        rollback_version=args.previous_version,
    )
    print(f"\n场景C完成: 演练{drill_result['status']}, 耗时{drill_result['duration_seconds']}s")

    logger.info("\n>>> 场景D: 生成周报")
    report = platform.generate_report()
    print(f"\n场景D完成: 周报 {report['report_id']} 已生成")

    logger.info("╔══════════════════════════════════════════════╗")
    logger.info("║           全流程演示完成                       ║")
    logger.info("╚══════════════════════════════════════════════╝")


def main():
    parser = argparse.ArgumentParser(
        description="跨境物流清关系统版本发布与合规回滚自动化平台",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    p_submit = subparsers.add_parser("submit", help="提交发布申请")
    p_submit.add_argument("--version", required=True, help="发布版本号")
    p_submit.add_argument("--previous-version", required=True, help="上一稳定版本号")
    p_submit.add_argument("--type", choices=["regular", "hotfix"], default="regular", help="发布类型")
    p_submit.add_argument("--applicant", default="developer", help="申请人")
    p_submit.add_argument("--reason", default="", help="发布原因(热修复必填)")

    p_approve = subparsers.add_parser("approve", help="审批通过")
    p_approve.add_argument("--release-id", required=True, help="发布ID")
    p_approve.add_argument("--role", required=True, choices=["customs", "operations", "finance", "tech"], help="审批角色")
    p_approve.add_argument("--approver", required=True, help="审批人")
    p_approve.add_argument("--comment", default="", help="审批意见")

    p_reject = subparsers.add_parser("reject", help="审批驳回")
    p_reject.add_argument("--release-id", required=True, help="发布ID")
    p_reject.add_argument("--role", required=True, choices=["customs", "operations", "finance", "tech"], help="审批角色")
    p_reject.add_argument("--approver", required=True, help="审批人")
    p_reject.add_argument("--comment", default="", help="驳回原因")

    p_release = subparsers.add_parser("release", help="执行灰度发布")
    p_release.add_argument("--release-id", required=True, help="发布ID")
    p_release.add_argument("--simulate-failure", type=int, default=0, help="模拟第N层熔断(测试用)")

    p_drill = subparsers.add_parser("drill", help="执行回滚演练")
    p_drill.add_argument("--target-version", default="v2.0.0", help="演练目标版本")
    p_drill.add_argument("--rollback-version", default="v1.9.0", help="演练回滚版本")

    p_report = subparsers.add_parser("report", help="生成周报")
    p_report.add_argument("--week-offset", type=int, default=0, help="周偏移(0=本周)")

    p_audit = subparsers.add_parser("audit", help="查询审计日志")
    p_audit.add_argument("--start-date", default="", help="开始日期")
    p_audit.add_argument("--end-date", default="", help="结束日期")
    p_audit.add_argument("--action", default="", help="操作类型")
    p_audit.add_argument("--operator", default="", help="操作人")
    p_audit.add_argument("--target", default="", help="目标对象")

    p_verify = subparsers.add_parser("verify", help="校验审计日志完整性")
    p_verify.add_argument("--date", default="", help="指定日期(空=全部)")

    p_full = subparsers.add_parser("full-flow", help="完整流程演示")
    p_full.add_argument("--version", default="v2.1.0", help="发布版本号")
    p_full.add_argument("--previous-version", default="v2.0.0", help="上一稳定版本号")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    platform = ReleasePlatform(args.config)

    commands_map = {
        "submit": cmd_submit,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "release": cmd_release,
        "drill": cmd_drill,
        "report": cmd_report,
        "audit": cmd_audit,
        "verify": cmd_verify,
        "full-flow": cmd_full_flow,
    }

    handler = commands_map.get(args.command)
    if handler:
        handler(args, platform)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
