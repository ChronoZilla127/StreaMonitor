import os
import shutil
import subprocess
import tempfile
from collections import deque
from concurrent.futures import Future, wait
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import ceil
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from time import monotonic, sleep
from urllib.parse import urljoin

import requests

from parameters import CONTAINER, FFMPEG_PATH, SEGMENT_TIME
from streamonitor.downloaders.hls import _invalid_fmp4_fragment_reason, _iter_mp4_boxes
from streamonitor.enums import Status


VIDEO_PROGRESS_TIMEOUT = 60
PLAYLIST_POLL_INTERVAL = 2
SEGMENT_QUEUE_LIMIT = 100
SEGMENT_STOP_GRACE = 5
RETRYABLE_HTTP_STATUSES = (408, 500, 502, 503, 504)
RETRYABLE_REQUEST_ERRORS = (requests.exceptions.ChunkedEncodingError, requests.ConnectionError, requests.Timeout)
RECENT_SAVED_SEGMENTS = 256


class _RecordingEnded(Exception):
    pass


class _PlaylistRequestFailed(_RecordingEnded):
    pass


class _SegmentWorkers:
    # The launcher can return while the bots remain active. These workers are
    # owned by the recording and do not depend on the executor's exit hook.
    def __init__(self, download):
        self.jobs = Queue()
        self.download = download
        self.threads = []

    def __enter__(self):
        try:
            for _ in range(2):
                thread = Thread(target=self._work, name='camsoda-hls')
                thread.start()
                self.threads.append(thread)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        for _ in self.threads:
            self.jobs.put(None)
        for thread in self.threads:
            thread.join()

    def submit(self, url, media, saved_fingerprints):
        job = Future()
        self.jobs.put((job, url, media, saved_fingerprints))
        return job

    def _work(self):
        with requests.Session() as session:
            while (item := self.jobs.get()) is not None:
                job, url, media, saved_fingerprints = item
                if not job.set_running_or_notify_cancel():
                    continue
                try:
                    result = self.download(session, url, media, saved_fingerprints)
                except BaseException as error:
                    job.set_exception(error)
                else:
                    job.set_result(result)


@dataclass
class _Segment:
    path: Path
    init: Path | None
    duration: float
    discontinuity: bool
    program_time: datetime | None
    init_fingerprint: bytes
    fingerprint: bytes | None = None


class _MediaTrack:
    def __init__(self, url, path, saved_segments):
        self.url = url
        self.path = path
        self.queued_uris = set()
        self.pending = deque()
        self.segments = []
        self.saved_segments = saved_segments
        self.saved_fingerprints = set(saved_segments)
        self.skipped_saved_segments = False
        self.init_url = None
        self.initialization = b''
        self.init_fingerprint = b''
        self.init_path = None
        self.init_group = 0
        self.first_program_time = None
        self.last_segment_at = monotonic()

    def poll(self, fetch, parse, stopping, submit):
        response = fetch(self.url)
        playlist = parse(response.text)
        if not response.text.lstrip('\ufeff').startswith('#EXTM3U') or playlist.is_variant:
            raise _RecordingEnded('Expected an HLS media playlist')

        for segment in playlist.segments:
            if stopping():
                return False
            # LL-HLS can end with a partial segment that has no media URI yet.
            if not segment.uri or segment.uri in self.queued_uris or segment.gap_tag:
                continue

            if segment.key and segment.key.method != 'NONE':
                raise _RecordingEnded('Encrypted HLS media is unsupported')
            init = segment.init_section
            if segment.byterange or (init and init.byterange):
                raise _RecordingEnded('Byte-range HLS media is unsupported')
            init_url = urljoin(response.url, init.uri) if init else None
            if init_url != self.init_url or (segment.discontinuity and init_url):
                try:
                    self._update_init(init_url, fetch)
                except requests.HTTPError as error:
                    raise _RecordingEnded(
                        f'HLS initialization segment returned HTTP {error.response.status_code}'
                    ) from None
                except requests.RequestException as error:
                    raise _RecordingEnded(
                        f'HLS initialization segment request failed ({type(error).__name__})'
                    ) from None
            ext = 'm4s' if self.init_path else 'ts'
            path = self.path.with_name(f'{self.path.stem}-{len(self.queued_uris):08d}.{ext}')
            media = _Segment(path, self.init_path, segment.duration or 0, segment.discontinuity,
                             segment.current_program_date_time, self.init_fingerprint)
            job = submit(self, urljoin(response.url, segment.uri), media)
            if job is None:
                return False
            self.pending.append((job, media))
            self.queued_uris.add(segment.uri)

        return playlist.is_endlist

    def _update_init(self, url, fetch):
        data = fetch(url).content if url else b''
        if url:
            boxes = list(_iter_mp4_boxes(data))
            if not any(kind == b'moov' for kind, *_ in boxes) or sum(box[3] for box in boxes) != len(data):
                raise _RecordingEnded('Incomplete HLS initialization segment')
        if data != self.initialization:
            self.init_group += 1
            self.init_path = self.path.with_name(f'{self.path.stem}-init-{self.init_group:08d}.mp4') if data else None
            if self.init_path:
                self.init_path.write_bytes(data)
            self.initialization = data
            self.init_fingerprint = sha256(data).digest() if data else b''
        self.init_url = url

    def collect(self, logger, report_resolution=None):
        while self.pending and self.pending[0][0].done():
            job, media = self.pending.popleft()
            if job.cancelled():
                continue
            try:
                completed_at, fingerprint = job.result()
            except requests.HTTPError as error:
                logger.debug('CamSoda segment request returned HTTP %s', error.response.status_code)
                continue
            except (requests.RequestException, _RecordingEnded) as error:
                logger.debug('CamSoda segment unavailable (%s)', type(error).__name__)
                continue
            if fingerprint in self.saved_fingerprints:
                self.skipped_saved_segments = True
                continue
            media.fingerprint = fingerprint
            if not self.segments:
                self.first_program_time = media.program_time
                if report_resolution is not None:
                    report_resolution(_video_dimensions(media.init.read_bytes()) if media.init else None)
            self.segments.append(media)
            self.last_segment_at = max(self.last_segment_at, completed_at)

    def input_options(self):
        groups = []
        for media in self.segments:
            if not groups or media.discontinuity or media.init != groups[-1][-1].init:
                groups.append([])
            groups[-1].append(media)
        options = ['-protocol_whitelist', 'file', '-allowed_extensions', 'mp4,m4s,ts']
        if len(groups) == 1:
            _write_playlist(self.path, groups[0])
            return options + ['-i', str(self.path)]

        # Each group may start a fresh fMP4 timeline. Mux it separately so
        # concat can offset it by the duration of the preceding group.
        parts = []
        for index, group in enumerate(groups):
            playlist = self.path.with_name(f'{self.path.stem}-group-{index}.m3u8')
            part = playlist.with_suffix('.mp4')
            _write_playlist(playlist, group)
            _run_ffmpeg(options + ['-i', str(playlist), '-c', 'copy', str(part)])
            parts.append(f"file '{part.name}'")
        concat = self.path.with_suffix('.ffconcat')
        concat.write_text('ffconcat version 1.0\n' + '\n'.join(parts) + '\n', encoding='ascii')
        return ['-protocol_whitelist', 'file', '-f', 'concat', '-safe', '1', '-i', str(concat)]


def _write_playlist(path, segments):
    target = max(1, ceil(max(media.duration for media in segments)))
    lines = ['#EXTM3U', '#EXT-X-VERSION:7', f'#EXT-X-TARGETDURATION:{target}', '#EXT-X-PLAYLIST-TYPE:VOD']
    if segments[0].init:
        lines.append(f'#EXT-X-MAP:URI="{segments[0].init.name}"')
    for media in segments:
        lines.extend((f'#EXTINF:{media.duration},', media.path.name))
    lines.append('#EXT-X-ENDLIST')
    path.write_text('\n'.join(lines) + '\n', encoding='ascii')


def _complete_fragment(data):
    boxes = list(_iter_mp4_boxes(data))
    kinds = {kind for kind, *_ in boxes}
    return (
        {b'moof', b'mdat'} <= kinds
        and sum(size for _, _, _, size in boxes) == len(data)
        and _invalid_fmp4_fragment_reason(data) is None
    )


def _video_dimensions(initialization):
    movie = next((box for box in _iter_mp4_boxes(initialization) if box[0] == b'moov'), None)
    if movie is None:
        return None
    _, offset, header, size = movie
    for kind, track, track_header, track_size in _iter_mp4_boxes(initialization, offset + header, offset + size):
        if kind != b'trak':
            continue
        children = {box[0]: box[1:] for box in _iter_mp4_boxes(
            initialization, track + track_header, track + track_size
        )}
        if b'tkhd' not in children or b'mdia' not in children:
            continue
        media, media_header, media_size = children[b'mdia']
        for kind, handler, handler_header, handler_size in _iter_mp4_boxes(
            initialization, media + media_header, media + media_size
        ):
            start = handler + handler_header
            if kind != b'hdlr' or handler_size < handler_header + 12 or initialization[start + 8:start + 12] != b'vide':
                continue
            track_header_offset, header_size, box_size = children[b'tkhd']
            if box_size < header_size + 84:
                continue
            end = track_header_offset + box_size
            width = int.from_bytes(initialization[end - 8:end - 4], 'big') >> 16
            height = int.from_bytes(initialization[end - 4:end], 'big') >> 16
            if width and height:
                return width, height
    return None


def _remux(tracks, filename):
    cmd = []
    first_times = [track.first_program_time for track in tracks]
    start = min(first_times) if len(tracks) == 2 and all(first_times) else None
    for track in tracks:
        if start is not None:
            offset = (track.first_program_time - start).total_seconds()
            if offset:
                cmd.extend(['-itsoffset', str(offset)])
        cmd.extend(track.input_options())
    if len(tracks) == 2:
        cmd.extend(['-map', '0:v:0', '-map', '1:a:0'])
    cmd.extend(['-c', 'copy'])
    if SEGMENT_TIME is not None:
        cmd.extend(['-f', 'segment', '-reset_timestamps', '1', '-segment_time', str(SEGMENT_TIME)])
        filename = f'{os.path.splitext(filename)[0]}_%03d.{CONTAINER}'
    cmd.append(filename)
    _run_ffmpeg(cmd)


def _run_ffmpeg(options):
    # On Windows the mux must have no console so Ctrl+C is handled by StreaMonitor.
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    cmd = [FFMPEG_PATH, '-nostdin', '-hide_banner', '-loglevel', 'error', *options]
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   creationflags=flags, start_new_session=os.name != 'nt')


def _remove_capture_directory(directory, logger):
    # Windows may briefly keep recently closed media files locked.
    for attempt in range(5):
        try:
            shutil.rmtree(directory)
            return
        except OSError as error:
            if isinstance(error, FileNotFoundError) and not directory.exists():
                return
            if attempt == 4:
                logger.warning('CamSoda HLS could not remove temporary files in %s: %s', directory, error)
                return
        sleep(0.25)


def getVideoCamSodaHLS(self, urls, filename):
    stop = Event()
    self.stopDownload = stop.set
    directory = None
    tracks = []
    remuxed = False
    playlist_failure = None
    discard_duplicate_capture = False

    def stopping():
        return stop.is_set() or not self.running or self.quitting

    def report_resolution(dimensions):
        if dimensions:
            self.logger.info('Recording resolution: %sx%s', *dimensions)
        else:
            self.logger.info('Recording resolution unavailable in the HLS initialization segment')

    try:
        if stopping():
            return True
        history = getattr(self, '_camsoda_saved_segments', None)
        if history is None:
            history = self._camsoda_saved_segments = {}
        directory = Path(tempfile.mkdtemp(prefix='.camsoda-', dir=Path(filename).parent))
        with requests.Session() as session:
            drain_deadline = None
            for index, url in enumerate(urls):
                saved = history.setdefault(index, deque(maxlen=RECENT_SAVED_SEGMENTS))
                tracks.append(_MediaTrack(url, directory / f'track-{index}.m3u8', saved))

            def request(client, url, cancelled):
                for attempt in range(3):
                    if cancelled():
                        raise _RecordingEnded('Stop requested')
                    try:
                        response = client.get(url, headers=self.headers, cookies=self.cookies, timeout=(4, 5))
                        response.raise_for_status()
                        return response
                    except requests.HTTPError as error:
                        if attempt == 2 or error.response.status_code not in RETRYABLE_HTTP_STATUSES:
                            raise
                    except RETRYABLE_REQUEST_ERRORS:
                        if attempt == 2:
                            raise
                    stop.wait(0.5 * (attempt + 1))

            def fetch(url):
                return request(session, url, stopping)

            def drain_expired():
                return drain_deadline is not None and monotonic() >= drain_deadline

            def download(client, url, media, saved_fingerprints):
                data = request(client, url, drain_expired).content
                if not data or (media.init and not _complete_fragment(data)):
                    raise _RecordingEnded('Incomplete HLS media segment')
                digest = sha256(media.init_fingerprint)
                digest.update(data)
                fingerprint = digest.digest()
                if fingerprint not in saved_fingerprints:
                    media.path.write_bytes(data)
                return monotonic(), fingerprint

            def submit(track, url, media):
                if stopping() or sum(len(track.pending) for track in tracks) >= SEGMENT_QUEUE_LIMIT:
                    return None
                return pool.submit(url, media, track.saved_fingerprints)

            def collect():
                for index, track in enumerate(tracks):
                    track.collect(self.logger, report_resolution if index == 0 else None)

            finished = set()
            waiting_reported = False
            video = tracks[0]
            retrying_video = None
            with _SegmentWorkers(download) as pool:
                try:
                    while not stopping() and len(finished) < len(tracks):
                        collect()
                        for index, track in enumerate(tracks):
                            if track in finished or stopping():
                                continue
                            try:
                                if track.poll(fetch, self._load_m3u8, stopping, submit):
                                    finished.add(track)
                                if index == 0 and retrying_video is not None:
                                    self.logger.info('CamSoda HLS video playlist is responding again')
                                    retrying_video = None
                            except requests.HTTPError as error:
                                status = error.response.status_code
                                if index:
                                    self.logger.debug('CamSoda audio playlist returned HTTP %s', status)
                                    continue
                                if status == 429:
                                    self.sc = Status.RATELIMIT
                                    self.ratelimit = True
                                if status in (404, 410):
                                    self.sc = Status.OFFLINE
                                transient = status in RETRYABLE_HTTP_STATUSES
                                reason = f'video playlist returned HTTP {status}'
                            except (requests.RequestException, _RecordingEnded) as error:
                                if index:
                                    self.logger.debug('CamSoda audio playlist unavailable (%s)', type(error).__name__)
                                    continue
                                if isinstance(error, _RecordingEnded):
                                    raise
                                transient = isinstance(error, RETRYABLE_REQUEST_ERRORS)
                                reason = f'video playlist request failed ({type(error).__name__})'
                            else:
                                continue
                            attempts = 3 if transient else 1
                            failure = _PlaylistRequestFailed(f'{reason} after {attempts} attempt(s)')
                            video.collect(self.logger, report_resolution)
                            if not transient or not video.segments:
                                raise failure
                            if retrying_video is None:
                                self.logger.warning('CamSoda HLS %s; keeping the recording open and retrying', failure)
                            retrying_video = failure
                        collect()
                        if len(finished) == len(tracks) or stopping():
                            break
                        if (not waiting_reported and video not in finished and not video.segments
                                and not video.pending and (not video.queued_uris or video.skipped_saved_segments)):
                            self.logger.info('Channel online; waiting for new video segments.')
                            waiting_reported = True
                        remaining = VIDEO_PROGRESS_TIMEOUT - (monotonic() - video.last_segment_at)
                        if remaining <= 0:
                            if retrying_video is not None:
                                raise _PlaylistRequestFailed(
                                    f'{retrying_video}; no new video segments for {VIDEO_PROGRESS_TIMEOUT} seconds'
                                )
                            raise _RecordingEnded('No new HLS video segments for 60 seconds')
                        stop.wait(min(PLAYLIST_POLL_INTERVAL, remaining))
                finally:
                    drain_deadline = monotonic() + SEGMENT_STOP_GRACE
                    pending = [job for track in tracks for job, _ in track.pending]
                    _, unfinished = wait(pending, timeout=SEGMENT_STOP_GRACE)
                    wait([job for job in unfinished if not job.cancel()])
                    collect()
    except _PlaylistRequestFailed as error:
        playlist_failure = error
    except _RecordingEnded as error:
        if not stopping():
            self.logger.warning('CamSoda HLS ended: %s', error)
    except (OSError, ValueError, KeyError) as error:
        self.logger.warning('CamSoda HLS capture ended (%s)', type(error).__name__)
    finally:
        self.stopDownload = None

    try:
        recorded = [track for track in tracks if track.segments]
        if playlist_failure and (not recorded or recorded[0] is not tracks[0]):
            self.logger.warning('CamSoda HLS %s; no new video was saved', playlist_failure)
            self.download_error_reported = True
        discard_duplicate_capture = bool(tracks and tracks[0].skipped_saved_segments and not tracks[0].segments)
        if discard_duplicate_capture:
            return True
        if not recorded:
            return stopping()
        if recorded[0] is not tracks[0]:
            return False
        _remux(recorded, filename)
        remuxed = True
        if playlist_failure:
            self.logger.warning('CamSoda HLS %s; saved %s with the video received',
                                playlist_failure, Path(filename).name if SEGMENT_TIME is None else 'the output files')
        for track in recorded:
            track.saved_segments.extend(media.fingerprint for media in track.segments)
        return True
    except subprocess.CalledProcessError as error:
        self.logger.error('CamSoda HLS remux failed: %s', error.stderr.decode(errors='replace').strip())
        self.download_error_reported = True
        return False
    except OSError as error:
        self.logger.error('CamSoda HLS remux failed: %s', error)
        self.download_error_reported = True
        return False
    finally:
        if directory is not None:
            if remuxed or discard_duplicate_capture or not any(path.suffix in ('.m4s', '.ts') and path.stat().st_size
                                  for path in directory.iterdir()):
                _remove_capture_directory(directory, self.logger)
            else:
                self.logger.warning('CamSoda HLS kept downloaded data in %s', directory)
