from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class EligibleWorkflowSpace:
    display: int
    space: int

    @property
    def storage_key(self) -> str:
        return f"{self.display}:{self.space}"


@dataclass(frozen=True)
class CollapseResult:
    workflow_space: EligibleWorkflowSpace
    focused_window_id: int
    background_window_ids: List[int]

    def to_state_payload(self) -> Dict[str, object]:
        return {
            "display": self.workflow_space.display,
            "space": self.workflow_space.space,
            "visible_window_id": self.focused_window_id,
            "background_window_ids": list(self.background_window_ids),
        }


@dataclass(frozen=True)
class SplitResult:
    workflow_space: EligibleWorkflowSpace
    focused_window_id: int
    promoted_window_id: Optional[int]
    background_window_ids: List[int]
    pending_split_direction: Optional[str]


@dataclass(frozen=True)
class WorkflowSpaceState:
    workflow_space: EligibleWorkflowSpace
    visible_window_id: int
    background_window_ids: List[int]
    pending_split_direction: Optional[str]


@dataclass(frozen=True)
class WindowCreatedResult:
    created_window_id: int
    workflow_space: Optional[EligibleWorkflowSpace]
    action: str
    visible_window_id: Optional[int]
    background_window_ids: List[int]


@dataclass(frozen=True)
class WindowFocusedResult:
    focused_window_id: int
    workflow_space: Optional[EligibleWorkflowSpace]
    action: str
    visible_window_id: Optional[int]
    background_window_ids: List[int]
    pending_split_direction: Optional[str]


@dataclass(frozen=True)
class BackgroundWindowExitCleanupResult:
    window_id: int
    event: str
    workflow_space: Optional[EligibleWorkflowSpace]
    action: str
    visible_window_id: Optional[int]
    background_window_ids: List[int]
    pending_split_direction: Optional[str]


@dataclass(frozen=True)
class BackgroundWindowReturnAsNewResult:
    window_id: int
    event: str
    workflow_space: Optional[EligibleWorkflowSpace]
    action: str
    visible_window_id: Optional[int]
    background_window_ids: List[int]
    pending_split_direction: Optional[str]


@dataclass(frozen=True)
class TrackedVisibleWindowExitRecoveryResult:
    window_id: int
    event: str
    workflow_space: Optional[EligibleWorkflowSpace]
    action: str
    visible_window_id: Optional[int]
    background_window_ids: List[int]
    pending_split_direction: Optional[str]


@dataclass(frozen=True)
class ArmedAltTabSession:
    origin_window_id: int
    origin_workflow_space: EligibleWorkflowSpace


@dataclass(frozen=True)
class AltTabFocusGuard:
    workflow_space: EligibleWorkflowSpace
    target_window_id: Optional[int]


@dataclass(frozen=True)
class AltTabSessionArmResult:
    workflow_space: Optional[EligibleWorkflowSpace]
    origin_window_id: Optional[int]
    action: str
    session_active: bool


@dataclass(frozen=True)
class AltTabSessionCancelResult:
    workflow_space: Optional[EligibleWorkflowSpace]
    origin_window_id: Optional[int]
    selected_window_id: Optional[int]
    reason: str
    action: str
    session_active: bool
