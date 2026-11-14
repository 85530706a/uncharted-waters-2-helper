#!/bin/bash
set -eu

cd -- "$(dirname -- "$0")"
if [ -x /opt/homebrew/bin/dosbox-x ]; then
    emulator=/opt/homebrew/bin/dosbox-x
else
    emulator=$(command -v dosbox-x) || { echo '未找到 DOSBox-X。请先安装 DOSBox-X。' >&2; exit 1; }
fi

workspace="$(pwd -P)"
session="$workspace/游戏副本"
source_game="$workspace/.原版资源"

if [ "$#" -eq 0 ] || [ "${1-}" = "--original" ]; then
    exec python3 "$workspace/original_game.py" "$emulator"
fi

if [ ! -f "$source_game/MAIN.EXE" ]; then
    echo "请先将中文版 DOS 游戏文件放入航海助手的 .原版资源 文件夹。" >&2
    exit 1
fi

if [ ! -d "$session" ]; then
    staging=$(mktemp -d "$workspace/.game-copy.XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT
    cp -R -- "$source_game/." "$staging"
    mv -n -- "$staging" "$session"
fi

if [ "${1-}" = "--trainer" ]; then
    exec python3 "$workspace/live.py" "$emulator" "$session" "$@"
fi

if [ "${1-}" = "--live" ]; then
    exec python3 "$workspace/live.py" "$emulator" "$session" --live
fi

exec python3 "$workspace/live.py" "$emulator" "$session" --auto "$@"
