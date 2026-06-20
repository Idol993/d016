from __future__ import annotations

import json
import os
import random
import time
import logging
from typing import Optional

from models import (
    CircuitBreakEvent,
    CircuitBreakReason,
    MonitoringSnapshot,
    PortGrayConfig,
    PortReleaseRecord,
    ReleaseStatus,
    now_iso,
)

logger = logging.getLogger(__name__)


class GrayReleaseEngine:
    def __init__(self, config: dict):
        self.gray_config = config.get("gray_release", {})
        self.monitoring_config = self.gray_config.get("monitoring", {})
        self.cb_config = self.gray_config.get("circuit_breaker", {})
        self.data_dir = os.path.join(config.get("system", {}).get("data_dir", "./data"), "gray_release")
        os.makedirs(self.data_dir, exist_ok=True)

    def build_gray_configs(self) -> list[PortGrayConfig]:
        configs = []
        for tier_cfg in self.gray_config.get("port_tiers", []):
            for port_name in tier_cfg.get("ports", []):
                configs.append(PortGrayConfig(
                    port_name=port_name,
                    tier=tier_cfg["tier"],
                    traffic_ratio=tier_cfg["traffic_ratio"],
                    is_core=tier_cfg.get("is_core", False),
                ))
        return sorted(configs, key=lambda c: c.tier)

    def deploy_tier(
        self,
        release_id: str,
        tier: int,
        version: str,
        previous_version: str,
        simulate_failure: bool = False,
    ) -> dict:
        gray_configs = self.build_gray_configs()
        tier_ports = [c for c in gray_configs if c.tier == tier]

        if not tier_ports:
            return {"success": False, "message": f"未找到第 {tier} 层口岸配置"}

        port_records = []
        for pc in tier_ports:
            pr = PortReleaseRecord(
                release_id=release_id,
                port_name=pc.port_name,
                tier=tier,
                version=version,
                previous_version=previous_version,
                deploy_started_at=now_iso(),
                status="deploying",
            )
            time.sleep(0.05)
            pr.deploy_completed_at = now_iso()
            pr.status = "deployed"
            port_records.append(pr)

        logger.info(
            "[%s] 部署第 %d 层灰度完成, 口岸: %s",
            release_id, tier, [p.port_name for p in port_records],
        )
        deploy_result = {
            "release_id": release_id,
            "tier": tier,
            "version": version,
            "previous_version": previous_version,
            "ports": [p.port_name for p in port_records],
            "traffic_ratio": tier_ports[0].traffic_ratio,
            "deployed_at": now_iso(),
            "success": True,
            "port_records": [self._serialize_port_record(pr) for pr in port_records],
        }

        self._save_deploy_record(release_id, tier, deploy_result)
        return deploy_result

    def monitor_tier(
        self,
        release_id: str,
        tier: int,
        duration_minutes: int = 30,
        simulate_anomaly: bool = False,
    ) -> dict:
        interval = self.monitoring_config.get("interval_seconds", 300)
        metrics_cfg = self.monitoring_config.get("metrics", {})
        snapshots = []
        circuit_break_triggered = False
        circuit_break_event = None

        checks = max(1, duration_minutes * 60 // interval)
        logger.info("[%s] 开始监控第 %d 层, 检查次数=%d, 间隔=%ds", release_id, tier, checks, interval)

        ports = self._get_tier_ports(tier)
        port_name = ports[0] if ports else "unknown"

        for i in range(checks):
            snapshot = self._collect_metrics(
                release_id=release_id,
                port_name=port_name,
                simulate_anomaly=simulate_anomaly and i >= checks // 2,
            )
            snapshots.append(snapshot)

            cb_result = self._check_circuit_break(snapshot, metrics_cfg)
            if cb_result["triggered"]:
                circuit_break_triggered = True
                circuit_break_event = CircuitBreakEvent(
                    release_id=release_id,
                    reason=CircuitBreakReason(cb_result["reason"]),
                    trigger_value=cb_result["value"],
                    threshold=cb_result["threshold"],
                    affected_ports=ports,
                    rollback_version="",
                    version="",
                    triggered_at=now_iso(),
                    details=f"口岸: {port_name}, 检查第 {i+1}/{checks} 次时触发熔断",
                )
                logger.warning(
                    "[%s] 熔断触发! 口岸=%s, 原因=%s, 触发值=%.4f, 阈值=%.4f",
                    release_id, port_name, cb_result["reason"],
                    cb_result["value"], cb_result["threshold"],
                )
                break

            if i < checks - 1:
                time.sleep(min(interval, 2))

        monitor_result = {
            "release_id": release_id,
            "tier": tier,
            "port_name": port_name,
            "snapshots_count": len(snapshots),
            "circuit_break_triggered": circuit_break_triggered,
            "circuit_break_event": self._serialize_cb_event(circuit_break_event) if circuit_break_event else None,
            "snapshots": [self._serialize_snapshot(s) for s in snapshots],
            "monitoring_started_at": snapshots[0].timestamp if snapshots else now_iso(),
            "monitoring_ended_at": snapshots[-1].timestamp if snapshots else now_iso(),
        }

        self._save_monitor_record(release_id, tier, monitor_result)
        return monitor_result

    def execute_rollback(
        self,
        release_id: str,
        previous_version: str,
        affected_ports: list[str],
        cb_event: Optional[CircuitBreakEvent] = None,
    ) -> dict:
        logger.info("[%s] 执行自动回滚, 目标版本=%s, 影响口岸=%s", release_id, previous_version, affected_ports)

        rollback_started = cb_event.triggered_at if cb_event else now_iso()
        time.sleep(0.1)
        rollback_completed = now_iso()

        if cb_event:
            cb_event.rollback_version = previous_version
            cb_event.rollback_completed_at = rollback_completed

        rollback_result = {
            "release_id": release_id,
            "rollback_version": previous_version,
            "affected_ports": affected_ports,
            "rollback_started_at": rollback_started,
            "rollback_completed_at": rollback_completed,
            "success": True,
            "circuit_break_event": self._serialize_cb_event(cb_event) if cb_event else None,
        }

        self._save_rollback_record(release_id, rollback_result)
        logger.info("[%s] 回滚完成, 版本已恢复至 %s", release_id, previous_version)
        return rollback_result

    def run_full_gray_release(
        self,
        release_id: str,
        version: str,
        previous_version: str,
        simulate_failure_tier: Optional[int] = None,
    ) -> dict:
        gray_configs = self.build_gray_configs()
        tiers = sorted(set(c.tier for c in gray_configs))
        results = []
        all_port_records = []
        all_cb_events = []
        all_snapshots = []

        for tier in tiers:
            deploy = self.deploy_tier(release_id, tier, version, previous_version)
            if not deploy.get("success"):
                return {"success": False, "stage": "deploy", "tier": tier, "results": results}

            for pr in deploy.get("port_records", []):
                pr["monitoring_started_at"] = now_iso()
                all_port_records.append(pr)

            should_simulate_failure = (tier == simulate_failure_tier)
            monitor = self.monitor_tier(
                release_id, tier, duration_minutes=1, simulate_anomaly=should_simulate_failure
            )

            all_snapshots.extend(monitor.get("snapshots", []))
            for pr in all_port_records:
                if pr["tier"] == tier:
                    pr["monitoring_ended_at"] = monitor["monitoring_ended_at"]

            if monitor.get("circuit_break_triggered"):
                cb_data = monitor.get("circuit_break_event", {})
                affected = cb_data.get("affected_ports", self._get_tier_ports(tier))
                cb_event = CircuitBreakEvent(
                    release_id=release_id,
                    reason=CircuitBreakReason(cb_data["reason"]),
                    trigger_value=cb_data["trigger_value"],
                    threshold=cb_data["threshold"],
                    affected_ports=affected,
                    rollback_version=previous_version,
                    version=version,
                    triggered_at=cb_data["triggered_at"],
                    details=cb_data.get("details", ""),
                )
                all_cb_events.append(self._serialize_cb_event(cb_event))

                for pr in all_port_records:
                    if pr["tier"] == tier:
                        pr["circuit_break"] = cb_data
                        pr["rollback_completed_at"] = now_iso()
                        pr["status"] = "rolled_back"

                rollback = self.execute_rollback(release_id, previous_version, affected, cb_event)
                results.append({
                    "tier": tier,
                    "deploy": deploy,
                    "monitor": monitor,
                    "rollback": rollback,
                    "status": "circuit_break_rolled_back",
                })
                return {
                    "success": False,
                    "stage": "monitor",
                    "tier": tier,
                    "circuit_break": True,
                    "results": results,
                    "port_records": all_port_records,
                    "circuit_break_events": all_cb_events,
                    "monitoring_snapshots": all_snapshots,
                    "version": version,
                    "previous_version": previous_version,
                }

            for pr in all_port_records:
                if pr["tier"] == tier:
                    pr["status"] = "passed"

            results.append({
                "tier": tier,
                "deploy": deploy,
                "monitor": monitor,
                "status": "passed",
            })

        return {
            "success": True,
            "stage": "completed",
            "tier": tiers[-1],
            "results": results,
            "port_records": all_port_records,
            "circuit_break_events": all_cb_events,
            "monitoring_snapshots": all_snapshots,
            "version": version,
            "previous_version": previous_version,
        }

    def get_port_release_history(
        self,
        port_name: Optional[str] = None,
        version: Optional[str] = None,
        release_id: Optional[str] = None,
    ) -> list[dict]:
        records = []
        if not os.path.exists(self.data_dir):
            return records

        for filename in sorted(os.listdir(self.data_dir)):
            if not (filename.endswith("_deploy.json") or filename.endswith("_rollback.json")):
                continue
            if release_id and not filename.startswith(release_id):
                continue

            path = os.path.join(self.data_dir, filename)
            with open(path, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    continue

            if port_name:
                ports = data.get("ports", [])
                if port_name not in ports and port_name != data.get("port_name"):
                    continue
            if version:
                if data.get("version") != version and data.get("rollback_version") != version:
                    continue

            records.append(data)

        return sorted(records, key=lambda r: r.get("deployed_at", r.get("rollback_started_at", "")), reverse=True)

    def _collect_metrics(
        self,
        release_id: str,
        port_name: str,
        simulate_anomaly: bool = False,
    ) -> MonitoringSnapshot:
        if simulate_anomaly:
            decl_fail = round(random.uniform(0.08, 0.20), 4)
            clear_delay = round(random.uniform(0.12, 0.30), 4)
            manifest_anomaly = round(random.uniform(0.05, 0.15), 4)
        else:
            decl_fail = round(random.uniform(0.001, 0.04), 4)
            clear_delay = round(random.uniform(0.01, 0.08), 4)
            manifest_anomaly = round(random.uniform(0.001, 0.02), 4)

        return MonitoringSnapshot(
            timestamp=now_iso(),
            port_name=port_name,
            declaration_failure_rate=decl_fail,
            clearance_delay_rate=clear_delay,
            manifest_anomaly_rate=manifest_anomaly,
        )

    def _check_circuit_break(self, snapshot: MonitoringSnapshot, metrics_cfg: dict) -> dict:
        decl_threshold = metrics_cfg.get("declaration_failure_rate", {}).get("threshold", 0.05)
        clear_threshold = metrics_cfg.get("clearance_delay_rate", {}).get("threshold", 0.10)
        manifest_threshold = metrics_cfg.get("manifest_anomaly_rate", {}).get("threshold", 0.03)

        if snapshot.declaration_failure_rate > decl_threshold:
            return {
                "triggered": True,
                "reason": CircuitBreakReason.DECLARATION_FAILURE_RATE.value,
                "value": snapshot.declaration_failure_rate,
                "threshold": decl_threshold,
            }
        if snapshot.clearance_delay_rate > clear_threshold:
            return {
                "triggered": True,
                "reason": CircuitBreakReason.CLEARANCE_DELAY_RATE.value,
                "value": snapshot.clearance_delay_rate,
                "threshold": clear_threshold,
            }
        if snapshot.manifest_anomaly_rate > manifest_threshold:
            return {
                "triggered": True,
                "reason": CircuitBreakReason.MANIFEST_ANOMALY_RATE.value,
                "value": snapshot.manifest_anomaly_rate,
                "threshold": manifest_threshold,
            }
        return {"triggered": False}

    def _get_tier_ports(self, tier: int) -> list[str]:
        gray_configs = self.build_gray_configs()
        return [c.port_name for c in gray_configs if c.tier == tier]

    def _serialize_snapshot(self, s: MonitoringSnapshot) -> dict:
        return {
            "timestamp": s.timestamp,
            "port_name": s.port_name,
            "declaration_failure_rate": s.declaration_failure_rate,
            "clearance_delay_rate": s.clearance_delay_rate,
            "manifest_anomaly_rate": s.manifest_anomaly_rate,
        }

    def _serialize_cb_event(self, e: CircuitBreakEvent) -> dict:
        return {
            "release_id": e.release_id,
            "reason": e.reason.value,
            "trigger_value": e.trigger_value,
            "threshold": e.threshold,
            "affected_ports": e.affected_ports,
            "rollback_version": e.rollback_version,
            "version": e.version,
            "triggered_at": e.triggered_at,
            "rollback_completed_at": e.rollback_completed_at,
            "details": e.details,
        }

    def _serialize_port_record(self, pr: PortReleaseRecord) -> dict:
        return {
            "release_id": pr.release_id,
            "port_name": pr.port_name,
            "tier": pr.tier,
            "version": pr.version,
            "previous_version": pr.previous_version,
            "deploy_started_at": pr.deploy_started_at,
            "deploy_completed_at": pr.deploy_completed_at,
            "monitoring_started_at": pr.monitoring_started_at,
            "monitoring_ended_at": pr.monitoring_ended_at,
            "rollback_completed_at": pr.rollback_completed_at,
            "status": pr.status,
            "circuit_break": self._serialize_cb_event(pr.circuit_break) if pr.circuit_break else None,
        }

    def _save_deploy_record(self, release_id: str, tier: int, data: dict):
        path = os.path.join(self.data_dir, f"{release_id}_tier{tier}_deploy.json")
        if "port_records" in data:
            data["port_records"] = [
                {k: v for k, v in pr.items() if k != "circuit_break" or v is not None}
                for pr in data["port_records"]
            ]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_monitor_record(self, release_id: str, tier: int, data: dict):
        path = os.path.join(self.data_dir, f"{release_id}_tier{tier}_monitor.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_rollback_record(self, release_id: str, data: dict):
        path = os.path.join(self.data_dir, f"{release_id}_rollback.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
