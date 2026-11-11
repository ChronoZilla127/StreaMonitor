import os
import subprocess


def run_ffmpeg_postprocess(command, *, stdout, stderr, timeout=None):
    """Let FFmpeg finish when the parent handles a terminal Ctrl+C."""
    if os.name == 'nt':
        isolation = {'creationflags': subprocess.CREATE_NO_WINDOW}
    else:
        isolation = {'start_new_session': True}
    return subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
        timeout=timeout, **isolation,
    )
