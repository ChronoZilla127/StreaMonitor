import m3u8
import os
import subprocess
from threading import Thread
from ffmpy import FFmpeg, FFRuntimeError
from time import sleep
from parameters import DEBUG, CONTAINER, SEGMENT_TIME, FFMPEG_PATH
from streamonitor.enums import Status

_http_lib = None
if not _http_lib:
    try:
        import pycurl_requests as requests
        _http_lib = 'pycurl'
    except ImportError:
        pass
if not _http_lib:
    try:
        import requests
        _http_lib = 'requests'
    except ImportError:
        pass
if not _http_lib:
    raise ImportError("Please install requests or pycurl package to proceed")


def _tail_output(output, max_lines=20):
    if output is None:
        return ''
    if isinstance(output, bytes):
        output = output.decode(errors='replace')
    output = output.strip()
    if not output:
        return ''
    return '\n'.join(output.splitlines()[-max_lines:])


def getVideoNativeHLS(self, url, filename, m3u_processor=None):
    self.stopDownloadFlag = False
    error = False
    ended_transiently = False
    tmpfilename = filename[:-len('.' + CONTAINER)] + '.tmp.ts'
    session = requests.Session()

    def execute():
        nonlocal ended_transiently, error
        downloaded_list = []

        def has_data(outfile):
            return outfile.tell() > 0

        def set_status_from_http(status_code):
            if status_code == 403:
                self.sc = Status.PRIVATE
            elif status_code in (404, 410):
                self.sc = Status.OFFLINE
            elif status_code == 429:
                self.sc = Status.RATELIMIT
                self.ratelimit = True

        def end_as_transient(outfile, message, status_code=None):
            nonlocal ended_transiently
            ended_transiently = True
            if status_code:
                set_status_from_http(status_code)
            if has_data(outfile):
                self.logger.warning(message + '; ending recording with downloaded data')
            else:
                self.logger.warning(message + '; ending recording before any data was downloaded')
            return True

        def end_after_partial_download(outfile, message):
            if has_data(outfile):
                self.logger.warning(message + '; ending recording with downloaded data')
                return True
            return False

        try:
            with open(tmpfilename, 'wb') as outfile:
                did_download = False
                while not self.stopDownloadFlag:
                    try:
                        r = session.get(url, headers=self.headers, cookies=self.cookies)
                    except Exception as e:
                        if end_after_partial_download(outfile, f'Failed to fetch HLS playlist: {e}'):
                            return
                        self.logger.exception(f'Failed to fetch HLS playlist: {e}')
                        error = True
                        return
                    if r.status_code != 200:
                        if r.status_code in (403, 404, 410, 429) and end_as_transient(
                            outfile,
                            f'HLS playlist returned HTTP {r.status_code}',
                            r.status_code,
                        ):
                            return
                        self.logger.error(f'Failed to fetch HLS playlist: HTTP {r.status_code}')
                        error = True
                        return
                    try:
                        content = r.content.decode("utf-8")
                    except UnicodeDecodeError as e:
                        self.logger.exception(f'Failed to decode HLS playlist response: {e}')
                        error = True
                        return
                    if m3u_processor:
                        content = m3u_processor(content)
                        if not content:
                            if end_after_partial_download(outfile, 'HLS playlist processor returned no content'):
                                return
                            self.logger.error('HLS playlist processor returned no content')
                            error = True
                            return
                    try:
                        chunklist = m3u8.loads(content)
                    except Exception as e:
                        self.logger.exception(f'Failed to parse HLS playlist: {e}')
                        error = True
                        return
                    if len(chunklist.segments) == 0:
                        if did_download or outfile.tell() > 0:
                            self.debug('HLS playlist contains no media segments; ending recording')
                        else:
                            self.logger.error('HLS playlist contains no media segments before any data was downloaded')
                            error = True
                        return
                    for init_segment in chunklist.segment_map:
                        if init_segment.uri not in downloaded_list and has_data(outfile):
                            self.logger.warning(
                                'HLS initialization segment changed; ending recording with downloaded data'
                            )
                            return
                    for chunk in chunklist.segment_map + chunklist.segments:
                        if chunk.uri in downloaded_list:
                            continue
                        did_download = True
                        downloaded_list.append(chunk.uri)
                        chunk_uri = chunk.uri
                        self.debug('Downloading ' + chunk_uri)
                        if not chunk_uri.startswith("https://"):
                            chunk_uri = '/'.join(url.split('.m3u8')[0].split('/')[:-1]) + '/' + chunk_uri
                        try:
                            m = session.get(chunk_uri, headers=self.headers, cookies=self.cookies)
                        except Exception as e:
                            if end_after_partial_download(outfile, f'Failed to download HLS segment {chunk_uri}: {e}'):
                                return
                            self.logger.exception(f'Failed to download HLS segment {chunk_uri}: {e}')
                            error = True
                            return
                        if m.status_code != 200:
                            if m.status_code in (403, 404, 410, 429) and end_as_transient(
                                outfile,
                                f'HLS segment returned HTTP {m.status_code}: {chunk_uri}',
                                m.status_code,
                            ):
                                return
                            self.logger.error(f'Failed to download HLS segment {chunk_uri}: HTTP {m.status_code}')
                            error = True
                            return
                        outfile.write(m.content)
                        if self.stopDownloadFlag:
                            return
                    if not did_download:
                        sleep(10)
        except Exception as e:
            self.logger.exception(f'Unexpected error while downloading HLS stream: {e}')
            error = True

    def terminate():
        self.stopDownloadFlag = True

    process = Thread(target=execute)
    process.start()
    self.stopDownload = terminate
    process.join()
    self.stopDownload = None

    if error:
        if os.path.exists(tmpfilename) and os.path.getsize(tmpfilename) == 0:
            os.remove(tmpfilename)
        return False

    if not os.path.exists(tmpfilename):
        if ended_transiently:
            return False
        self.logger.error(f'HLS temp file was not created: {tmpfilename}')
        return False

    if os.path.getsize(tmpfilename) == 0:
        os.remove(tmpfilename)
        if ended_transiently:
            return False
        self.logger.error(f'HLS temp file is empty: {tmpfilename}')
        return False

    # Post-processing
    stdout = None
    stderr = None
    stderr_log = filename + '.postprocess_stderr.log'
    try:
        stdout = open(filename + '.postprocess_stdout.log', 'w+') if DEBUG else subprocess.PIPE
        stderr = open(stderr_log, 'w+') if DEBUG else subprocess.PIPE
        output_str = '-c:a copy -c:v copy'
        suffix = ''
        if SEGMENT_TIME is not None:
            output_str += f' -f segment -reset_timestamps 1 -segment_time {str(SEGMENT_TIME)}'
            if hasattr(self, 'filename_extra_suffix'):
                suffix = self.filename_extra_suffix
            filename = filename[:-len('.' + CONTAINER)] + '_%03d' + suffix + '.' + CONTAINER
        ff = FFmpeg(executable=FFMPEG_PATH, inputs={tmpfilename: None}, outputs={filename: output_str})
        ff.run(stdout=stdout, stderr=stderr)
        os.remove(tmpfilename)
    except FFRuntimeError as e:
        if e.exit_code and e.exit_code != 255:
            details = _tail_output(e.stderr)
            if details:
                self.logger.error(f'FFmpeg post-processing failed with exit code {e.exit_code}:\n{details}')
            elif DEBUG:
                self.logger.error(f'FFmpeg post-processing failed with exit code {e.exit_code}. See {stderr_log}')
            else:
                self.logger.error(f'FFmpeg post-processing failed with exit code {e.exit_code}: {e}')
            return False
    finally:
        if hasattr(stdout, 'close'):
            stdout.close()
        if hasattr(stderr, 'close'):
            stderr.close()

    return True
