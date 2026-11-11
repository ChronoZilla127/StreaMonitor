import json
import os
import re
import subprocess
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

from websocket import create_connection, WebSocketConnectionClosedException, WebSocketException
from parameters import DEBUG, CONTAINER, SEGMENT_TIME, FFMPEG_PATH
from streamonitor.downloaders.postprocess import run_ffmpeg_postprocess


def _stream_resolution(filename):
    try:
        result = run_ffmpeg_postprocess([
            FFMPEG_PATH, '-hide_banner', '-nostdin', '-probesize', '1048576',
            '-analyzeduration', '1000000', '-ignore_editlist', '1', '-i', filename,
            '-map', '0:v:0', '-c:v', 'copy', '-frames:v', '0', '-f', 'null', '-',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stderr.decode(errors='replace').splitlines():
        if 'Video:' not in line:
            continue
        dimensions = re.search(r', (\d+x\d+)(?=$|[ ,])', line)
        if dimensions:
            rate = re.search(r', (\d+(?:\.\d+)?) fps\b', line)
            fps = f' {float(rate[1]):.1f}fps' if rate else ''
            return f'{dimensions[1]}{fps}'
    return None


def getVideoWSSVR(self, url, filename, *, origin=None, protocol_version='0.0.1'):
    self.stopDownloadFlag = False
    error = False
    if url.startswith('fmp4s://'):
        url = url.replace('fmp4s://', 'wss://', 1)
    modern_protocol = protocol_version == '3.0.0'
    connection_options = {'origin': origin} if origin else {}

    def send(conn, route, **fields):
        message = {'url': route, 'version': protocol_version, **fields}
        if modern_protocol:
            message['correlationId'] = str(uuid4())
        conn.send(json.dumps(message))

    suffix = ''
    if hasattr(self, 'filename_extra_suffix'):
        suffix = self.filename_extra_suffix

    basefilename = filename[:-len('.' + CONTAINER)]
    filename = basefilename + suffix + '.' + CONTAINER
    tmpfilename = basefilename + '.tmp.mp4'

    def debug_(message):
        self.debug(message, filename + '.log')

    def execute():
        nonlocal error
        has_media = False
        with open(tmpfilename, 'wb') as outfile:
            while not self.stopDownloadFlag:
                try:
                    with closing(create_connection(url, timeout=10, **connection_options)) as conn:
                        send(conn, 'stream/hello')
                        while not self.stopDownloadFlag:
                            t = conn.recv()
                            try:
                                tj = json.loads(t)
                                if 'url' in tj:
                                    if tj['url'] == 'stream/qual':
                                        if modern_protocol:
                                            send(conn, 'stream/play', includeMask=False, passthroughToken='')
                                        else:
                                            send(conn, 'stream/play', quality='test')
                                            debug_('Connection opened')
                                        break
                                if 'message' in tj:
                                    if tj['message'] == 'ping':
                                        debug_('Server is not ready or there was a change')
                                        error = True
                                        return
                            except:
                                debug_('Failed to open the connection')
                                error = True
                                return

                        while not self.stopDownloadFlag:
                            data = conn.recv()
                            if not data:
                                raise WebSocketConnectionClosedException()
                            if isinstance(data, bytes):
                                outfile.write(data)
                                if not has_media and (not modern_protocol or data[4:8] == b'moof'):
                                    has_media = True
                                    if modern_protocol:
                                        outfile.flush()
                                        resolution = _stream_resolution(tmpfilename)
                                        if resolution:
                                            self.log(f'Selected {resolution} resolution')
                                        else:
                                            debug_('Received video; resolution could not be detected')
                except WebSocketConnectionClosedException:
                    if modern_protocol:
                        # The next connection starts with a new MP4 initialization segment.
                        break
                    debug_('WebSocket connection closed - try to continue')
                    continue
                except WebSocketException as wex:
                    debug_('Error when downloading')
                    debug_(wex)
                    error = not (modern_protocol and has_media)
                    break
        if modern_protocol and not has_media:
            os.remove(tmpfilename)
            error = True

    def terminate():
        self.stopDownloadFlag = True

    process = Thread(target=execute)
    self.stopDownload = terminate
    try:
        if not self.running:
            return True
        process.start()
        process.join()
    finally:
        self.stopDownload = None

    if error:
        return False

    try:
        output = Path(filename)
        with TemporaryDirectory(prefix='.vr-remux-', dir=output.parent) as directory, ExitStack() as logs:
            stdout = stderr = subprocess.DEVNULL
            if DEBUG:
                stdout = logs.enter_context(open(filename + '.postprocess_stdout.log', 'wb'))
                stderr = logs.enter_context(open(filename + '.postprocess_stderr.log', 'wb'))
            command = [FFMPEG_PATH, '-nostdin', '-ignore_editlist', '1', '-i', tmpfilename, '-c', 'copy']
            output_name = output.name
            if SEGMENT_TIME is not None:
                command.extend(['-f', 'segment', '-reset_timestamps', '1', '-segment_time', str(SEGMENT_TIME)])
                output_name = Path(basefilename).name.replace('%', '%%') + '_%03d' + suffix + '.' + CONTAINER
            command.append(str(Path(directory) / output_name))
            result = run_ffmpeg_postprocess(command, stdout=stdout, stderr=stderr)
            parts = sorted(Path(directory).iterdir())
            if result.returncode or not parts:
                self.logger.error(
                    f'FFmpeg could not finalize the VR recording (exit {result.returncode}); '
                    f'capture kept at {tmpfilename}'
                )
                return False
            for part in parts:
                os.replace(part, output.parent / part.name)
        os.remove(tmpfilename)
    except OSError as exc:
        self.logger.error(f'Could not finalize the VR recording: {exc}')
        return False

    return True
