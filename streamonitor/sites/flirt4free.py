import json
import re
from contextlib import closing
from urllib.parse import quote, urljoin

import requests
from websocket import WebSocketException, create_connection

from parameters import WANTED_RESOLUTION, WANTED_RESOLUTION_PREFERENCE
from streamonitor.bot import RoomIdBot
from streamonitor.downloaders.ffmpeg_flirt4free import getVideoFfmpegFlirt4Free
from streamonitor.enums import Status


# Site of Hungarian group AdultPerformerNetwork
class Flirt4Free(RoomIdBot):
    site = 'Flirt4Free'
    siteslug = 'F4F'
    models = {}
    ROOM_STATE = '8011'
    VIDEO_KEY_REFRESH = '8040'
    OPEN = 'O'
    PRIVATE = 'P'
    OFFLINE = 'F'
    CLOSED = 'N'
    BREAK = 'B'

    def __init__(self, username, room_id=None):
        super().__init__(username, room_id)
        self.getVideo = getVideoFfmpegFlirt4Free

    def _unknownStatus(self, reason):
        return Status.UNKNOWN

    def getWebsiteURL(self):
        return "https://www.flirt4free.com/?model=" + self.username

    def getRoomIdFromUsername(self, username):
        if username not in Flirt4Free.models:
            r = self.session.get(f'https://www.flirt4free.com/?model={username}')

            start = b'window.__homePageData__ = '

            if r.content.find(start) == -1:
                return Status.OFFLINE

            j = r.content[r.content.find(start) + len(start):]
            j = j[j.find(b'['):j.find(b'],\n') + 1]
            j = j[j.find(b'['):j.rfind(b',')] + b']'

            try:
                m = json.loads(j)
            except Exception as e:
                self.log(f'Failed to parse JSON: {e}')
                m = []

            Flirt4Free.models = {
                v['model_seo_name']: v
                for v in m
            }

        if username in Flirt4Free.models:
            return Flirt4Free.models[username]['model_id']

        r = self.session.get(
            f'https://www.flirt4free.com/models/bios/{username}/about.php'
        )
        if r.status_code == 200:
            match = re.search(r"listsModelId\s*=\s*['\"](\d+)['\"]", r.text)
            if match:
                return match.group(1)
        return None

    def getVideoUrl(self):
        video_url = self.lastInfo.get('video_url')
        if not video_url:
            video_url = self._getWantedLivePlaylistFromStreamUrls(self.lastInfo.get('stream_urls', {}))

        if not video_url or not self._playlistHasMediaSegments(video_url):
            self.logger.warning('F4F selected playlist has no media segments')
            return None
        return video_url

    def _getJson(self, url, params=None):
        r = self.session.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def _getRoomInfo(self):
        return self._getJson(
            'https://www.flirt4free.com/ws/rooms/chat-room-interface.php',
            {
                'a': 'login_room',
                'model_id': self.room_id,
                'browser': 'chrome',
                'screen_resolution': '1920x1080',
            },
        )

    @staticmethod
    def _getChatWebsocketUrl(room, token_enc, model_id):
        room_host = room.get('host') or room.get('server_public_name')
        port = room.get('port_to_be') or room.get('port') or room.get('browser_port')
        if not room_host or not token_enc or not port:
            return None

        chat_host = room_host.split('.', 1)[0]
        return (
            f'wss://www.flirt4free.com/{chat_host}/chat?'
            f'token={quote(str(token_enc), safe="")}&port_to_be={port}&model_id={model_id}'
        )

    @staticmethod
    def _recvJson(conn):
        return json.loads(conn.recv())

    def _isPublicRoomState(self, room_state):
        return room_state.get('room_state') == self.OPEN and room_state.get('state') == self.OPEN

    @staticmethod
    def _getStreamKey(room_state):
        for key in ('video_key', 'open_room_key', 'stream_key'):
            value = room_state.get(key)
            if value and value != 'nil':
                return value
        return None

    def _refreshVideoKey(self, conn, room_state):
        conn.send(json.dumps({'command': self.VIDEO_KEY_REFRESH, 'sequence': 2, 'type': 0}))

        for _ in range(8):
            message = self._recvJson(conn)
            payload = message.get('data') or {}

            if message.get('command') == self.VIDEO_KEY_REFRESH:
                video_key = payload.get('video_key')
                if video_key:
                    room_state['video_key'] = video_key
                    return

            if message.get('command') == self.ROOM_STATE:
                room_state.update(payload)
                if self._getStreamKey(room_state):
                    return

    def _getRoomState(self, room_info):
        room = room_info.get('config', {}).get('room', {})
        url = self._getChatWebsocketUrl(room, room_info.get('token_enc'), self.room_id)
        if not url:
            return None

        try:
            with closing(create_connection(url, timeout=10, header=['Origin: https://www.flirt4free.com'])) as conn:
                room_state = None
                for _ in range(8):
                    message = self._recvJson(conn)
                    if message.get('command') == self.ROOM_STATE:
                        room_state = message.get('data') or {}
                        break

                if not room_state:
                    return None

                conn.send(json.dumps({'command': self.ROOM_STATE, 'sequence': 1, 'type': 1, 'data': 'true'}))
                if self._isPublicRoomState(room_state) and not self._getStreamKey(room_state):
                    self._refreshVideoKey(conn, room_state)
                return room_state
        except (OSError, ValueError, WebSocketException) as e:
            self.debug(f'Failed to read F4F room websocket: {e}')
            return None

    def _getStreamUrls(self, room_state):
        params = {'model_id': self.room_id}
        stream_host = room_state.get('stream_host')
        stream_key = self._getStreamKey(room_state)

        if stream_host:
            params['video_host'] = stream_host
        if stream_key:
            params['stream_key'] = stream_key

        return self._getJson('https://www.flirt4free.com/ws/chat/get-stream-urls.php', params)

    @staticmethod
    def _normalizePlaylistUrl(url):
        if not url:
            return None
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('http://') or url.startswith('https://'):
            return url
        return 'https://www.flirt4free.com/' + url.lstrip('/')

    def _getPlaylistUrl(self, stream_urls):
        urls = self._getPlaylistUrls(stream_urls)
        return urls[0] if urls else None

    def _getPlaylistUrls(self, stream_urls):
        data = stream_urls.get('data') or {}
        urls = []
        for stream_type in ('hls', 'llhls'):
            streams = data.get(stream_type) or []
            for stream in streams:
                url = self._normalizePlaylistUrl(stream.get('url'))
                if url and url not in urls:
                    urls.append(url)
        return urls

    def _hasInvalidStreamKey(self, stream_urls):
        playlist_urls = self._getPlaylistUrls(stream_urls)
        return not playlist_urls or all('key=nil' in url.lower() for url in playlist_urls)

    @staticmethod
    def _sourceHasVideo(source):
        width, height = source['resolution']
        return width > 0 and height > 0

    @staticmethod
    def _setResolutionDiff(source):
        width, height = source['resolution']
        if width < height:
            source['resolution_diff'] = width - WANTED_RESOLUTION
        else:
            source['resolution_diff'] = height - WANTED_RESOLUTION

    def _getWantedResolutionCandidates(self, sources):
        for source in sources:
            self._setResolutionDiff(source)

        sources.sort(key=lambda a: abs(a['resolution_diff']))

        if WANTED_RESOLUTION_PREFERENCE == 'exact':
            return [source for source in sources if source['resolution_diff'] == 0]
        if WANTED_RESOLUTION_PREFERENCE == 'closest' or len(sources) == 1:
            return sources
        if WANTED_RESOLUTION_PREFERENCE == 'exact_or_least_higher':
            return [source for source in sources if source['resolution_diff'] >= 0]
        if WANTED_RESOLUTION_PREFERENCE == 'exact_or_highest_lower':
            return [source for source in sources if source['resolution_diff'] <= 0]

        self.logger.error('Invalid value for WANTED_RESOLUTION_PREFERENCE')
        return []

    def _playlistHasMediaSegments(self, url):
        try:
            result = self.session.get(url, headers=self.headers, cookies=self.cookies, timeout=20)
        except requests.exceptions.RequestException as e:
            self.debug(f'Failed to fetch F4F media playlist: {e}')
            return False

        if result.status_code != 200:
            return False

        lines = [line.strip() for line in result.text.splitlines()]
        for index, line in enumerate(lines):
            line = line.strip()
            if not line.startswith('#EXTINF'):
                continue

            segment = next((value for value in lines[index + 1:] if value), '')
            if segment and not segment.startswith('#'):
                return True
        return False

    def _getWantedLivePlaylist(self, playlist_url, allow_unknown_video=False):
        sources = self.getPlaylistVariants(playlist_url)
        if sources is None:
            return None
        if len(sources) == 0:
            return None

        video_sources = [source for source in sources if self._sourceHasVideo(source)]
        if not video_sources:
            if allow_unknown_video and len(sources) == 1:
                source_url = urljoin(playlist_url, sources[0]['url'])
                if self._playlistHasMediaSegments(source_url):
                    self.logger.warning(
                        'F4F playlist has no advertised video variant; ffmpeg will verify the stream'
                    )
                    return source_url
            self.debug('F4F playlist has no video variants')
            return None

        candidates = self._getWantedResolutionCandidates(video_sources)
        if not candidates:
            self.logger.error("Couldn't select a resolution")
            return None

        for source in candidates:
            source_url = urljoin(playlist_url, source['url'])
            if not self._playlistHasMediaSegments(source_url):
                continue

            if source['resolution'][1] != 0:
                frame_rate = ''
                if source['frame_rate'] is not None and source['frame_rate'] != 0:
                    frame_rate = f" {source['frame_rate']}fps"
                self.logger.info(f"Selected {source['resolution'][0]}x{source['resolution'][1]}{frame_rate} resolution")
            return source_url

        return None

    def _getWantedLivePlaylistFromStreamUrls(self, stream_urls):
        playlist_urls = self._getPlaylistUrls(stream_urls)
        for playlist_url in playlist_urls:
            video_url = self._getWantedLivePlaylist(playlist_url)
            if video_url:
                return video_url

        for playlist_url in playlist_urls:
            video_url = self._getWantedLivePlaylist(playlist_url, allow_unknown_video=True)
            if video_url:
                return video_url
        return None

    def getStatus(self):
        if self.room_id is None:
            return Status.NOTEXIST

        try:
            room_info = self._getRoomInfo()
        except (requests.exceptions.RequestException, ValueError) as e:
            return self._unknownStatus(f'failed to get room info: {e}')

        self.lastInfo = {'room': room_info}
        if room_info.get('message') == 'Invalid Model':
            return Status.OFFLINE
        if 'config' not in room_info:
            return Status.OFFLINE

        status = room_info['config'].get('room', {}).get('status')
        if status == self.PRIVATE:
            return Status.PRIVATE

        room_state = self._getRoomState(room_info)
        if not room_state:
            return Status.OFFLINE

        self.lastInfo['room_state'] = room_state
        if room_state.get('room_state') == self.PRIVATE or room_state.get('state') == self.PRIVATE:
            return Status.PRIVATE
        if not self._isPublicRoomState(room_state):
            return Status.OFFLINE
        if not self._getStreamKey(room_state):
            return Status.OFFLINE

        try:
            stream_urls = self._getStreamUrls(room_state)
        except (requests.exceptions.RequestException, ValueError) as e:
            return self._unknownStatus(f'failed to get stream URLs: {e}')

        self.lastInfo['stream_urls'] = stream_urls
        if stream_urls.get('code') == 44:
            return Status.NOTEXIST
        if stream_urls.get('code') != 0:
            return Status.OFFLINE
        if self._hasInvalidStreamKey(stream_urls):
            return Status.OFFLINE

        video_url = self._getWantedLivePlaylistFromStreamUrls(stream_urls)
        if not video_url:
            return Status.OFFLINE

        self.lastInfo['video_url'] = video_url
        return Status.PUBLIC
