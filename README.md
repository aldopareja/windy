# Windy

Windy is a small macOS window workflow for yabai + Hammerspoon. It gives you a few keyboard actions for splitting a space, moving between visible tiles, and swapping windows with `opt+tab`.

## Prerequisites

- macOS
- yabai installed and working
- Hammerspoon installed
- `hs` available in your shell
- Accessibility and automation permissions already granted to yabai and Hammerspoon

## Install

```sh
git clone <repo-url>
cd windy
./bin/windy install hammerspoon
```

Windy stores the repo path in your Hammerspoon config. If you move the repo later, run the install command again.

## Use

- `ctrl+alt+space`: start managing the current space
- `ctrl+alt+h`: split top/bottom
- `ctrl+alt+v`: split left/right
- `ctrl+alt+left/right/up/down`: move between visible tiles
- `ctrl+alt+d`: close the current tile into another tile
- `ctrl+alt+f`: stop managing the current space
- `opt+tab`: choose another window and commit the swap when you release `opt`

## Update

```sh
git pull
./bin/windy install hammerspoon
```
