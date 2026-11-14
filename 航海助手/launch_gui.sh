#!/bin/bash
set -eu

cd -- "$(dirname -- "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mode="${1-}"
shift
case "$mode" in
    game) command=(./launch_game.sh --original) ;;
    trainer) command=(./launch_game.sh --trainer "$@") ;;
    teleport) command=(python3 teleport.py) ;;
    route) command=(python3 teleport.py --story-destinations) ;;
    *) exit 2 ;;
esac

if ! output=$("${command[@]}" 2>&1); then
    printf '%s\n' "$output" > "${TMPDIR:-/tmp}/sea2-$mode.launch-error.log"
    /usr/bin/osascript -e 'on run argv
        display alert "航海助手启动失败" message (item 1 of argv)
    end run' "${output:-启动器没有返回错误详情。}"
    exit 1
fi
