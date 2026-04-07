local module = {}

local runtimeState = _G.__windy_runtime

local function stopExisting()
  if runtimeState == nil then
    return
  end

  if runtimeState.keyDownTap ~= nil then
    runtimeState.keyDownTap:stop()
  end
  if runtimeState.flagsTap ~= nil then
    runtimeState.flagsTap:stop()
  end
  if runtimeState.releaseTimer ~= nil then
    runtimeState.releaseTimer:stop()
  end
  if runtimeState.hotkeys ~= nil then
    for _, hotkey in ipairs(runtimeState.hotkeys) do
      hotkey:delete()
    end
  end
end

local function focusedWindowId()
  local win = hs.window.focusedWindow()
  if win == nil then
    return nil
  end
  return win:id()
end

local function defaultErrorHandler(exitCode, _, stdErr)
  if exitCode == 0 then
    return
  end
  local message = stdErr
  if message == nil or message == "" then
    message = "windy command failed"
  end
  hs.alert.show(message, 1.5)
end

local function runWindy(state, args, onExit)
  local task = hs.task.new(state.windyPath, function(exitCode, stdOut, stdErr)
    if onExit ~= nil then
      onExit(exitCode, stdOut, stdErr)
      return
    end
    defaultErrorHandler(exitCode, stdOut, stdErr)
  end, args)

  if task == nil then
    hs.alert.show("failed to launch windy", 1.5)
    return
  end
  task:start()
end

local function modifiersAreAltOnly(flags)
  return flags.alt and not flags.cmd and not flags.ctrl and not flags.shift and not flags.fn
end

local function normalizeFrame(frame)
  return {
    x = math.floor(frame.x + 0.5),
    y = math.floor(frame.y + 0.5),
    w = math.floor(frame.w + 0.5),
    h = math.floor(frame.h + 0.5),
  }
end

local function frameKey(frame)
  return string.format("%d,%d,%d,%d", frame.x, frame.y, frame.w, frame.h)
end

local function captureAltTabSnapshot()
  local firstByFrame = {}
  local byWindowId = {}

  for _, win in ipairs(hs.window.orderedWindows()) do
    local windowId = win:id()
    if windowId ~= nil then
      local normalized = normalizeFrame(win:frame())
      local key = frameKey(normalized)
      local isVisible = firstByFrame[key] == nil
      if isVisible then
        firstByFrame[key] = windowId
      end
      byWindowId[windowId] = {
        frame = normalized,
        frame_key = key,
        visible = isVisible,
      }
    end
  end

  return {
    by_window_id = byWindowId,
  }
end

local function clearAltTabSession(state)
  state.alttabSession = nil
  if state.releaseTimer ~= nil then
    state.releaseTimer:stop()
    state.releaseTimer = nil
  end
end

local function queueAltTabCommit(state)
  local session = state.alttabSession
  clearAltTabSession(state)
  if session == nil then
    return
  end

  state.releaseTimer = hs.timer.doAfter(0.05, function()
    local selectedWindowId = focusedWindowId()
    if selectedWindowId == nil or selectedWindowId == session.origin_window_id then
      return
    end

    local originMeta = session.snapshot.by_window_id[session.origin_window_id]
    local selectedMeta = session.snapshot.by_window_id[selectedWindowId]
    if originMeta == nil or selectedMeta == nil then
      return
    end

    local args = {
      "alttab",
      "--origin-window-id",
      tostring(session.origin_window_id),
      "--selected-window-id",
      tostring(selectedWindowId),
      "--origin-open-frame",
      originMeta.frame_key,
      "--selected-open-frame",
      selectedMeta.frame_key,
    }
    if selectedMeta.visible then
      table.insert(args, "--selected-was-visible-at-open")
    end
    runWindy(state, args)
  end)
end

function module.start(config)
  stopExisting()

  local state = {
    windyPath = config.windy_path,
    alttabSession = nil,
    releaseTimer = nil,
    hotkeys = {},
  }
  _G.__windy_runtime = state

  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "space", function()
    runWindy(state, {"reseed"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "f", function()
    runWindy(state, {"float"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "d", function()
    runWindy(state, {"delete-tile"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "left", function()
    runWindy(state, {"navigate", "--direction", "west"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "right", function()
    runWindy(state, {"navigate", "--direction", "east"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "up", function()
    runWindy(state, {"navigate", "--direction", "north"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "down", function()
    runWindy(state, {"navigate", "--direction", "south"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "h", function()
    runWindy(state, {"split", "--direction", "south"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "v", function()
    runWindy(state, {"split", "--direction", "east"})
  end))

  state.keyDownTap = hs.eventtap.new({hs.eventtap.event.types.keyDown}, function(event)
    local keyCode = event:getKeyCode()
    local flags = event:getFlags()

    if keyCode == hs.keycodes.map.tab and modifiersAreAltOnly(flags) then
      if state.alttabSession == nil then
        local originWindowId = focusedWindowId()
        if originWindowId ~= nil then
          local snapshot = captureAltTabSnapshot()
          if snapshot.by_window_id[originWindowId] ~= nil then
            state.alttabSession = {
              origin_window_id = originWindowId,
              snapshot = snapshot,
            }
          end
        end
      end
      return false
    end

    if state.alttabSession ~= nil and keyCode == hs.keycodes.map.escape then
      clearAltTabSession(state)
      return false
    end

    if state.alttabSession ~= nil and keyCode == hs.keycodes.map.space then
      clearAltTabSession(state)
      return false
    end

    return false
  end)

  state.flagsTap = hs.eventtap.new({hs.eventtap.event.types.flagsChanged}, function(event)
    local flags = event:getFlags()
    if state.alttabSession ~= nil and not flags.alt then
      queueAltTabCommit(state)
    end
    return false
  end)

  state.keyDownTap:start()
  state.flagsTap:start()
end

return module
