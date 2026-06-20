from __future__ import annotations

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
        self.data_dir = os.path.join(config.get("system", {}).get("data_dir", "./data"), "audit")
        self.immutable = self.config.get("immutable", True)
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
