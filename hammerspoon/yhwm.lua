local module = {}

local runtimeState = _G.__yhwm_runtime_v2

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
  if runtimeState.appWatcher ~= nil then
    runtimeState.appWatcher:stop()
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
    message = "yhwm command failed"
  end
  hs.alert.show(message, 1.5)
end

local function runYhwm(state, args, onExit)
  local task = hs.task.new(state.yhwmPath, function(exitCode, stdOut, stdErr)
    if onExit ~= nil then
      onExit(exitCode, stdOut, stdErr)
      return
    end
    defaultErrorHandler(exitCode, stdOut, stdErr)
  end, args)

  if task == nil then
    hs.alert.show("failed to launch yhwm", 1.5)
    return
  end
  task:start()
end

local function cancelSession(state, reason)
  local args = {"alttab", "cancel", "--reason", reason}
  local windowId = focusedWindowId()
  if windowId ~= nil then
    table.insert(args, "--window-id")
    table.insert(args, tostring(windowId))
  end
  runYhwm(state, args)
end

local function modifiersAreAltOnly(flags)
  return flags.alt and not flags.cmd and not flags.ctrl and not flags.shift and not flags.fn
end

function module.start(config)
  stopExisting()

  local state = {
    yhwmPath = config.yhwm_path,
    alttabArmed = false,
    releaseTimer = nil,
    hotkeys = {},
  }
  _G.__yhwm_runtime_v2 = state

  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "space", function()
    runYhwm(state, {"reseed"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "h", function()
    runYhwm(state, {"split", "--direction", "south"})
  end))
  table.insert(state.hotkeys, hs.hotkey.bind({"ctrl", "alt"}, "v", function()
    runYhwm(state, {"split", "--direction", "east"})
  end))

  state.keyDownTap = hs.eventtap.new({hs.eventtap.event.types.keyDown}, function(event)
    local keyCode = event:getKeyCode()
    local flags = event:getFlags()

    if keyCode == hs.keycodes.map.tab and modifiersAreAltOnly(flags) then
      state.alttabArmed = true
      runYhwm(state, {"alttab", "open"})
      return false
    end

    if state.alttabArmed and keyCode == hs.keycodes.map.escape then
      state.alttabArmed = false
      cancelSession(state, "esc")
      return false
    end

    if state.alttabArmed and keyCode == hs.keycodes.map.space then
      state.alttabArmed = false
      cancelSession(state, "space")
      return false
    end

    return false
  end)

  state.flagsTap = hs.eventtap.new({hs.eventtap.event.types.flagsChanged}, function(event)
    local flags = event:getFlags()
    if state.alttabArmed and not flags.alt then
      state.alttabArmed = false
      if state.releaseTimer ~= nil then
        state.releaseTimer:stop()
      end
      state.releaseTimer = hs.timer.doAfter(0.05, function()
        local args = {"alttab", "release"}
        local windowId = focusedWindowId()
        if windowId ~= nil then
          table.insert(args, "--window-id")
          table.insert(args, tostring(windowId))
        end
        runYhwm(state, args)
      end)
    end
    return false
  end)

  state.appWatcher = hs.application.watcher.new(function(appName, eventType, appObject)
    local bundleId = nil
    if appObject ~= nil then
      bundleId = appObject:bundleID()
    end
    if bundleId ~= "com.lwouis.alt-tab-macos" and appName ~= "AltTab" then
      return
    end
    if not state.alttabArmed then
      return
    end

    if eventType == hs.application.watcher.deactivated then
      state.alttabArmed = false
      cancelSession(state, "chooser_close")
      return
    end

    if eventType == hs.application.watcher.hidden then
      state.alttabArmed = false
      cancelSession(state, "chooser_hide")
      return
    end

    if eventType == hs.application.watcher.terminated then
      state.alttabArmed = false
      cancelSession(state, "chooser_quit")
    end
  end)

  state.keyDownTap:start()
  state.flagsTap:start()
  state.appWatcher:start()
end

return module
