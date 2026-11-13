from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from random import randint
from threading import Lock
from time import monotonic
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import requests

from parameters import env
from streamonitor.bot import Bot
from streamonitor.downloaders.camsoda_hls import getVideoCamSodaHLS
from streamonitor.enums import Status


def _playlist_url(base, uri, params):
    target = urlsplit(urljoin(base, uri))
    source = urlsplit(base)
    if (target.scheme, target.netloc) != (source.scheme, source.netloc):
        return target.geturl()
    existing = dict(parse_qsl(target.query, keep_blank_values=True))
    inherited = urlencode({key: value for key, value in params.items() if key not in existing})
    query = '&'.join(part for part in (target.query, inherited) if part)
    return target._replace(query=query).geturl()


class CamSoda(Bot):
    site = 'CamSoda'
    siteslug = 'CS'
    status_messages = {**Bot.status_messages, Status.UNKNOWN: 'CamSoda status unavailable'}
    _status_lock = Lock()
    _status_retry_at = 0

    def __init__(self, username):
        super().__init__(username)
        self._first_status_check = True
        self.custom_hls = env.bool('CAMSODA_CUSTOM_HLS', default=False)
        if self.custom_hls:
            self.getVideo = getVideoCamSodaHLS

    def getWebsiteURL(self):
        return "https://www.camsoda.com/" + self.username

    def getVideoUrl(self):
        track_params = "filter=tracks:v4v3v2v1a1a2&multitrack=true"
        stream = self.lastInfo['stream']
        server = stream['edge_servers'][0]
        stream_name = stream['stream_name']
        token = stream['token']
        base = f"https://{server}/{stream_name}_v1/"
        url = f"{base}index.ll.m3u8?{track_params}&token={token}"
        if not self.custom_hls:
            return self.getWantedResolutionPlaylist(url)

        renditions = stream.get('quality_renditions')
        params = {'multitrack': 'true', 'token': token}
        if isinstance(renditions, str) and renditions:
            params['filter.tracks'] = 'v4v3v2v1a1a2'
            url = f"{base}tracks-v{quote(renditions, safe='')}a2/index.ll.m3u8?{urlencode(params)}"
        else:
            params['filter'] = 'tracks:v4v3v2v1a1a2'
            url = f'{base}index.ll.m3u8?{urlencode(params)}'

        for attempt in range(1, 4):
            try:
                response = self.session.get(url, headers=self.headers, cookies=self.cookies, timeout=10)
                if response.status_code == 200:
                    break
                reason = f'HTTP {response.status_code}'
                retryable = response.status_code in (408, 500, 502, 503, 504)
            except requests.RequestException as error:
                reason = type(error).__name__
                retryable = isinstance(error, (requests.ConnectionError, requests.Timeout,
                                               requests.exceptions.ChunkedEncodingError))
            if not retryable or attempt == 3:
                self.logger.warning('CamSoda HLS master playlist failed (%s) after %d attempt(s); recording did not start',
                                    reason, attempt)
                self.download_error_reported = True
                return None
            self._sleep(1)
            if not self.running or self.quitting:
                self.download_error_reported = True
                return None
        master = self._load_m3u8(response.text)
        selected = self.getWantedResolutionPlaylist(response.url, m3u_data=master)
        if selected is None:
            return None

        variant = next((p for p in master.playlists if urljoin(response.url, p.uri) == selected), None)
        video_url = _playlist_url(url, selected, params)
        if variant and variant.stream_info.audio:
            audio = [m for m in master.media if m.type == 'AUDIO' and m.group_id == variant.stream_info.audio]
            rendition = next((m for m in audio if m.default == 'YES'), audio[0] if audio else None)
            if rendition and rendition.uri:
                audio_url = _playlist_url(url, urljoin(response.url, rendition.uri), params)
                return video_url, audio_url
        return (video_url,)

    def getStatus(self):
        if self._first_status_check:
            self._sleep(randint(3, 15))
            if self.quitting or not self.running:
                return Status.NOTRUNNING
            self._first_status_check = False

        # Keep a confirmed offline state if the next status check is inconclusive.
        unavailable = self.sc if self.sc in (Status.OFFLINE, Status.LONG_OFFLINE) else Status.UNKNOWN
        limited = unavailable if unavailable != Status.UNKNOWN else Status.RATELIMIT
        url = 'https://www.camsoda.com/api/v1/chat/react/' + self.username
        for attempt in range(2):
            try:
                with CamSoda._status_lock:
                    if monotonic() < CamSoda._status_retry_at:
                        self.ratelimit = limited != Status.LONG_OFFLINE
                        return limited
                    r = self.session.get(url, headers=self.headers, timeout=(4, 5))
                    if r.status_code == 429:
                        delay = self._status_retry_delay(r.headers.get('Retry-After'))
                        CamSoda._status_retry_at = monotonic() + delay
                        self.ratelimit = limited != Status.LONG_OFFLINE
                        self.logger.warning('CamSoda returned HTTP 429. Pausing status checks for %.0f seconds', delay)
                        return limited
                break
            except (requests.exceptions.ChunkedEncodingError, requests.ConnectionError, requests.Timeout) as error:
                if attempt == 1:
                    self.logger.warning('CamSoda status request failed after two attempts: %s', type(error).__name__)
                    return unavailable

        if r.status_code == 403:
            return Status.RATELIMIT
        if r.status_code != 200:
            self.logger.debug('CamSoda status request returned HTTP %s', r.status_code)
            return unavailable

        self.ratelimit = False
        try:
            info = r.json()
        except ValueError:
            return unavailable
        if not isinstance(info, dict):
            return unavailable

        if info.get('error') == 'No username found.':
            return Status.NOTEXIST
        stream_data = info.get('stream')
        if not isinstance(stream_data, dict):
            return unavailable
        if stream_data.get('edge_servers'):
            self.lastInfo = info
            return Status.PUBLIC
        if stream_data.get('private_servers'):
            return Status.PRIVATE
        if "token" in stream_data:
            return Status.LONG_OFFLINE if unavailable == Status.LONG_OFFLINE else Status.OFFLINE
        return unavailable

    def _status_retry_delay(self, value):
        if value:
            try:
                if value.isascii() and value.isdecimal():
                    return max(self.sleep_on_ratelimit, int(value))
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(self.sleep_on_ratelimit, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, OverflowError):
                pass
        return self.sleep_on_ratelimit
