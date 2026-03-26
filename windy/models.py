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
class NormalizedFrame:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class PendingSplit:
    direction: str
    anchor_window_id: int
    anchor_frame: NormalizedFrame


@dataclass(frozen=True)
class TrackedSpaceState:
    workflow_space: EligibleWorkflowSpace
    pending_split: Optional[PendingSplit]


@dataclass(frozen=True)
class RuntimeState:
    spaces: Dict[str, TrackedSpaceState]

    @staticmethod
    def empty() -> "RuntimeState":
        return RuntimeState(spaces={})


@dataclass(frozen=True)
class LiveTile:
    frame: NormalizedFrame
    visible_window_id: int
    background_window_ids: List[int]

    @property
    def all_window_ids(self) -> List[int]:
        return [self.visible_window_id, *self.background_window_ids]
