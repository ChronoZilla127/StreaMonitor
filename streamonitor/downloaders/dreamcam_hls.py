import json
import os
import shutil
import subprocess
import tempfile
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import ceil
from pathlib import Path
from queue import Empty, Queue, SimpleQueue
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import urljoin, urlsplit

import requests
from urllib3.exceptions import HTTPError as ResponseReadError

from parameters import CONTAINER, FFMPEG_PATH, SEGMENT_TIME
from streamonitor.downloaders.hls import _invalid_fmp4_fragment_reason, _iter_mp4_boxes
from streamonitor.enums import Status


POLL_INTERVAL = 0.75
VIDEO_TIMEOUT = 15
MAX_PENDING = 12
RECENT_SEGMENTS = 64
HTTP_TIMEOUT = (4, 5)
FRAGMENT_TIMEOUT = 8
FRAGMENT_SOCKET_TIMEOUT = 3


@dataclass(frozen=True)
class _Fragment:
    sequence: int
    url: str
    path: Path
    init: Path
    duration: float
    discontinuity: bool


def _complete_mp4(data, *required):
    boxes = list(_iter_mp4_boxes(data))
    return sum(size for _, _, _, size in boxes) == len(data) and set(required) <= {box[0] for box in boxes}


class _StallDiagnostic:
    def __init__(self):
        self.events = deque(maxlen=256)
        self.lock = Lock()

    def add(self, kind, **details):
        with self.lock:
            self.events.append((monotonic(), {'kind': kind, **details}))

    def save(self, filename, last_progress, pending, next_sequence):
        with self.lock:
            events = [dict(event, seconds_after_progress=round(when - last_progress, 3))
                      for when, event in self.events if when >= last_progress]
        playlists = [event for event in events if event['kind'] == 'playlist']
        complete = [event for event in playlists if event['complete_segments']]
        ranges = {(event['first_complete'], event['last_complete']) for event in complete}
        if not playlists:
            pattern = 'no_playlist_response'
        elif not complete:
            pattern = 'partial_segments_only' if any(event['parts'] for event in playlists) else 'empty_playlist'
        elif any(event['kind'] in ('segment_failure', 'initialization_failure') for event in events):
            pattern = 'media_download_failures'
        elif len(complete) > 1 and len(ranges) == 1:
            pattern = 'complete_segments_not_advancing'
        elif any(entry['state'] == 'running' for entry in pending):
            pattern = 'media_requests_in_flight'
        else:
            pattern = 'undetermined'
        path = Path(filename + '.hls-stall.json')
        path.write_text(json.dumps({
            'captured_at_utc': datetime.now(timezone.utc).isoformat(),
            'seconds_without_progress': round(monotonic() - last_progress, 3),
            'pattern': pattern,
            'next_sequence': next_sequence,
            'pending_sequences': [entry['sequence'] for entry in pending],
            'pending_requests': pending,
            'events': events,
        }, indent=2) + '\n', encoding='utf-8')
        return path, pattern


def _ffmpeg(directory, args):
    executable = shutil.which(FFMPEG_PATH) or FFMPEG_PATH
    if os.path.dirname(executable):
        executable = os.path.abspath(executable)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    subprocess.run([executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', *args],
                   cwd=directory, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
                   creationflags=flags, start_new_session=os.name != 'nt')


def _assemble(directory, fragments, filename):
    groups = []
    for fragment in fragments:
        if not groups or fragment.discontinuity or fragment.init != groups[-1][-1].init:
            groups.append([])
        groups[-1].append(fragment)

    inputs = []
    for index, group in enumerate(groups):
        playlist = directory / f'part-{index}.m3u8'
        target = max(1, ceil(max(fragment.duration for fragment in group)))
        lines = ['#EXTM3U', '#EXT-X-VERSION:7', f'#EXT-X-TARGETDURATION:{target}',
                 '#EXT-X-PLAYLIST-TYPE:VOD', f'#EXT-X-MAP:URI="{group[0].init.name}"']
        for fragment in group:
            lines.extend((f'#EXTINF:{fragment.duration},', fragment.path.name))
        playlist.write_text('\n'.join([*lines, '#EXT-X-ENDLIST']) + '\n', encoding='ascii')
        inputs.append(['-protocol_whitelist', 'file', '-allowed_extensions', 'mp4,m4s', '-i', playlist.name])

    if len(inputs) == 1:
        command = inputs[0]
    else:
        parts = []
        for index, source in enumerate(inputs):
            part = f'part-{index}.mp4'
            _ffmpeg(directory, [*source, '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', part])
            parts.append(f"file '{part}'")
        (directory / 'parts.ffconcat').write_text('ffconcat version 1.0\n' + '\n'.join(parts) + '\n', encoding='ascii')
        command = ['-protocol_whitelist', 'file', '-f', 'concat', '-safe', '1', '-i', 'parts.ffconcat']

    command.extend(['-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy'])
    if SEGMENT_TIME is not None:
        command.extend(['-f', 'segment', '-reset_timestamps', '1', '-segment_time', str(SEGMENT_TIME)])
        filename = f'{os.path.splitext(filename)[0]}_%03d.{CONTAINER}'
    _ffmpeg(directory, [*command, os.path.abspath(filename)])


def getVideoDreamCam(self, url, filename):
    stop = Event()
    self.stopDownload = stop.set
    directory = None
    saved = False
    recorded = []
    pending = deque()
    jobs = Queue()
    progress = SimpleQueue()
    workers = []
    oldest = None
    drain_deadline = None
    last_progress = monotonic()
    init_url = None
    init_path = None
    init_number = 0
    playlist_id = urlsplit(url).path
    prior_playlist, prior_fingerprints = getattr(self, '_dreamcam_saved_fragments', (None, ()))
    saved_fingerprints = deque(prior_fingerprints if prior_playlist == playlist_id else (), maxlen=RECENT_SEGMENTS)
    already_saved = set(saved_fingerprints)
    recorded_fingerprints = deque(maxlen=RECENT_SEGMENTS)
    duplicates = 0
    next_sequence = None
    diagnostic = _StallDiagnostic()

    def stopping():
        return stop.is_set() or not self.running or self.quitting

    def fetch(session, target):
        response = session.get(target, headers=self.headers, cookies=self.cookies, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return response

    def read_fragment(session, url, deadline):
        with session.get(url, headers=self.headers, cookies=self.cookies, stream=True,
                         timeout=FRAGMENT_SOCKET_TIMEOUT) as response:
            response.raise_for_status()
            read1 = getattr(response.raw, 'read1', None)
            # Read available bytes so a slow response cannot keep filling a large chunk indefinitely.
            chunks = (iter(lambda: read1(65536, decode_content=True), b'')
                      if read1 else response.iter_content(chunk_size=8192))
            data = bytearray()
            for chunk in chunks:
                if monotonic() >= deadline or (drain_deadline is not None and monotonic() >= drain_deadline):
                    raise requests.Timeout('DreamCam fragment download deadline reached')
                data.extend(chunk)
            if monotonic() >= deadline:
                raise requests.Timeout('DreamCam fragment download deadline reached')
            return bytes(data)

    def download(session, fragment):
        deadline = monotonic() + FRAGMENT_TIMEOUT
        for attempt in range(2):
            if (monotonic() >= deadline or (drain_deadline is not None and monotonic() >= drain_deadline)
                    or (oldest is not None and fragment.sequence < oldest)):
                diagnostic.add('segment_failure', sequence=fragment.sequence, reason='expired_or_deadline')
                return None
            try:
                data = read_fragment(session, fragment.url, deadline)
            except requests.HTTPError as error:
                status = error.response.status_code
                diagnostic.add('segment_failure', sequence=fragment.sequence, http_status=status)
                if status in (404, 410) or attempt or status not in (408, 500, 502, 503, 504):
                    self.logger.debug('DreamCam segment %s returned HTTP %s', fragment.sequence, status)
                    return None
            except (requests.RequestException, ResponseReadError) as error:
                diagnostic.add('segment_failure', sequence=fragment.sequence, reason=type(error).__name__)
                if attempt:
                    return None
            else:
                if _complete_mp4(data, b'moof', b'mdat') and not _invalid_fmp4_fragment_reason(data):
                    fingerprint = sha256(data).digest()
                    if fingerprint not in already_saved:
                        fragment.path.write_bytes(data)
                        completed = monotonic()
                        progress.put(completed)
                        return completed, fingerprint
                    return monotonic(), fingerprint
                diagnostic.add('segment_failure', sequence=fragment.sequence, reason='incomplete_fmp4')
        return None

    def worker():
        with requests.Session() as session:
            while (job := jobs.get()) is not None:
                result, fragment = job
                if not result.set_running_or_notify_cancel():
                    continue
                try:
                    result.set_result(download(session, fragment))
                except Exception as error:
                    result.set_exception(error)

    def collect():
        nonlocal duplicates, last_progress
        while True:
            try:
                last_progress = max(last_progress, progress.get_nowait())
            except Empty:
                break
        while pending and pending[0][0].done():
            future, fragment = pending.popleft()
            if future.cancelled() or (result := future.result()) is None:
                continue
            completed, fingerprint = result
            if fingerprint in already_saved:
                duplicates += 1
                diagnostic.add('duplicate_segment', sequence=fragment.sequence)
                continue
            if recorded and fragment.sequence > recorded[-1].sequence + 1:
                self.logger.warning('DreamCam skipped %s unavailable segment(s)',
                                    fragment.sequence - recorded[-1].sequence - 1)
            recorded.append(fragment)
            recorded_fingerprints.append(fingerprint)
            last_progress = max(last_progress, completed)

    try:
        if stopping():
            return True
        directory = Path(tempfile.mkdtemp(prefix='.dreamcam-', dir=Path(filename).parent))
        try:
            for _ in range(2):
                thread = Thread(target=worker, name='dreamcam-hls')
                thread.start()
                workers.append(thread)

            with requests.Session() as session:
                while not stopping():
                    collect()
                    if monotonic() - last_progress >= VIDEO_TIMEOUT:
                        self.logger.warning('DreamCam HLS supplied no new complete segments for %s seconds (%s already saved)',
                                            VIDEO_TIMEOUT, duplicates)
                        try:
                            requests_pending = [
                                {'sequence': fragment.sequence,
                                 'state': 'done' if future.done() else 'running' if future.running() else 'queued'}
                                for future, fragment in pending
                            ]
                            path, pattern = diagnostic.save(filename, last_progress, requests_pending, next_sequence)
                            self.logger.info('Saved DreamCam stall diagnostic (%s) to %s', pattern, path.name)
                        except OSError as error:
                            self.logger.warning('Could not save DreamCam stall diagnostic (%s)', type(error).__name__)
                        break
                    reading_playlist = True
                    try:
                        response = fetch(session, url)
                        reading_playlist = False
                        playlist = self._load_m3u8(response.text)
                        if not response.text.lstrip('\ufeff').startswith('#EXTM3U') or playlist.is_variant:
                            raise ValueError('Expected a DreamCam HLS media playlist')
                        segments = [segment for segment in playlist.segments if segment.uri and not segment.gap_tag]
                        diagnostic.add('playlist', media_sequence=playlist.media_sequence,
                                       complete_segments=len(segments),
                                       first_complete=segments[0].media_sequence if segments else None,
                                       last_complete=segments[-1].media_sequence if segments else None,
                                       parts=sum(len(segment.parts) for segment in playlist.segments))
                        if segments:
                            if next_sequence is None:
                                start = segments if playlist.is_endlist else segments[-2:]
                                next_sequence = start[0].media_sequence
                            oldest = playlist.media_sequence
                            for result, fragment in pending:
                                if fragment.sequence < oldest:
                                    if result.cancel():
                                        diagnostic.add('segment_failure', sequence=fragment.sequence, reason='expired_in_queue')

                        for segment in segments:
                            if stopping() or len(pending) >= MAX_PENDING:
                                break
                            if segment.media_sequence < next_sequence:
                                continue
                            init = segment.init_section
                            if (not init or init.byterange or segment.byterange
                                    or (segment.key and segment.key.method != 'NONE')):
                                raise ValueError('Expected unencrypted DreamCam fMP4 segments')
                            segment_init = urljoin(response.url, init.uri)
                            if segment_init != init_url or segment.discontinuity:
                                data = fetch(session, segment_init).content
                                if not _complete_mp4(data, b'moov'):
                                    raise ValueError('Incomplete DreamCam initialization segment')
                                init_number += 1
                                init_path = directory / f'init-{init_number}.mp4'
                                init_path.write_bytes(data)
                                init_url = segment_init
                            sequence = segment.media_sequence
                            fragment = _Fragment(sequence, urljoin(response.url, segment.uri),
                                                 directory / f'segment-{sequence}.m4s', init_path,
                                                 segment.duration, segment.discontinuity)
                            result = Future()
                            pending.append((result, fragment))
                            jobs.put((result, fragment))
                            next_sequence = sequence + 1
                        if playlist.is_endlist and (not segments or next_sequence > segments[-1].media_sequence):
                            break
                    except requests.HTTPError as error:
                        status = error.response.status_code
                        diagnostic.add('playlist_failure' if reading_playlist else 'initialization_failure', http_status=status)
                        if reading_playlist and status in (403, 404, 410, 429):
                            if status == 429:
                                self.sc = Status.RATELIMIT
                                self.ratelimit = True
                            self.logger.info('DreamCam playlist returned HTTP %s', status)
                            break
                    except requests.RequestException as error:
                        diagnostic.add('playlist_failure' if reading_playlist else 'initialization_failure',
                                       reason=type(error).__name__)
                        self.logger.debug('DreamCam HLS request failed (%s)', type(error).__name__)
                    stop.wait(POLL_INTERVAL)
        except (OSError, ValueError, KeyError) as error:
            self.logger.warning('DreamCam capture ended (%s)', type(error).__name__)
        finally:
            drain_deadline = monotonic() + 5
            for thread in workers:
                jobs.put(None)
            for thread in workers:
                thread.join()
            collect()

        if not recorded:
            if not stopping() and self.sc == Status.PUBLIC:
                self.defer_download_attempt(2)
            return stopping()
        _assemble(directory, recorded, filename)
        saved_fingerprints.extend(recorded_fingerprints)
        self._dreamcam_saved_fragments = (playlist_id, saved_fingerprints)
        saved = True
        return True
    except subprocess.CalledProcessError as error:
        self.logger.error('DreamCam MP4 assembly failed: %s', error.stderr.decode(errors='replace').strip())
    except OSError as error:
        self.logger.error('DreamCam HLS capture failed (%s)', type(error).__name__)
    finally:
        self.stopDownload = None
        if directory is not None:
            if saved or not any(directory.glob('*.m4s')):
                try:
                    shutil.rmtree(directory)
                except OSError:
                    self.logger.warning('Could not remove DreamCam capture directory %s', directory)
            else:
                self.logger.warning('DreamCam kept downloaded fragments in %s', directory)
    return False
