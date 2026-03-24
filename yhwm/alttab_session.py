from __future__ import annotations

from .current_space import (
    derive_workflow_space_from_window,
    query_eligible_windows,
    query_focused_window_record,
    query_window_record,
    validate_workflow_space,
)
from .eligibility import is_eligible_window
from .models import (
    AltTabFocusGuard,
    AltTabModifierReleaseResult,
    AltTabSessionArmResult,
    AltTabSessionCancelResult,
    ArmedAltTabSession,
)
from .state import AltTabSessionStore, WorkflowStateStore
from .yabai import YabaiClient

SUPPORTED_ALTTAB_CANCEL_REASONS = frozenset(
    {
        "chooser_close",
        "chooser_hide",
        "chooser_minimize",
        "chooser_quit",
        "esc",
        "space",
        "thumbnail_click",
    }
)


class AltTabSessionArmService:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        state_store: WorkflowStateStore,
        session_store: AltTabSessionStore,
    ):
        self._yabai = yabai
        self._state_store = state_store
        self._session_store = session_store

    def run(self) -> AltTabSessionArmResult:
        armed_session = self._session_store.read_session()
        if armed_session is not None:
            return AltTabSessionArmResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                action="ignored_existing_session",
                session_active=True,
            )

        focused_window = query_focused_window_record(
            self._yabai,
            description="focused window",
        )
        focused_window_id = int(focused_window["id"])
        workflow_space = derive_workflow_space_from_window(
            focused_window,
            description="focused window",
        )
        if not is_eligible_window(
            focused_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return AltTabSessionArmResult(
                workflow_space=workflow_space,
                origin_window_id=focused_window_id,
                action="ignored_ineligible_origin",
                session_active=False,
            )

        persisted_space_state = self._state_store.read_space_state(workflow_space)
        if persisted_space_state is None:
            return AltTabSessionArmResult(
                workflow_space=workflow_space,
                origin_window_id=focused_window_id,
                action="ignored_untracked_space",
                session_active=False,
            )

        if focused_window_id in persisted_space_state.background_window_ids:
            return AltTabSessionArmResult(
                workflow_space=workflow_space,
                origin_window_id=focused_window_id,
                action="ignored_background_window",
                session_active=False,
            )

        if focused_window_id != persisted_space_state.visible_window_id:
            return AltTabSessionArmResult(
                workflow_space=workflow_space,
                origin_window_id=focused_window_id,
                action="ignored_untracked_visible_window",
                session_active=False,
            )

        validate_workflow_space(
            self._yabai,
            workflow_space=workflow_space,
            allowed_layouts=("bsp",),
        )
        self._session_store.arm_session(
            ArmedAltTabSession(
                origin_window_id=focused_window_id,
                origin_workflow_space=workflow_space,
                selected_window_id=focused_window_id,
            )
        )
        return AltTabSessionArmResult(
            workflow_space=workflow_space,
            origin_window_id=focused_window_id,
            action="armed_session",
            session_active=True,
        )


class AltTabSessionCancelService:
    def __init__(
        self,
        *,
        session_store: AltTabSessionStore,
        reason: str,
        selected_window_id: int | None = None,
    ):
        self._session_store = session_store
        self._reason = reason
        self._selected_window_id = selected_window_id

    def run(self) -> AltTabSessionCancelResult:
        armed_session = self._session_store.read_session()
        if armed_session is None:
            return AltTabSessionCancelResult(
                workflow_space=None,
                origin_window_id=None,
                selected_window_id=None,
                reason=self._reason,
                action="ignored_no_armed_session",
                session_active=False,
            )

        focus_guard = None
        if self._reason == "thumbnail_click":
            focus_guard = AltTabFocusGuard(
                workflow_space=armed_session.origin_workflow_space,
                target_window_id=self._selected_window_id,
            )
        self._session_store.disarm_session(focus_guard=focus_guard)
        return AltTabSessionCancelResult(
            workflow_space=armed_session.origin_workflow_space,
            origin_window_id=armed_session.origin_window_id,
            selected_window_id=self._selected_window_id,
            reason=self._reason,
            action="canceled_session",
            session_active=False,
        )


class AltTabSelectedWindowService:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        session_store: AltTabSessionStore,
        selected_window_id: int,
    ):
        self._yabai = yabai
        self._session_store = session_store
        self._selected_window_id = selected_window_id

    def run(self) -> AltTabSessionCancelResult:
        armed_session = self._session_store.read_session()
        if armed_session is None:
            return AltTabSessionCancelResult(
                workflow_space=None,
                origin_window_id=None,
                selected_window_id=self._selected_window_id,
                reason="selected_window",
                action="ignored_no_armed_session",
                session_active=False,
            )

        selected_window = query_window_record(
            self._yabai,
            window_id=self._selected_window_id,
            description=f"selected window {self._selected_window_id}",
        )
        selected_workflow_space = derive_workflow_space_from_window(
            selected_window,
            description=f"selected window {self._selected_window_id}",
        )
        if selected_workflow_space != armed_session.origin_workflow_space:
            self._session_store.disarm_session()
            return AltTabSessionCancelResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=self._selected_window_id,
                reason="selected_window",
                action="canceled_cross_space_or_display_selection",
                session_active=False,
            )

        self._session_store.arm_session(
            ArmedAltTabSession(
                origin_window_id=armed_session.origin_window_id,
                origin_workflow_space=armed_session.origin_workflow_space,
                selected_window_id=self._selected_window_id,
            )
        )
        return AltTabSessionCancelResult(
            workflow_space=armed_session.origin_workflow_space,
            origin_window_id=armed_session.origin_window_id,
            selected_window_id=self._selected_window_id,
            reason="selected_window",
            action="ignored_same_origin_space_selection",
            session_active=True,
        )


class AltTabModifierReleaseService:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        state_store: WorkflowStateStore,
        session_store: AltTabSessionStore,
    ):
        self._yabai = yabai
        self._state_store = state_store
        self._session_store = session_store

    def run(self) -> AltTabModifierReleaseResult:
        armed_session = self._session_store.read_session()
        if armed_session is None:
            return AltTabModifierReleaseResult(
                workflow_space=None,
                origin_window_id=None,
                selected_window_id=None,
                action="ignored_no_armed_session",
                visible_window_id=None,
                background_window_ids=[],
                pending_split_direction=None,
                session_active=False,
            )

        selected_window_id = (
            armed_session.selected_window_id or armed_session.origin_window_id
        )
        if selected_window_id == armed_session.origin_window_id:
            self._session_store.disarm_session()
            return AltTabModifierReleaseResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=selected_window_id,
                action="canceled_origin_still_selected",
                visible_window_id=armed_session.origin_window_id,
                background_window_ids=[],
                pending_split_direction=None,
                session_active=False,
            )

        persisted_space_state = self._state_store.read_space_state(
            armed_session.origin_workflow_space
        )
        if persisted_space_state is None:
            self._session_store.disarm_session()
            return AltTabModifierReleaseResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=selected_window_id,
                action="ignored_untracked_space",
                visible_window_id=None,
                background_window_ids=[],
                pending_split_direction=None,
                session_active=False,
            )

        validate_workflow_space(
            self._yabai,
            workflow_space=armed_session.origin_workflow_space,
            allowed_layouts=("bsp",),
        )
        origin_window = query_window_record(
            self._yabai,
            window_id=armed_session.origin_window_id,
            description=f"origin window {armed_session.origin_window_id}",
        )
        origin_workflow_space = derive_workflow_space_from_window(
            origin_window,
            description=f"origin window {armed_session.origin_window_id}",
        )
        if origin_workflow_space != armed_session.origin_workflow_space or not is_eligible_window(
            origin_window,
            target_display=armed_session.origin_workflow_space.display,
            target_space=armed_session.origin_workflow_space.space,
        ):
            self._session_store.disarm_session()
            return AltTabModifierReleaseResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=selected_window_id,
                action="canceled_ineligible_origin_window",
                visible_window_id=persisted_space_state.visible_window_id,
                background_window_ids=list(persisted_space_state.background_window_ids),
                pending_split_direction=persisted_space_state.pending_split_direction,
                session_active=False,
            )

        selected_window = query_window_record(
            self._yabai,
            window_id=selected_window_id,
            description=f"selected window {selected_window_id}",
        )
        selected_workflow_space = derive_workflow_space_from_window(
            selected_window,
            description=f"selected window {selected_window_id}",
        )
        if selected_workflow_space != armed_session.origin_workflow_space:
            self._session_store.disarm_session()
            return AltTabModifierReleaseResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=selected_window_id,
                action="canceled_cross_space_or_display_selection",
                visible_window_id=persisted_space_state.visible_window_id,
                background_window_ids=list(persisted_space_state.background_window_ids),
                pending_split_direction=persisted_space_state.pending_split_direction,
                session_active=False,
            )

        if not is_eligible_window(
            selected_window,
            target_display=armed_session.origin_workflow_space.display,
            target_space=armed_session.origin_workflow_space.space,
        ):
            self._session_store.disarm_session()
            return AltTabModifierReleaseResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=selected_window_id,
                action="canceled_ineligible_selected_window",
                visible_window_id=persisted_space_state.visible_window_id,
                background_window_ids=list(persisted_space_state.background_window_ids),
                pending_split_direction=persisted_space_state.pending_split_direction,
                session_active=False,
            )

        eligible_window_ids = {
            window["id"]
            for window in query_eligible_windows(
                self._yabai,
                workflow_space=armed_session.origin_workflow_space,
            )
        }
        if armed_session.origin_window_id not in eligible_window_ids:
            self._session_store.disarm_session()
            return AltTabModifierReleaseResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=selected_window_id,
                action="canceled_missing_origin_window",
                visible_window_id=persisted_space_state.visible_window_id,
                background_window_ids=list(persisted_space_state.background_window_ids),
                pending_split_direction=persisted_space_state.pending_split_direction,
                session_active=False,
            )

        if selected_window_id not in eligible_window_ids:
            self._session_store.disarm_session()
            return AltTabModifierReleaseResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=selected_window_id,
                action="canceled_missing_selected_window",
                visible_window_id=persisted_space_state.visible_window_id,
                background_window_ids=list(persisted_space_state.background_window_ids),
                pending_split_direction=persisted_space_state.pending_split_direction,
                session_active=False,
            )

        if selected_window_id in persisted_space_state.background_window_ids:
            refreshed_background_window_ids = [
                window_id
                for window_id in persisted_space_state.background_window_ids
                if window_id != selected_window_id
            ]
            if armed_session.origin_window_id not in refreshed_background_window_ids:
                refreshed_background_window_ids.append(armed_session.origin_window_id)
            prepared_state_payload = self._state_store.prepare_background_pool_payload(
                workflow_space=armed_session.origin_workflow_space,
                visible_window_id=selected_window_id,
                background_window_ids=refreshed_background_window_ids,
                pending_split_direction=persisted_space_state.pending_split_direction,
            )
            self._yabai.focus_window(selected_window_id)
            self._state_store.write_payload(prepared_state_payload)
            self._session_store.disarm_session()
            return AltTabModifierReleaseResult(
                workflow_space=armed_session.origin_workflow_space,
                origin_window_id=armed_session.origin_window_id,
                selected_window_id=selected_window_id,
                action="committed_background_window_replacement",
                visible_window_id=selected_window_id,
                background_window_ids=refreshed_background_window_ids,
                pending_split_direction=persisted_space_state.pending_split_direction,
                session_active=False,
            )

        prepared_state_payload = self._state_store.prepare_background_pool_payload(
            workflow_space=armed_session.origin_workflow_space,
            visible_window_id=selected_window_id,
            background_window_ids=persisted_space_state.background_window_ids,
            pending_split_direction=persisted_space_state.pending_split_direction,
        )
        self._yabai.swap_window(armed_session.origin_window_id, selected_window_id)
        self._yabai.focus_window(selected_window_id)
        self._state_store.write_payload(prepared_state_payload)
        self._session_store.disarm_session()
        return AltTabModifierReleaseResult(
            workflow_space=armed_session.origin_workflow_space,
            origin_window_id=armed_session.origin_window_id,
            selected_window_id=selected_window_id,
            action="committed_visible_window_swap",
            visible_window_id=selected_window_id,
            background_window_ids=list(persisted_space_state.background_window_ids),
            pending_split_direction=persisted_space_state.pending_split_direction,
            session_active=False,
        )
