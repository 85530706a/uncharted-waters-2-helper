"""Launch a separate DOSBox-X session with the original Chinese game files."""

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sea2 import HEADER_SIZE, ORIGINAL_EXE_SHA256, SAVE, SLOT_COUNT, SLOT_SIZE


def run_original(emulator, source, session):
    with (session.parent / ".sea2-original.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("原版游戏已经打开。") from error
        if not session.exists():
            if not (source / "MAIN.EXE").is_file():
                raise ValueError("请先将中文版 DOS 游戏文件放入航海助手的 .原版资源 文件夹。")
            with tempfile.TemporaryDirectory(prefix=".original-", dir=session.parent) as tmp:
                staging = Path(tmp) / "game"
                shutil.copytree(source, staging, ignore=shutil.ignore_patterns(SAVE.name))
                (staging / SAVE.name).write_bytes(bytes(HEADER_SIZE + SLOT_SIZE * SLOT_COUNT))
                staging.rename(session)
        if hashlib.sha256((session / "MAIN.EXE").read_bytes()).hexdigest() != ORIGINAL_EXE_SHA256:
            raise ValueError("原版游戏目录里的 MAIN.EXE 已变更。请使用支持的中文版原始文件。")
        game = subprocess.Popen(
            [emulator, "-nopromptfolder", "-noautoexec", "-set", "dosbox mcp_server=0",
             "-c", "mount c .",
             "-c", "c:", "-c", "koukai2", "-c", "exit"],
            cwd=session, pass_fds=(lock.fileno(),),
        )
        if focus_file := os.environ.get("SEA2_FOCUS_FILE"):
            try:
                Path(focus_file).write_text(str(game.pid))
            except OSError:
                pass
        return game.wait()


if __name__ == "__main__":
    try:
        sys.exit(run_original(sys.argv[1], SAVE.parent, SAVE.parent.parent / ".原版游戏"))
    except (OSError, ValueError) as error:
        sys.exit(str(error))
