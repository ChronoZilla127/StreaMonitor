"""Download complete CamSoda HLS segments before remuxing them."""

import os
import subprocess
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from threading import Event
from time import monotonic
from urllib.parse import urljoin, urlsplit

import m3u8
import requests
from parameters import FFMPEG_PATH, SEGMENT_TIME

from streamonitor.downloaders import DownloadSourceUnavailable
from streamonitor.downloaders.hls import _invalid_fmp4_fragment_reason, _iter_mp4_boxes
from streamonitor.downloaders.postprocess import run_ffmpeg_postprocess


class CaptureError(Exception):
    pass


class PlaylistStalled(CaptureError):
    pass


class HLSForbidden(CaptureError):
    pass


def _box_payloads(data, kind):
    return [
        data[offset + header:offset + size]
        for box, offset, header, size in _iter_mp4_boxes(data)
        if box == kind
    ]


def _init_layout(data):
    """Describe the fields needed to interpret subsequent fMP4 fragments."""
    if not data:
        return None
    try:
        movie, = _box_payloads(data, b'moov')
        header, = _box_payloads(movie, b'mvhd')
        if header[0] not in (0, 1):
            return None
        movie_clock = 12 if header[0] == 0 else 20
        defaults, = _box_payloads(movie, b'mvex')
        track_defaults = _box_payloads(defaults, b'trex')
        tracks = []
        for track in _box_payloads(movie, b'trak'):
            track_header, = _box_payloads(track, b'tkhd')
            media, = _box_payloads(track, b'mdia')
            media_header, = _box_payloads(media, b'mdhd')
            handler, = _box_payloads(media, b'hdlr')
            info, = _box_payloads(media, b'minf')
            samples, = _box_payloads(info, b'stbl')
            description, = _box_payloads(samples, b'stsd')
            if track_header[0] not in (0, 1) or media_header[0] not in (0, 1):
                return None
            track_id = 12 if track_header[0] == 0 else 20
            media_clock = 12 if media_header[0] == 0 else 20
            tracks.append((
                track_header[track_id:track_id + 4],
                media_header[media_clock:media_clock + 4],
                handler[8:12],
                description,
                _box_payloads(track, b'edts'),
            ))
        if not tracks or not track_defaults:
            return None
        return header[movie_clock:movie_clock + 4], tracks, track_defaults
    except (ValueError, IndexError):
        return None


def _fetch(session, url, stop, resource='media segment'):
    for _ in range(2):
        if stop.is_set():
            raise InterruptedError
        try:
            response = session.get(url, timeout=(3, 5))
        except requests.RequestException:
            failure = CaptureError('network error')
        else:
            if response.ok:
                return response
            if response.status_code == 403:
                raise HLSForbidden('HTTP 403')
            if 500 <= response.status_code < 600:
                failure = DownloadSourceUnavailable(
                    f'CamSoda HLS server returned HTTP {response.status_code} for {resource}'
                )
            else:
                failure = CaptureError(f'HTTP {response.status_code}')
            if response.status_code not in (500, 502, 503, 504):
                break
    raise failure


def _playlist(session, url, stop, parse=m3u8.loads):
    response = _fetch(session, url, stop, 'playlist')
    if not response.content.lstrip().startswith(b'#EXTM3U'):
        raise CaptureError('invalid HLS playlist')
    return response.url, parse(response.text)


def _media_urls(bot, url, master):
    if not master.is_variant:
        return [url]
    video = bot.getWantedResolutionPlaylist(url, m3u_data=master)
    if video is None:
        raise CaptureError('no matching HLS quality')
    variant = next((item for item in master.playlists if urljoin(url, item.uri) == video), None)
    if variant is None:
        raise CaptureError('selected HLS variant is missing from the master playlist')
    audio = [
        item for item in master.media
        if item.type == 'AUDIO' and item.group_id == variant.stream_info.audio
    ]
    selected = next((item for item in audio if item.default == 'YES'), audio[0] if audio else None)
    return [video, urljoin(url, selected.uri)] if selected and selected.uri else [video]


class MediaTrack:
    def __init__(self, url, output, stack, resume_after=None, same_stream_path=True):
        self.url = url
        self.output = output
        self.stack = stack
        self.parts = [Path(output.name)]
        self.next_sequence = None
        self.resume_after = resume_after
        self.same_stream_path = same_stream_path
        self.last_saved = None
        self.init_url = None
        self.initialization = None
        self.init_layout = None
        self.segments_written = 0
        self.last_progress = monotonic()
        self.ended = False

    def _initial_sequence(self, playlist, segments, start):
        if self.resume_after is None:
            return start if playlist.is_endlist else max(start, start + len(segments) - 3)
        last_sequence, last_time, last_duration = self.resume_after
        if last_time is not None and not self.same_stream_path:
            # Alternate playlists can shift program dates slightly. Allow
            # half a segment of drift before treating it as new media.
            last_time += timedelta(seconds=last_duration / 2)
        for sequence, segment in enumerate(segments, start):
            segment_time = getattr(segment, 'current_program_date_time', None)
            if last_time is not None and segment_time is not None:
                if segment_time > last_time:
                    return sequence
            elif not self.same_stream_path or sequence > last_sequence:
                return sequence
        return start + len(segments)

    def download(self, session, stop):
        if self.ended:
            return
        base, playlist = _playlist(session, self.url, stop)
        segments = playlist.segments
        # LL-HLS can end with partial segments but no complete segment URI.
        # That placeholder becomes a complete segment on a subsequent reload.
        if segments and not segments[-1].uri:
            segments = segments[:-1]
        start = playlist.media_sequence or 0
        end = start + len(segments)
        sequence = self.next_sequence
        if sequence is None:
            sequence = self._initial_sequence(playlist, segments, start)
        if start > sequence or end < sequence:
            raise CaptureError(f'HLS sequence changed unexpectedly (expected {sequence}, window {start}–{end - 1})')

        for index in range(sequence - start, len(segments)):
            if stop.is_set():
                raise InterruptedError
            segment = segments[index]
            if not segment.uri:
                raise CaptureError(f'HLS segment has no URI at sequence {start + index}')
            if segment.byterange or (segment.key and segment.key.method != 'NONE'):
                raise CaptureError('unsupported HLS byte range or encryption')
            if getattr(segment, 'gap_tag', False) or (segment.discontinuity and self.segments_written):
                raise CaptureError(f'HLS gap or discontinuity at sequence {start + index}')
            init = segment.init_section
            init_url = urljoin(base, init.uri) if init else None
            if init and init.byterange:
                raise CaptureError('unsupported HLS initialization byte range')
            initialization = self.initialization
            layout = self.init_layout
            new_part = False
            if init_url != self.init_url:
                initialization = _fetch(session, init_url, stop, 'initialization file').content if init_url else None
                if init_url and (not initialization or initialization.lstrip().startswith(b'#EXTM3U')):
                    raise CaptureError('invalid HLS initialization response')
                if initialization != self.initialization:
                    layout = _init_layout(initialization)
                    new_part = self.segments_written and (layout is None or layout != self.init_layout)
            data = _fetch(session, urljoin(base, segment.uri), stop).content
            if not data or data.lstrip().startswith(b'#EXTM3U') or _invalid_fmp4_fragment_reason(data):
                raise CaptureError(f'invalid HLS fragment at sequence {start + index}')
            if new_part:
                self.output.close()
                first = self.parts[0]
                part = first.with_name(f'{first.stem}-{len(self.parts)}{first.suffix}')
                self.output = self.stack.enter_context(part.open('wb'))
                self.parts.append(part)
            if initialization and (new_part or not self.segments_written):
                self.output.write(initialization)
            self.output.write(data)
            self.init_url = init_url
            self.initialization = initialization
            self.init_layout = layout
            self.next_sequence = start + index + 1
            self.last_saved = (
                start + index, getattr(segment, 'current_program_date_time', None),
                getattr(segment, 'duration', None) or 0,
            )
            self.segments_written += 1
            self.last_progress = monotonic()

        target = playlist.target_duration or 2
        if not playlist.is_endlist and monotonic() - self.last_progress > max(10, 3 * target):
            raise PlaylistStalled('HLS playlist stopped advancing')
        self.ended = playlist.is_endlist
        self.poll_delay = max(0.25, target / 2)


def _join_track(track):
    if len(track.parts) == 1:
        return track.parts[0]

    # Each MP4 part needs its own init. MPEG-TS carries codec parameters in
    # the stream and allows us to join the parts using their original PTS.
    joined = track.parts[0].with_suffix('.ts')
    with joined.open('wb') as output:
        for part in track.parts:
            result = run_ffmpeg_postprocess([
                FFMPEG_PATH, '-nostdin', '-loglevel', 'error', '-xerror', '-copyts',
                '-i', str(part), '-map', '0:v?', '-map', '0:a?', '-c', 'copy',
                '-mpegts_copyts', '1', '-mpegts_flags', '+initial_discontinuity',
                '-muxdelay', '0', '-f', 'mpegts', '-',
            ], stdout=output, stderr=subprocess.PIPE)
            if result.returncode:
                raise CaptureError(f'FFmpeg could not join HLS part {part.name} (exit {result.returncode})')
    return joined


def _remux(tracks, directory, filename):
    command = [FFMPEG_PATH, '-nostdin', '-loglevel', 'error', '-copyts']
    for track in tracks:
        command.extend(['-i', str(_join_track(track))])
    audio = '1:a:0' if len(tracks) == 2 else '0:a:0?'
    command.extend(['-map', '0:v:0', '-map', audio, '-c', 'copy', '-avoid_negative_ts', 'make_zero'])
    output_dir = directory / 'output'
    output_dir.mkdir()
    output = filename.name
    if SEGMENT_TIME is not None:
        command.extend(['-f', 'segment', '-reset_timestamps', '1', '-segment_time', str(SEGMENT_TIME)])
        output = filename.stem.replace('%', '%%') + '_%03d' + filename.suffix
    command.append(str(output_dir / output))
    result = run_ffmpeg_postprocess(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    outputs = sorted(output_dir.iterdir())
    if result.returncode or not outputs:
        raise CaptureError(f'FFmpeg remux failed (exit {result.returncode})')
    for path in outputs:
        os.replace(path, filename.parent / path.name)


def getVideoCamSoda(bot, url, filename):
    filename = Path(filename)
    directory = Path(mkdtemp(prefix='.camsoda-hls-', dir=filename.parent))
    stop = Event()
    bot.stopDownload = stop.set
    failed = False
    stopped_reason = None
    finalized = False
    tracks = []
    stream_path = urlsplit(url).path
    saved_segments = bot.camsoda_saved_segments
    try:
        if not bot.running:
            return True
        with requests.Session() as session, ExitStack() as stack:
            session.headers.update(bot.headers)
            if bot.cookies:
                session.cookies.update(bot.cookies)
            try:
                base, master = _playlist(session, url, stop, bot._load_m3u8)
                for index, media in enumerate(_media_urls(bot, base, master)):
                    output = stack.enter_context((directory / f'track-{index}.media').open('wb'))
                    key = (stream_path, index)
                    checkpoint = saved_segments.get(key)
                    if checkpoint is None:
                        checkpoint = next(
                            (saved for (_, saved_index), saved in saved_segments.items()
                             if saved_index == index and saved[1] is not None),
                            None,
                        )
                    tracks.append(MediaTrack(media, output, stack, checkpoint, key in saved_segments))
                while not stop.is_set():
                    for track in tracks:
                        track.download(session, stop)
                    active = [track for track in tracks if not track.ended]
                    if not active:
                        break
                    stop.wait(min(track.poll_delay for track in active))
            except InterruptedError:
                pass
            except (PlaylistStalled, HLSForbidden) as exc:
                stopped_reason = exc
                bot._edge_index += 1
            except (CaptureError, DownloadSourceUnavailable, ValueError, KeyError) as exc:
                bot._edge_index += 1
                if isinstance(exc, DownloadSourceUnavailable) and not any(track.segments_written for track in tracks):
                    raise
                failed = True
                bot.logger.warning(f'CamSoda Custom ended this capture: {exc}')

        waiting_for_new_media = tracks and all(
            track.resume_after is not None and not track.segments_written for track in tracks
        )
        if waiting_for_new_media and not failed and not isinstance(stopped_reason, HLSForbidden):
            bot.defer_download_attempt(bot.sleep_on_error)
            bot.logger.debug('CamSoda Custom: waiting for new HLS fragments')
            return True

        if not tracks or not all(track.segments_written for track in tracks):
            if stopped_reason:
                bot.logger.warning(f'CamSoda Custom: {stopped_reason} before all tracks had media')
            if any(track.segments_written for track in tracks):
                bot.logger.warning(f'CamSoda Custom kept partial tracks in {directory}')
            return False
        try:
            _remux(tracks, directory, filename)
        except (CaptureError, OSError) as exc:
            bot.logger.error(f'CamSoda Custom could not finalize the recording: {exc}. Tracks kept in {directory}')
            return False
        finalized = True
        bot.camsoda_saved_segments = {
            (stream_path, index): track.last_saved for index, track in enumerate(tracks)
        }
        if stopped_reason:
            bot.logger.info(f'CamSoda Custom: {stopped_reason}; recording saved')
            if isinstance(stopped_reason, HLSForbidden):
                bot.defer_download_attempt(bot.sleep_on_error)
        return not failed
    finally:
        bot.stopDownload = None
        if finalized or not any(track.segments_written for track in tracks):
            rmtree(directory)
