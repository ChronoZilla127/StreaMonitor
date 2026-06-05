import errno
import os
import subprocess
import sys

import requests.cookies
from threading import Thread
from parameters import DEBUG, SEGMENT_TIME, CONTAINER, FFMPEG_PATH, FFMPEG_READRATE


def _tail_file(path, max_lines=20):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536), os.SEEK_SET)
            output = f.read().decode(errors='replace').strip()
    except OSError:
        return ''
    if not output:
        return ''
    return '\n'.join(output.splitlines()[-max_lines:])


def _format_return_code(returncode):
    if returncode > 0x7fffffff:
        return f'{returncode} ({returncode - 0x100000000})'
    return str(returncode)


def getVideoFfmpeg(self, url, filename):
    cmd = [
        FFMPEG_PATH,
        '-user_agent', self.headers['User-Agent']
    ]

    if type(self.cookies) is requests.cookies.RequestsCookieJar:
        cookies_text = ''
        for cookie in self.cookies:
            cookies_text += cookie.name + "=" + cookie.value + "; path=" + cookie.path + '; domain=' + cookie.domain + '\n'
        if len(cookies_text) > 10:
            cookies_text = cookies_text[:-1]
        cmd.extend([
            '-cookies', cookies_text
        ])

    if FFMPEG_READRATE:
        cmd.extend(['-readrate', f'{FFMPEG_READRATE!s}'])

    cmd.extend([
        '-max_reload', '20',
        '-seg_max_retry', '20',
        '-m3u8_hold_counters', '20',
        '-i', url,
        '-c:a', 'copy',
        '-c:v', 'copy',
    ])

    suffix = ''
    if hasattr(self, 'filename_extra_suffix'):
        suffix = self.filename_extra_suffix

    if SEGMENT_TIME is not None:
        username = filename.rsplit('-', maxsplit=2)[0]
        cmd.extend([
            '-f', 'segment',
            '-reset_timestamps', '1',
            '-segment_time', str(SEGMENT_TIME),
            '-strftime', '1',
            f'{username}-%Y%m%d-%H%M%S{suffix}.{CONTAINER}'
        ])
    else:
        cmd.extend([
            os.path.splitext(filename)[0] + suffix + '.' + CONTAINER
        ])

    class _Stopper:
        def __init__(self):
            self.stop = False

        def pls_stop(self):
            self.stop = True

    stopping = _Stopper()
    error = False

    def execute():
        nonlocal error
        stderr_path = filename + '.stderr.log'
        stderr = None
        process = None
        try:
            stderr = open(stderr_path, 'wb')
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            process = subprocess.Popen(
                args=cmd, stdin=subprocess.PIPE, stderr=stderr, stdout=subprocess.DEVNULL, startupinfo=startupinfo)
        except OSError as e:
            if e.errno == errno.ENOENT:
                self.logger.error('FFMpeg executable not found!')
                error = True
                return
            else:
                self.logger.error("Got OSError, errno: " + str(e.errno))
                error = True
                return
        finally:
            if process is None and stderr is not None:
                stderr.close()

        while process.poll() is None:
            if stopping.stop:
                process.communicate(b'q')
                break
            try:
                process.wait(1)
            except subprocess.TimeoutExpired:
                pass

        if stderr is not None:
            stderr.close()

        if process.returncode and process.returncode != 0 and process.returncode != 255:
            details = _tail_file(stderr_path)
            return_code = _format_return_code(process.returncode)
            if details:
                self.logger.error(
                    f'The process exited with an error. Return code: {return_code}\n{details}'
                )
            elif DEBUG:
                self.logger.error(
                    f'The process exited with an error. Return code: {return_code}. See {stderr_path}'
                )
            else:
                self.logger.error('The process exited with an error. Return code: ' + return_code)
            error = True
            return

        if not DEBUG:
            try:
                os.remove(stderr_path)
            except OSError:
                pass

    thread = Thread(target=execute)
    thread.start()
    self.stopDownload = lambda: stopping.pls_stop()
    thread.join()
    self.stopDownload = None
    return not error
