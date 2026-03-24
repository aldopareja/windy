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
class TrackedSpaceState:
    workflow_space: EligibleWorkflowSpace
    leader_window_id: int
    background_window_ids: List[int]
    pending_split_direction: Optional[str]


@dataclass(frozen=True)
class AltTabSession:
    origin_window_id: int
    origin_workflow_space: EligibleWorkflowSpace


@dataclass(frozen=True)
class FocusGuard:
    workflow_space: EligibleWorkflowSpace
    target_window_id: Optional[int]


@dataclass(frozen=True)
class RuntimeState:
    spaces: Dict[str, TrackedSpaceState]
    alttab_session: Optional[AltTabSession]
    focus_guard: Optional[FocusGuard]

    @staticmethod
    def empty() -> "RuntimeState":
        return RuntimeState(
            spaces={},
            alttab_session=None,
            focus_guard=None,
        )
