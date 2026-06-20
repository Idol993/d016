from __future__ import annotations

import enum
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional


class ReleaseType(enum.Enum):
    REGULAR = "regular"
    HOTFIX = "hotfix"


class ReleaseStatus(enum.Enum):
    PENDING_CHECK = "pending_check"
    CHECK_FAILED = "check_failed"
    CHECK_PASSED = "check_passed"
    PENDING_APPROVAL = "pending_approval"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_PASSED = "approval_passed"
    GRAY_DEPLOYING = "gray_deploying"
    GRAY_MONITORING = "gray_monitoring"
    FULL_RELEASED = "full_released"
    CIRCUIT_BREAK = "circuit_break"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class ApprovalRole(enum.Enum):
    CUSTOMS = "customs"
    OPERATIONS = "operations"
    FINANCE = "finance"
    TECH = "tech"


class ApprovalStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    POST_SIGNED = "post_signed"


class CircuitBreakReason(enum.Enum):
    DECLARATION_FAILURE_RATE = "declaration_failure_rate"
    CLEARANCE_DELAY_RATE = "clearance_delay_rate"
    MANIFEST_ANOMALY_RATE = "manifest_anomaly_rate"


class CheckItemStatus(enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class DrillStatus(enum.Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class HotfixMode(enum.Enum):
    PARALLEL = "parallel"
    POST_SIGN = "post_sign"


@dataclass
class CheckItem:
    name: str
    category: str
    status: CheckItemStatus = CheckItemStatus.SKIP
    value: Optional[float] = None
    threshold: Optional[float] = None
    message: str = ""
    suggestion: str = ""


@dataclass
class PreCheckResult:
    release_id: str
    check_items: list[CheckItem] = field(default_factory=list)
    passed: bool = False
    checked_at: str = ""

    def compute_passed(self) -> bool:
        self.passed = all(
            item.status == CheckItemStatus.PASS
            for item in self.check_items
            if item.status != CheckItemStatus.SKIP
        )
        return self.passed

    def get_failed_items(self) -> list[CheckItem]:
        return [i for i in self.check_items if i.status == CheckItemStatus.FAIL]


@dataclass
class ApprovalRecord:
    role: ApprovalRole
    approver: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    comment: str = ""
    approved_at: str = ""
    is_critical: bool = False


@dataclass
class ApprovalFlow:
    release_id: str
    release_type: ReleaseType
    records: list[ApprovalRecord] = field(default_factory=list)
    current_step: int = 0
    reason: str = ""
    hotfix_mode: Optional[HotfixMode] = None
    hotfix_critical_roles: list[ApprovalRole] = field(default_factory=list)
    post_sign_deadline: str = ""

    def is_fully_approved(self) -> bool:
        return all(
            r.status in (ApprovalStatus.APPROVED, ApprovalStatus.SKIPPED, ApprovalStatus.POST_SIGNED)
            for r in self.records
        )

    def is_critical_approved(self) -> bool:
        for r in self.records:
            if r.is_critical and r.status not in (ApprovalStatus.APPROVED, ApprovalStatus.POST_SIGNED):
                return False
        return True

    def can_enter_gray(self) -> bool:
        if self.release_type == ReleaseType.REGULAR:
            return self.is_fully_approved()
        else:
            if self.hotfix_mode == HotfixMode.PARALLEL:
                return self.is_critical_approved()
            elif self.hotfix_mode == HotfixMode.POST_SIGN:
                return True
            else:
                return self.is_fully_approved()

    def get_current_pending_role(self) -> Optional[ApprovalRole]:
        for record in self.records:
            if record.status == ApprovalStatus.PENDING:
                return record.role
        return None

    def get_pending_roles(self) -> list[ApprovalRole]:
        return [r.role for r in self.records if r.status == ApprovalStatus.PENDING]

    def get_roles_needing_post_sign(self) -> list[ApprovalRole]:
        return [r.role for r in self.records if r.status == ApprovalStatus.SKIPPED]


@dataclass
class PortGrayConfig:
    port_name: str
    tier: int
    traffic_ratio: float
    is_core: bool


@dataclass
class MonitoringSnapshot:
    timestamp: str
    port_name: str
    declaration_failure_rate: float
    clearance_delay_rate: float
    manifest_anomaly_rate: float


@dataclass
class CircuitBreakEvent:
    release_id: str
    reason: CircuitBreakReason
    trigger_value: float
    threshold: float
    affected_ports: list[str]
    rollback_version: str
    version: str = ""
    triggered_at: str = ""
    rollback_completed_at: str = ""
    details: str = ""


@dataclass
class PortReleaseRecord:
    release_id: str
    port_name: str
    tier: int
    version: str
    previous_version: str
    deploy_started_at: str = ""
    deploy_completed_at: str = ""
    monitoring_started_at: str = ""
    monitoring_ended_at: str = ""
    circuit_break: Optional[CircuitBreakEvent] = None
    rollback_completed_at: str = ""
    status: str = ""


@dataclass
class ReleaseRecord:
    release_id: str
    version: str
    previous_version: str
    release_type: ReleaseType
    status: ReleaseStatus
    applicant: str
    apply_time: str
    emergency_reason: str = ""
    pre_check_result: Optional[PreCheckResult] = None
    approval_flow: Optional[ApprovalFlow] = None
    gray_configs: list[PortGrayConfig] = field(default_factory=list)
    monitoring_snapshots: list[MonitoringSnapshot] = field(default_factory=list)
    circuit_break_events: list[CircuitBreakEvent] = field(default_factory=list)
    port_records: list[PortReleaseRecord] = field(default_factory=list)
    approval_first_at: str = ""
    approval_last_at: str = ""
    approval_duration_minutes: float = 0.0
    port_results: dict[str, bool] = field(default_factory=dict)
    finish_time: str = ""


@dataclass
class DrillRecord:
    drill_id: str
    scheduled_at: str
    executed_at: str = ""
    completed_at: str = ""
    status: DrillStatus = DrillStatus.SCHEDULED
    target_version: str = ""
    rollback_version: str = ""
    duration_seconds: float = 0.0
    result_detail: str = ""


@dataclass
class WeeklyReport:
    report_id: str
    period_start: str
    period_end: str
    total_releases: int = 0
    success_releases: int = 0
    rollback_count: int = 0
    avg_approval_duration_minutes: float = 0.0
    approval_duration_list: list[float] = field(default_factory=list)
    failure_rate_by_port: dict[str, float] = field(default_factory=dict)
    trend_data: list[dict] = field(default_factory=list)
    file_paths: dict[str, str] = field(default_factory=dict)


def generate_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def parse_iso(iso_str: str) -> datetime:
    return datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S")
