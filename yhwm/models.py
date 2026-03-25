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
class ManagedTile:
    tile_id: int
    visible_window_id: int
    hidden_window_ids: List[int]


@dataclass(frozen=True)
class PendingSplit:
    tile_id: int
    direction: str


@dataclass(frozen=True)
class ManagedSpaceState:
    workflow_space: EligibleWorkflowSpace
    tiles: Dict[int, ManagedTile]
    last_focused_tile_id: int
    next_tile_id: int
    pending_split: Optional[PendingSplit]


@dataclass(frozen=True)
class AltTabSession:
    origin_window_id: int
    origin_tile_id: int
    origin_workflow_space: EligibleWorkflowSpace


@dataclass(frozen=True)
class FocusGuard:
    workflow_space: EligibleWorkflowSpace
    target_window_id: Optional[int]


@dataclass(frozen=True)
class RuntimeState:
    spaces: Dict[str, ManagedSpaceState]
    alttab_session: Optional[AltTabSession]
    focus_guard: Optional[FocusGuard]

    @staticmethod
    def empty() -> "RuntimeState":
        return RuntimeState(
            spaces={},
            alttab_session=None,
            focus_guard=None,
        )
