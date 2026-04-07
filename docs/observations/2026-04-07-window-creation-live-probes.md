# Window Creation Handling — Live Probe Observations

Date: 2026-04-07
Session: Debugging and implementing window creation handling for windy (v1 and v2 attempts)

This document records every live observation made during the session. It is written as a handoff for a future session to design v3.

---

## 1. The Two Bugs (Confirmed and Reproduced)

### Bug 1: Zen Browser ghost windows

**Reproduction:**
1. Focus Zen Browser window (id=85649, `app=Zen`, `subrole=AXStandardWindow`)
2. Press cmd+N (via `osascript -e 'tell application "System Events" to keystroke "n" using command down'`)
3. A new window appears

**Observation — yabai vs Hammerspoon disagree on the new window:**

| Property | yabai view | Hammerspoon view |
|----------|-----------|-----------------|
| app | `zen` (lowercase) | `Zen` (capitalized) |
| role | empty | `AXWindow` |
| subrole | empty | `AXStandardWindow` |
| can-move | `False` | N/A |
| has-ax-reference | `False` | N/A (HS has full AX access) |
| title | empty | `Zen Browser` (correct page title) |

**Root cause from yabai source:** The window hits the `window_nonax_serialize` path in `window_manager.c:47-48`. When yabai first detects the window via the macOS window server, it tries to acquire an `AXUIElementRef`. For Zen Browser (Firefox-based), the AX tree isn't ready at detection time. yabai records the window with hardcoded empty values and never retries. The `app="zen"` (lowercase) comes from `proc_name()` (the raw process name), while the proper `app="Zen"` comes from `application->name` (the AX display name).

**Timing observation:** Hammerspoon can see proper AX properties within ~500ms of window creation. yabai's `--rediscover` command (our patch) succeeds at ~500ms. But yabai NEVER retries on its own.

**Pattern:** Every Zen Browser window creates TWO entries in yabai: one proper (`Zen`, AXStandardWindow) and one ghost (`zen`, empty subrole). Existing ghost windows from before the session had `has-ax-reference=False` even though Hammerspoon saw them as fully functional `AXWindow/AXStandardWindow` windows. The ghosts persist indefinitely.

### Bug 2: Terminal cmd+N creates unwanted BSP split

**Reproduction:**
1. Reseed space (ctrl+alt+space) — all windows stacked in 1 tile
2. Focus any Warp terminal window
3. Press cmd+N

**Observation:** yabai's BSP mode auto-splits the focused tile to accommodate the new window. The tile count goes from 1 to 2. The new window gets its own tile with half the width.

**Confirmed with data:** Before cmd+N: tile at `frame=8,1096,1271,1056` with `[Warp(90708), Finder(95880)]`. After cmd+N: two tiles — `frame=8,1096,632,1056` with `[Warp(90708), Finder(95880)]` and `frame=647,1096,632,1056` with `[Warp(99945)]`. Width split from 1271 to 632+632.

---

## 2. yabai `--rediscover` Patch

### What it does

Added `--rediscover <window_server_id>` to yabai's `message.c`. 16 lines of C. The handler:
1. Takes a macOS window server ID (integer)
2. Calls `SLSGetWindowOwner(g_connection, wid, &connection)` to find the owning connection
3. Calls `SLSConnectionGetPID(connection, &pid)` to get the PID
4. Calls `window_manager_find_application(&g_window_manager, pid)` to find the yabai application struct
5. If the window is NOT already in yabai's table: calls `window_manager_add_existing_application_windows(...)` which re-scans ALL windows for that application

### Key behavior confirmed live

- `--rediscover` on a valid ghost window ID: exit 0. Window becomes fully managed (`subrole=AXStandardWindow`, `has-ax-reference=True`).
- `--rediscover` on an invalid ID (999999): exit 1 with `could not rediscover window with id '999999'.`
- `--rediscover` on an already-known window: exit 0 (no-op, the rescan skips it).
- **Side effect:** `window_manager_add_existing_application_windows()` re-scans ALL windows for the application, not just the target. If Zen has other untracked ghost windows, they ALL get added.
- **Tiling:** Rediscovered windows ARE added to the BSP tree (they get their own tile/frame), creating additional splits. Confirmed: after rediscovering a Zen ghost, the tile count increased.
- **Timing:** `--rediscover` succeeds for Zen windows starting at ~500ms after creation (when AX becomes ready). Before that, the AX window list doesn't include the new window, so the rescan finds nothing.

### Build and deploy

```bash
cd github_and_docs_reference/yabai/yabai
make install  # builds optimized binary to ./bin/yabai
# Deploy:
chmod u+w /opt/homebrew/Cellar/yabai/7.1.17/bin/yabai
cp bin/yabai /opt/homebrew/Cellar/yabai/7.1.17/bin/yabai
chmod u-w /opt/homebrew/Cellar/yabai/7.1.17/bin/yabai
# yabai symlink at /opt/homebrew/bin/yabai points to Cellar
```

Note: yabai was installed via Homebrew as v7.1.17. The patched binary reports `v7.1.18` (version string comes from the source tree, not from Homebrew). Homebrew updates may overwrite the patch.

---

## 3. yabai `--insert stack` Behavior

### What it does

`yabai -m window <id> --insert stack` tells yabai: "the next window that arrives at this BSP node should be stacked, not split." It's a one-shot mechanism — consumed by the next window creation.

### Visual feedback problem (CRITICAL)

**Observation:** `--insert stack` triggers `insert_feedback_show()` (view.c:8-102) which creates a persistent SkyLight overlay window with the `insert_feedback_color` (default `0xffd75f5f` = red). For the `STACK` case specifically (view.c:78-83), the overlay draws a full border around the entire window — a very visible red rectangle.

**This overlay persists indefinitely** until:
- A new window arrives and consumes the insertion (the overlay is destroyed at view.c:773-776)
- The same `--insert stack` command is sent again (toggles it off, window_manager.c:1757-1763)
- The BSP node is destroyed

**Impact:** Using `--insert stack` programmatically after every reseed/split/delete_tile created a persistent red border that sat on screen until the next window was created. This was the primary reason for moving to v2 (signal-based approach).

**Confirmed:** No yabai config exists to disable the visual feedback. `insert_feedback_color` can be set to `0x00000000` (fully transparent) to hide it, but that also hides feedback for intentional user-triggered splits.

---

## 4. yabai Signal Behavior (CRITICAL FINDING)

### Signal timing from source

From `event_loop.c` WINDOW_CREATED handler:
1. Line 541: `window_manager_create_and_add_window()` — window struct created
2. Line 566: `space_manager_tile_window_on_space()` — BSP insertion (split happens here)
3. Line 571: `event_signal_push(SIGNAL_WINDOW_CREATED)` — signal queued
4. Line 1629: `event_signal_flush()` — signal actions dispatched via `fork`+`execvp("sh", "-c", action)`

Signals fire AFTER BSP insertion, on the same event loop iteration.

### Signal reliability observations

| Creation method | `window_created` signal fires? | Notes |
|----------------|-------------------------------|-------|
| `open -na Terminal` | YES | Fired within 2s |
| `open -a Finder ~/` | YES | Fired within 2s |
| `osascript -e '...keystroke "n"...'` on Warp | **INCONSISTENT** | Fired in early tests, stopped firing later in session |
| `osascript -e '...keystroke "n"...'` on Zen | **NO** | Never observed firing |
| `window_focused` signal | YES | Always reliable for all focus changes |

**Critical finding:** `window_created` signals are UNRELIABLE for windows created via keyboard shortcuts (`osascript cmd+N`) on already-running applications. They work for application launches (`open -na`) but not for in-app window creation via simulated keystrokes. This may be a yabai bug or a macOS notification ordering issue.

**Tested exhaustively:**
- Simple `echo` signal action: NO output after 15 seconds
- `touch /tmp/file` signal action: file NOT created
- `date >> /tmp/file` signal action: NO output
- Same tests with `window_focused` signal: ALL worked immediately

**Implication:** yabai signals CANNOT be the sole mechanism for detecting window creation from cmd+N. A Hammerspoon `hs.window.filter` watcher is needed as the primary detection mechanism for keyboard-triggered window creation.

---

## 5. Hammerspoon `hs.window.filter` Behavior

### Reliability

`hs.window.filter:subscribe(hs.window.filter.windowCreated, callback)` fires reliably for ALL window creation methods — keyboard shortcuts, application launches, and programmatic creation. It uses macOS window server notifications, not yabai's event system.

### Timing

Hammerspoon's `windowCreated` callback fires quickly (within ~100ms of window appearance). A `hs.timer.doAfter(delay, ...)` is used to add a delay before processing. The delay is needed to give AX time to initialize for apps like Zen Browser.

### Delay requirements

- **Normal apps (Warp, Cursor, etc.):** AX ready immediately. No delay needed for rediscovery (not needed at all — these windows are managed by yabai from creation).
- **Zen Browser:** AX ready at ~500ms. `--rediscover` succeeds at ~500ms.
- **Tested delays:** 300ms was insufficient (Zen AX not ready). 500ms worked for the Hammerspoon watcher in v1. A retry loop (5 attempts * 200ms = 1s max) in `on_window_created` was added but still failed for Zen when the signal fired too early.

---

## 6. Space-Level vs Window-Level Absorption

### Space-level absorption (absorb ALL solo tiles)

Attempted in v2: `on_window_created` loops over ALL tiles, finds solo tiles (1 window), stacks them into the anchor tile (tile with most windows).

**Problem:** Too aggressive. After an intentional split (user presses ctrl+alt+v which promotes a background window into its own tile), the promoted tile has 1 window. The space-level absorption absorbs it back — destroying the intentional split.

**Observed:** After reseed → split → cmd+N: 1 tile (should be 2). The intentional split was eaten.

### Window-level absorption (absorb only window_id's tile)

The fix: only absorb the specific `window_id` passed to `on_window_created`, not all solo tiles.

**Behavior:**
- cmd+N: signal fires with window_id of the new window. Handler finds window_id's tile (solo), stacks it into anchor. Correct.
- Intentional split: promoted window's tile is NOT touched (no signal fired for it). Correct.
- Bulk launch: each of the 5 windows triggers its own signal/watcher. Each handler absorbs its own window. All 5 absorbed.

**Observed:** After reseed → split → cmd+N: 2 tiles (correct — intentional split preserved).

### Anchor tile detection

**`_find_anchor_tile`:** Returns the tile with the most windows (`max(tiles, key=len(all_window_ids))`). More robust than focused/recent window lookup, which breaks when focus shifts to the new window.

**Focus-shift problem (observed and fixed):** When an app creates a new window, macOS focuses it immediately. The `on_window_created` handler queries the focused window, gets the NEW window (not the original). If it uses the focused window as the anchor, it compares the window to itself and incorrectly concludes "already stacked." Fixed by using `_find_anchor_tile` (max window count) instead.

---

## 7. `pending_split` Reconciliation

### How it works

`TrackedSpaceState` has `pending_split: Optional[PendingSplit]` which records:
- `direction`: east or south
- `anchor_window_id`: the window that was focused when split was armed
- `anchor_frame`: the frame of that window at arming time

When `_reconcile_tracked_space` runs, it checks: does the anchor window's current frame still match `anchor_frame`? If not, the split was consumed (a new window arrived and split the space). It clears `pending_split`.

### Two split paths

1. **Promote path:** A background window exists → immediately promoted into a new tile. `pending_split` is set to `None` (the split is done).
2. **Arm path:** No background window → `--insert <direction>` armed on yabai, `pending_split` stored. The next new window consumes the arming.

### Interaction with on_window_created

- `pending_split` consumed (frame changed): the new tile is intentional. `on_window_created` should NOT absorb `window_id`'s tile.
- `pending_split` NOT consumed (or was never set): the split is unwanted. `on_window_created` should absorb.

---

## 8. Reseed Timing

**Observation:** Reseed takes approximately 10 seconds to fully complete. yabai needs time to process all the `--stack` commands for 12+ windows and settle the BSP tree.

**Impact:** Scripts that check tile state less than 10s after reseed see stale results. All automated tests must wait at least 10 seconds after reseed before asserting.

---

## 9. What Worked and What Didn't

### v1 Architecture (arm + Hammerspoon watcher)

| Component | Status | Issue |
|-----------|--------|-------|
| `--rediscover` patch | WORKS | Ghost windows rediscovered successfully |
| `--insert stack` arming | WORKS but VISUAL BUG | Persistent red overlay on the focused window |
| Hammerspoon `windowFilter` watcher | WORKS | 300ms delay, reliable for all creation methods |
| `on_window_created` with focused/recent anchor | BUG | Focus-shift causes self-comparison. Fixed with `_find_anchor_tile` |

### v2 Architecture (signal-only, no arming)

| Component | Status | Issue |
|-----------|--------|-------|
| yabai signal (replaces Hammerspoon) | DOES NOT WORK | `window_created` signal unreliable for cmd+N |
| No `--insert stack` arming | CORRECT | No red overlay |
| Space-level absorption | BUG | Absorbs intentional splits |
| Window-level absorption | CORRECT | Only absorbs the triggering window |
| `_find_anchor_tile` | CORRECT | Robust anchor detection by window count |
| Retry loop for `--rediscover` | PARTIALLY WORKS | 5 attempts * 200ms. Sufficient for some apps, may need tuning for Zen |

### Hybrid (signal + Hammerspoon)

Tested briefly at end of session. The Hammerspoon watcher is needed because yabai signals don't fire for cmd+N. The yabai signal is still registered (may help for app launches where it does fire). Both call `on_window_created` — the handler is idempotent.

**Not fully tested** — session ended during hybrid testing.

---

## 10. Recommendations for v3

1. **Primary detection: Hammerspoon `hs.window.filter`** with 500ms delay. This is the only reliable mechanism for detecting all window creation methods (keyboard shortcuts, app launches, programmatic).

2. **Keep `--rediscover` patch** in yabai. It's the only way to make ghost windows manageable. The retry loop (try rediscover, sleep, retry) is necessary because AX readiness is timing-dependent.

3. **Do NOT use `--insert stack` arming.** The visual feedback cannot be suppressed without adding special cases to yabai source. The brief split-then-absorb flicker (~500ms) is acceptable.

4. **Do NOT rely on yabai `window_created` signals** as the primary detection mechanism. They are unreliable for in-app window creation via keyboard shortcuts.

5. **Use window-level absorption** (absorb only the triggering `window_id`), not space-level. Space-level absorption destroys intentional splits.

6. **Use `_find_anchor_tile` (max window count)** for anchor detection, not focused/recent window lookup. Focus shifts to the new window on creation.

7. **Reseed wait time:** 10 seconds minimum before asserting state in automated scripts.

8. **Signal registration** can remain in `reseed()` as a supplementary mechanism. It may catch app-launch windows that Hammerspoon misses (though no evidence of Hammerspoon missing any creation events was observed).

---

## 11. Files and Line Numbers (as of session end, before rollback)

| File | Key locations |
|------|--------------|
| `windy/windy/workflow.py` | `WorkflowRuntime.__init__` (line 49), `reseed()` (line 74), `split()` (line 113), `delete_tile()` (line 167), `float_space()` (line 193), `on_window_created()` (line 295), `_reconcile_tracked_space()` (line 414), `_live_snapshot()` (line 433) |
| `windy/windy/yabai.py` | `YabaiClient` Protocol (line 10), `SubprocessYabaiClient` (line 56), `arm_window_split()` (line 150), `rediscover_window()` (after swap_window) |
| `windy/windy/cli.py` | `WorkflowRuntime` construction (line 114), `on-window-created` subparser and dispatch |
| `windy/windy/models.py` | `PendingSplit` (line 26), `TrackedSpaceState` (line 33), `LiveTile` (line 48) |
| `windy/windy/eligibility.py` | `is_eligible_window()` (line 6) — checks subrole, role, can-move, has-ax-reference, etc. |
| `windy/hammerspoon/windy.lua` | `stopExisting()` (line 5), `module.start()` (line 149), hotkeys (lines 160-186), alttab (lines 188-231) |
| `windy/tests/test_windy.py` | `FakeYabaiClient` (line ~703), `eligible_window()` helper (line ~817) |
| `github_and_docs_reference/yabai/yabai/src/message.c` | `--rediscover` handler (lines 2258-2274 in patched version) |
| `github_and_docs_reference/yabai/yabai/src/event_loop.c` | `WINDOW_CREATED` handler (lines 510-577), signal flush (line 1629) |
| `github_and_docs_reference/yabai/yabai/src/view.c` | `insert_feedback_show()` (lines 8-102), BSP insertion point handling (lines 768-781) |
| `github_and_docs_reference/yabai/yabai/src/window_manager.c` | `window_manager_add_existing_application_windows()` (line 1595), `window_manager_set_window_insertion()` (line 1736) |
