from __future__ import annotations

import csv
import hashlib
import json
import os
import logging
from typing import Optional
from datetime import datetime

from models import now_iso

logger = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self, config: dict):
        self.config = config.get("audit", {})
        self.system_config = config.get("system", {})
        self.data_dir = os.path.join(self.system_config.get("data_dir", "./data"), "audit")
        self.immutable = self.config.get("immutable", True)
        self.release_data_dir = os.path.join(self.system_config.get("data_dir", "./data"), "releases")
        self.gray_data_dir = os.path.join(self.system_config.get("data_dir", "./data"), "gray_release")
        self.approval_data_dir = os.path.join(self.system_config.get("data_dir", "./data"), "approval")
        os.makedirs(self.data_dir, exist_ok=True)

    def log(
        self,
        action: str,
        operator: str,
        target: str = "",
        detail: str = "",
        extra: Optional[dict] = None,
    ):
        timestamp = now_iso()
        entry = {
            "timestamp": timestamp,
            "action": action,
            "operator": operator,
            "target": target,
            "detail": detail,
            "extra": extra or {},
        }
        entry["hash"] = self._compute_hash(entry)

        date_str = timestamp[:10]
        log_path = os.path.join(self.data_dir, f"audit_{date_str}.jsonl")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        logger.debug("审计日志: %s %s %s", action, operator, target)

    def query(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        action: Optional[str] = None,
        operator: Optional[str] = None,
        target: Optional[str] = None,
    ) -> list[dict]:
        results = []
        if not os.path.exists(self.data_dir):
            return results

        for filename in sorted(os.listdir(self.data_dir)):
            if not filename.startswith("audit_") or not filename.endswith(".jsonl"):
                continue
            date_str = filename[6:16]
            if start_date and date_str < start_date:
                continue
            if end_date and date_str > end_date:
                continue

            path = os.path.join(self.data_dir, filename)
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if action and entry.get("action") != action:
                        continue
                    if operator and entry.get("operator") != operator:
                        continue
                    if target and entry.get("target") != target:
                        continue

                    if self.immutable:
                        original_hash = entry.pop("hash", "")
                        computed_hash = self._compute_hash(entry)
                        entry["hash"] = original_hash
                        entry["tamper_check"] = "pass" if original_hash == computed_hash else "TAMPERED"

                    results.append(entry)

        return results

    def query_release_history(
        self,
        version: Optional[str] = None,
        port: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        release_id: Optional[str] = None,
    ) -> list[dict]:
        results = []
        if not os.path.exists(self.release_data_dir):
            return results

        release_ids_to_check = []
        if release_id:
            release_ids_to_check = [release_id]
        else:
            for filename in sorted(os.listdir(self.release_data_dir)):
                if not filename.endswith(".json"):
                    continue
                rid = filename[:-5]
                release_ids_to_check.append(rid)

        for rid in release_ids_to_check:
            release_info = self._load_release_info(rid)
            if not release_info:
                continue

            if start_date and release_info.get("apply_time", "")[:10] < start_date:
                continue
            if end_date and release_info.get("apply_time", "")[:10] > end_date:
                continue

            if version and release_info.get("version") != version and release_info.get("previous_version") != version:
                continue

            port_records = self._load_port_records(rid)
            if port:
                port_matched = any(
                    pr.get("port_name") == port
                    for pr in port_records
                )
                if not port_matched:
                    continue

            approval_info = self._load_approval_info(rid)
            circuit_break_events = self._load_circuit_break_events(rid)

            result = {
                "release_id": rid,
                "version": release_info.get("version"),
                "previous_version": release_info.get("previous_version"),
                "release_type": release_info.get("release_type"),
                "status": release_info.get("status"),
                "applicant": release_info.get("applicant"),
                "apply_time": release_info.get("apply_time"),
                "finish_time": release_info.get("finish_time", ""),
                "emergency_reason": release_info.get("emergency_reason", ""),
                "approval": approval_info,
                "port_records": port_records,
                "circuit_break_events": circuit_break_events,
            }
            results.append(result)

        return sorted(results, key=lambda r: r.get("apply_time", ""), reverse=True)

    def _load_release_info(self, release_id: str) -> Optional[dict]:
        path = os.path.join(self.release_data_dir, f"{release_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_approval_info(self, release_id: str) -> dict:
        path = os.path.join(self.approval_data_dir, f"{release_id}.json")
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        records = []
        for r in data.get("records", []):
            records.append({
                "role": r.get("role"),
                "status": r.get("status"),
                "approver": r.get("approver", ""),
                "comment": r.get("comment", ""),
                "approved_at": r.get("approved_at", ""),
                "is_critical": r.get("is_critical", False),
            })

        rtype = data.get("release_type")
        hotfix_mode = data.get("hotfix_mode")
        modified = False
        if rtype == "hotfix" and hotfix_mode == "parallel":
            critical_recs = [r for r in records if r.get("is_critical")]
            critical_approved = all(
                r.get("status") in ("approved", "post_signed") for r in critical_recs
            )
            if critical_approved:
                for r in records:
                    if not r.get("is_critical") and r.get("status") == "pending":
                        r["status"] = "skipped"
                        modified = True

        if modified:
            for i, raw in enumerate(data.get("records", [])):
                if i < len(records) and records[i]["status"] != raw.get("status"):
                    data["records"][i]["status"] = records[i]["status"]
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        return {
            "release_type": rtype,
            "hotfix_mode": hotfix_mode,
            "emergency_reason": data.get("reason", ""),
            "post_sign_deadline": data.get("post_sign_deadline", ""),
            "records": records,
        }

    def _load_port_records(self, release_id: str) -> list[dict]:
        records = []
        if not os.path.exists(self.gray_data_dir):
            return records

        for filename in sorted(os.listdir(self.gray_data_dir)):
            if not filename.startswith(release_id):
                continue

            path = os.path.join(self.gray_data_dir, filename)
            with open(path, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    continue

            if "port_records" in data:
                for pr in data["port_records"]:
                    pr_data = {
                        "port_name": pr.get("port_name"),
                        "tier": pr.get("tier"),
                        "version": pr.get("version"),
                        "previous_version": pr.get("previous_version"),
                        "deploy_started_at": pr.get("deploy_started_at", ""),
                        "deploy_completed_at": pr.get("deploy_completed_at", ""),
                        "monitoring_started_at": pr.get("monitoring_started_at", ""),
                        "monitoring_ended_at": pr.get("monitoring_ended_at", ""),
                        "rollback_completed_at": pr.get("rollback_completed_at", ""),
                        "status": pr.get("status", ""),
                    }
                    if pr.get("circuit_break"):
                        cb = pr["circuit_break"]
                        pr_data["circuit_break"] = {
                            "reason": cb.get("reason"),
                            "trigger_value": cb.get("trigger_value"),
                            "threshold": cb.get("threshold"),
                            "triggered_at": cb.get("triggered_at", ""),
                            "rollback_version": cb.get("rollback_version", ""),
                            "rollback_completed_at": cb.get("rollback_completed_at", ""),
                            "details": cb.get("details", ""),
                        }
                    records.append(pr_data)
            elif filename.endswith("_rollback.json"):
                for pr in records:
                    if not pr.get("rollback_completed_at"):
                        pr["rollback_completed_at"] = data.get("rollback_completed_at", "")

        return records

    def _load_circuit_break_events(self, release_id: str) -> list[dict]:
        events = []
        if not os.path.exists(self.gray_data_dir):
            return events

        for filename in sorted(os.listdir(self.gray_data_dir)):
            if not filename.startswith(release_id) or not filename.endswith("_monitor.json"):
                continue
            path = os.path.join(self.gray_data_dir, filename)
            with open(path, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    continue
                if data.get("circuit_break_event"):
                    cb = data["circuit_break_event"]
                    events.append({
                        "port_name": data.get("port_name"),
                        "reason": cb.get("reason"),
                        "trigger_value": cb.get("trigger_value"),
                        "threshold": cb.get("threshold"),
                        "triggered_at": cb.get("triggered_at", ""),
                        "rollback_version": cb.get("rollback_version", ""),
                        "rollback_completed_at": cb.get("rollback_completed_at", ""),
                        "affected_ports": cb.get("affected_ports", []),
                        "details": cb.get("details", ""),
                    })
        return events

    def export_release_history(
        self,
        output_path: str,
        version: Optional[str] = None,
        port: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        release_id: Optional[str] = None,
    ) -> str:
        records = self.query_release_history(version, port, start_date, end_date, release_id)

        if output_path.endswith(".json"):
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            logger.info("审计记录已导出到 JSON: %s", output_path)
            return output_path
        elif output_path.endswith(".csv"):
            with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "发布ID", "版本", "上一版本", "发布类型", "状态",
                    "申请人", "申请时间", "完成时间", "紧急原因",
                    "口岸", "发布层级", "发布时间", "熔断原因", "熔断触发值",
                    "熔断阈值", "熔断时间", "回滚版本", "回滚完成时间",
                ])
                for rec in records:
                    if rec.get("port_records"):
                        for pr in rec["port_records"]:
                            cb = pr.get("circuit_break", {})
                            writer.writerow([
                                rec.get("release_id"),
                                rec.get("version"),
                                rec.get("previous_version"),
                                rec.get("release_type"),
                                rec.get("status"),
                                rec.get("applicant"),
                                rec.get("apply_time"),
                                rec.get("finish_time"),
                                rec.get("emergency_reason", ""),
                                pr.get("port_name"),
                                pr.get("tier"),
                                pr.get("deploy_started_at"),
                                cb.get("reason", ""),
                                cb.get("trigger_value", ""),
                                cb.get("threshold", ""),
                                cb.get("triggered_at", ""),
                                cb.get("rollback_version", ""),
                                cb.get("rollback_completed_at", ""),
                            ])
                    else:
                        writer.writerow([
                            rec.get("release_id"),
                            rec.get("version"),
                            rec.get("previous_version"),
                            rec.get("release_type"),
                            rec.get("status"),
                            rec.get("applicant"),
                            rec.get("apply_time"),
                            rec.get("finish_time"),
                            rec.get("emergency_reason", ""),
                            "", "", "", "", "", "", "", "", "",
                        ])
            logger.info("审计记录已导出到 CSV: %s", output_path)
            return output_path
        else:
            raise ValueError("仅支持导出为 .json 或 .csv 格式")

    def verify_integrity(self, date_str: Optional[str] = None) -> dict:
        files_to_check = []
        if date_str:
            path = os.path.join(self.data_dir, f"audit_{date_str}.jsonl")
            if os.path.exists(path):
                files_to_check.append(path)
        else:
            if os.path.exists(self.data_dir):
                for filename in sorted(os.listdir(self.data_dir)):
                    if filename.startswith("audit_") and filename.endswith(".jsonl"):
                        files_to_check.append(os.path.join(self.data_dir, filename))

        result = {"total_entries": 0, "tampered": 0, "files_checked": len(files_to_check)}
        for path in files_to_check:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        result["tampered"] += 1
                        continue
                    result["total_entries"] += 1
                    original_hash = entry.pop("hash", "")
                    computed_hash = self._compute_hash(entry)
                    if original_hash != computed_hash:
                        result["tampered"] += 1
        return result

    def _compute_hash(self, entry: dict) -> str:
        canonical = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
