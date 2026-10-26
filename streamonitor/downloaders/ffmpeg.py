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

    referer = getattr(self, 'ffmpeg_referer', None)
    if referer:
        cmd.extend(['-referer', referer])

    if type(self.cookies) is requests.cookies.RequestsCookieJar:
        cookies_text = ''
        for cookie in self.cookies:
            cookies_text += cookie.name + "=" + cookie.value + "; path=" + cookie.path + '; domain=' + cookie.domain + '\n'
        if len(cookies_text) > 10:
            cookies_text = cookies_text[:-1]
        cmd.extend([
            '-cookies', cookies_text
        ])

    if FFMPEG_READRATE and not getattr(self, 'ffmpeg_disable_readrate', False):
        cmd.extend(['-readrate', f'{FFMPEG_READRATE!s}'])
    if getattr(self, 'ffmpeg_copy_timestamps', False):
        cmd.extend(['-copyts', '-start_at_zero'])

    allowed_extensions = getattr(self, 'ffmpeg_allowed_extensions', None)
    if allowed_extensions:
        cmd.extend(['-allowed_extensions', allowed_extensions])

    allowed_segment_extensions = getattr(
        self, 'ffmpeg_allowed_segment_extensions', None
    )
    if allowed_segment_extensions:
        cmd.extend([
            '-allowed_segment_extensions', allowed_segment_extensions
        ])

    extension_picky = getattr(self, 'ffmpeg_extension_picky', None)
    if extension_picky is not None:
        cmd.extend(['-extension_picky', str(int(extension_picky))])

    cmd.extend([
        '-max_reload', '20',
        '-seg_max_retry', '20',
        '-m3u8_hold_counters', '20',
        '-i', url,
        '-c:a', getattr(self, 'ffmpeg_audio_codec', 'copy'),
        '-c:v', 'copy',
    ])
    for stream in getattr(self, 'ffmpeg_output_maps', None) or ():
        cmd.extend(['-map', stream])

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

        recording_error = getattr(self, 'ffmpeg_recording_error', None)
        recording_failure = None
        log_position = 0
        pending_log = ''

        def check_recording():
            nonlocal log_position, pending_log
            if not recording_error:
                return None
            try:
                with open(stderr_path, 'rb') as output:
                    output.seek(log_position)
                    content = output.read()
                    log_position = output.tell()
            except OSError:
                return None
            text = pending_log + content.decode(errors='replace')
            reason = recording_error(text)
            # Preserve a partial line between reads without retaining full logs.
            pending_log = text.rsplit('\n', 1)[-1][-4096:]
            return reason

        while process.poll() is None:
            recording_failure = check_recording()
            if recording_failure:
                try:
                    process.communicate(b'q', timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                break
            if stopping.stop:
                process.communicate(b'q')
                break
            try:
                process.wait(1)
            except subprocess.TimeoutExpired:
                pass

        if process.stdin is not None:
            process.stdin.close()
        if stderr is not None:
            stderr.close()

        recording_failure = recording_failure or check_recording()
        if recording_failure:
            error = True
            self.logger.warning(recording_failure)
            return

        if process.returncode and process.returncode != 0 and process.returncode != 255:
            details = _tail_file(stderr_path)
            error = True
            error_handler = getattr(self, 'handle_ffmpeg_error', None)
            if error_handler and error_handler(details):
                return
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
