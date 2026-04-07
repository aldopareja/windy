# Window Creation Handling v3 — Design Spec

Date: 2026-04-07

## Problem

Two confirmed bugs in windy's window management:

1. **Zen Browser ghost windows:** yabai captures windows before Accessibility (AX) properties are ready, creating ghost entries with empty role/subrole and `has-ax-reference=False`. These ghosts are unmanaged and don't stack with other windows, creating extra tiles.

2. **Unwanted BSP splits on cmd+N:** When any application creates a new window via cmd+N, yabai auto-splits the focused tile in BSP mode. The new window gets its own tile instead of stacking into the existing tile.

Both bugs cause the tile count to increase unexpectedly and break the user's intended layout.

## Prior Art

**v1** used `--insert stack` arming + Hammerspoon `hs.window.filter` watcher. Absorption worked but `--insert stack` produces a persistent red visual overlay from yabai's `insert_feedback_show()` (view.c:8-102). No yabai config exists to suppress this overlay selectively.

**v2** used yabai `window_created` signals as the detection mechanism, removing `--insert stack` entirely. Signals are unreliable for windows created via keyboard shortcuts (cmd+N) on already-running applications — confirmed live with multiple signal types and 15-second waits.

See `docs/observations/2026-04-07-window-creation-live-probes.md` for full observations.

## Philosophy Alignment

From style_v2.md:

- "prefer fresh observation at action time over continuously maintaining a shadow model"
- "avoid signal-driven bookkeeping"
- "explicit actions matter more than implicit heuristics"

v3 removes yabai signal plumbing entirely and uses a single Hammerspoon-based detection path with fresh live queries at action time.

## Architecture

```
[any window created on macOS]
  -> hs.window.filter fires callback (~100ms)
  -> 500ms delay (hs.timer.doAfter)
  -> windy on-window-created --window-id <id>
      -> _rediscover_and_query (retry loop for ghost windows)
      -> check tracked space + eligibility
      -> live snapshot + reconcile pending_split
      -> absorb into anchor tile (if not intentional split)
```

Single detection path. No fallbacks. No yabai signals.

## Detection: Hammerspoon windowFilter

A global `hs.window.filter` runs continuously after `module.start()` in `windy.lua`.

**Scope:** Unscoped — watches all window creation events on the system. All filtering (tracked space, eligibility, BSP layout) happens in Python via `on_window_created`.

**Callback:** Gets window ID from `hs.window:id()`, nil-checks both the window object and the ID, waits 500ms via `hs.timer.doAfter`, then calls `windy on-window-created --window-id <id>` using the existing `runWindy` function (non-blocking via `hs.task.new`).

**Intelligence:** None in Lua. The watcher is intentionally dumb. For untracked spaces, `on_window_created` checks tracked state and exits early — the cost is one short-lived Python process.

**Lifecycle:** Started in `module.start()`, cleaned up in `stopExisting()`. Not tied to reseed or float — always running while windy is loaded.

### 500ms Delay Rationale

- Zen Browser AX initialization takes ~500ms (confirmed live)
- Normal apps have AX ready immediately; 500ms visual flicker before absorption is acceptable
- The retry loop in `_rediscover_and_query` (5 attempts x 200ms = 1s max) acts as a safety net for extremely slow AX initialization
- Total worst case before giving up: 500ms + 1000ms = 1.5s

## Absorption: on_window_created

The existing `on_window_created(window_id)` in `workflow.py` handles absorption. This logic is unchanged from v2.

### _rediscover_and_query

Retry loop: 5 attempts, 200ms sleep between each.

Each attempt:
1. Call yabai `--rediscover <window_id>` — our patch that makes ghost windows manageable by re-scanning the application's AX window list
2. Query the window from yabai
3. If `has-ax-reference` is not False: return the window record
4. Otherwise: sleep 200ms and retry

Returns None after 5 failed attempts. `on_window_created` exits early.

For normal apps (Warp, Cursor, etc.), the first attempt succeeds immediately — `--rediscover` is a no-op for already-known windows. For Zen Browser, 2-3 retries may be needed.

### Absorption Logic

1. Derive workflow space from window record — exit if not derivable
2. Check tracked state — exit if space not tracked by windy
3. Validate workflow space layout is BSP — exit if invalid
4. Check eligibility (role, subrole, can-move, has-ax-reference, etc.) — exit if not eligible
5. Take live snapshot: fresh query of all tiles and window z-ordering
6. Reconcile pending_split: if the anchor window's frame changed since the split was armed, the pending_split was consumed by this window (intentional split)
7. Find anchor tile: `max(tiles, key=len(all_window_ids))` — the tile with the most windows
8. Find new window's tile via `tile_for_window(window_id)`
9. Decision:
   - Same frame as anchor tile: already stacked, no-op
   - pending_split consumed: intentional split from user's split action, no-op
   - Otherwise: `stack(anchor.visible_window_id, window_id)` + `focus(window_id)`

### Key Properties

**Window-level absorption:** Only stacks `window_id` into the anchor tile. Never touches other solo tiles. This preserves intentional splits — after the user presses ctrl+alt+v (split), the promoted window stays in its own tile.

**Anchor by max window count:** The anchor tile is the one with the most windows, not the focused window. This avoids the focus-shift bug: macOS focuses new windows immediately, so querying the focused window in `on_window_created` returns the NEW window, not the original anchor.

**Idempotent:** Safe to call twice for the same window. The second call sees the window already in the anchor tile (`new_tile.frame == anchor_tile.frame`) and exits.

**Concurrent-safe:** Multiple `windy on-window-created` processes can run simultaneously (e.g., bulk app launch). Each takes its own fresh snapshot and absorbs only its own `window_id`. No shared mutable state between processes.

## yabai Patch: --rediscover

The existing 16-line patch in `message.c` (lines 2261-2273). No changes needed.

```
yabai -m window --rediscover <window_server_id>
```

Handler:
1. Gets PID from window server ID via `SLSGetWindowOwner` and `SLSConnectionGetPID`
2. Finds application in yabai's table via `window_manager_find_application`
3. If application found AND window NOT in yabai's window table: calls `window_manager_add_existing_application_windows` (re-scans all AX windows for the application)
4. Returns success if window is now in table, failure otherwise

**Side effect:** Re-scanning all app windows may fix other ghost windows from the same application. This is harmless — each ghost that gets added to BSP will trigger its own Hammerspoon `windowCreated` callback and be absorbed independently.

**Integration quality:** The patch follows yabai's existing command dispatch pattern, uses only existing internal functions, and is properly integrated into the null-acting-window check at line 2058. No new internal APIs introduced.

## Code Changes

### Deleted

**workflow.py:**
- `WorkflowRuntime.__init__`: remove `windy_bin` parameter and `self._windy_bin` field
- `reseed()`: remove the try/except `remove_signal("windy_absorb")` and `add_signal(...)` block (lines 115-123)
- `float_space()`: remove the `remove_signal("windy_absorb")` block on last tracked space (lines 217-221)

**yabai.py:**
- `YabaiClient` Protocol: remove `add_signal(label, event, action)` and `remove_signal(label)` method signatures
- `SubprocessYabaiClient`: remove `add_signal()` and `remove_signal()` implementations

**cli.py:**
- Remove `windy_bin` computation (line 124: `windy_bin = str(Path(...))`)
- Remove `windy_bin=windy_bin` from WorkflowRuntime constructor call

**tests/test_windy.py:**
- `test_reseed_tracks_space_and_stacks_other_windows`: remove `("remove_signal", "windy_absorb")` and `("add_signal", ...)` from expected actions
- Delete `test_reseed_registers_absorb_signal` (lines 201-224)
- `test_float_clears_tracking`: remove `("remove_signal", "windy_absorb")` from expected actions
- Delete `test_float_removes_signal_when_last_tracked_space` (lines 280-299)
- Remove `windy_bin="/test/bin/windy"` from all WorkflowRuntime constructor calls
- `FakeYabaiClient`: remove `add_signal()` and `remove_signal()` methods

### Added

**windy.lua:**
- `stopExisting()`: if `runtimeState.windowFilter` exists, call `unsubscribeAll()` on it and set to nil
- `module.start()`: create `hs.window.filter.new()`, subscribe to `windowCreated` with callback that nil-checks window/ID, waits 500ms, calls `runWindy(state, {"on-window-created", "--window-id", tostring(windowId)})`
- Store filter reference in `state.windowFilter`

### Unchanged

- `on_window_created()`, `_rediscover_and_query()`, `_find_anchor_tile()` in workflow.py
- `rediscover_window()` in yabai.py
- `on-window-created` CLI subcommand and dispatch in cli.py
- All 7 on_window_created unit tests
- yabai `--rediscover` patch (message.c)
- All hotkey bindings and alt-tab logic in windy.lua

## Testing

### Unit Tests

Existing on_window_created tests cover all absorption scenarios. No new unit tests needed since the absorption logic is unchanged.

**Kept (7 tests):**
- `test_on_window_created_absorbs_solo_tile`
- `test_on_window_created_absorbs_only_triggering_window`
- `test_on_window_created_idempotent_when_no_solo_tiles`
- `test_on_window_created_preserves_intentional_split`
- `test_on_window_created_exits_for_untracked_space`
- `test_on_window_created_exits_when_window_destroyed`
- `test_on_window_created_command_dispatches_runtime`

**Deleted (2 tests):**
- `test_reseed_registers_absorb_signal`
- `test_float_removes_signal_when_last_tracked_space`

**Modified (2 tests):**
- `test_reseed_tracks_space_and_stacks_other_windows` — remove signal actions from expected list
- `test_float_clears_tracking` — remove signal action from expected list

### Live Verification

After implementation, verify on the live system (wait 10s after reseed for completion):

1. Reseed a space containing Zen + terminal windows
2. Focus Zen window, press cmd+N — new Zen window should absorb into anchor tile (not float, not create new split)
3. Focus terminal, press cmd+N — new terminal window should absorb (not create new split)
4. Split east (ctrl+alt+v), then cmd+N — new tile should be preserved (intentional split)
5. Open a new app (`open -na TextEdit`) — new window should absorb
6. Create a window in an untracked space — no absorption, no error

## Rejected Alternatives

1. **yabai `window_created` signals as detection:** Unreliable for cmd+N on running applications. Confirmed live: signals never fired for `osascript cmd+N` on Zen, and fired inconsistently on Warp. Cannot be the sole or primary detection mechanism.

2. **`--insert stack` arming:** Creates persistent red visual overlay via `insert_feedback_show()` in yabai's view.c. No yabai config exists to suppress this selectively. Suppressing for STACK direction would mean adding a special case to the yabai codebase.

3. **Space-level absorption (absorb all solo tiles):** Too aggressive — after an intentional split (user presses ctrl+alt+v), the promoted window's tile has 1 window. Space-level absorption treats it as unwanted and absorbs it back, destroying the intentional split.

4. **Anchor by focused/recent window:** macOS focuses new windows immediately. Querying the focused window inside `on_window_created` returns the NEW window. Using it as anchor causes self-comparison ("already stacked") and the absorption is skipped.

5. **Hybrid detection (Hammerspoon + signals):** Adds complexity for no observed benefit. Hammerspoon's `hs.window.filter` reliably fires for all window creation methods tested. Adding signals as a secondary path contradicts style_v2's preference for simplicity and introduces the exact signal-driven bookkeeping the philosophy warns against.
