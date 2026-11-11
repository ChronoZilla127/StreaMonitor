import os

from streamonitor.bot import Bot
from streamonitor.enums import Status


class CamSoda(Bot):
    site = 'CamSoda'
    siteslug = 'CS'

    # CamSoda HLS segment URLs can lack a standard media extension.
    # Allow them through FFmpeg's HLS extension checks.
    ffmpeg_allowed_extensions = 'ALL'
    ffmpeg_allowed_segment_extensions = 'ALL'
    ffmpeg_extension_picky = False
    _edge_index = 0

    def __init__(self, username):
        super().__init__(username)
        self.custom_hls = os.getenv('CAMSODA_CUSTOM_HLS') == '1'
        if self.custom_hls:
            from streamonitor.downloaders.camsoda import getVideoCamSoda

            self.camsoda_saved_segments = {}
            self.getVideo = getVideoCamSoda

    def getWebsiteURL(self):
        return "https://www.camsoda.com/" + self.username

    def getVideoUrl(self):
        track_params = "filter=tracks:v4v3v2v1a1a2&multitrack=true"
        stream = self.lastInfo['stream']
        servers = stream['edge_servers']
        if not servers:
            return None
        self._edge_index %= len(servers)
        server = servers[self._edge_index]
        stream_name = stream['stream_name']
        token = stream['token']
        url = f"https://{server}/{stream_name}_v1/index.ll.m3u8?{track_params}&token={token}"
        if self.custom_hls:
            return url
        selected = self.getWantedResolutionPlaylist(url)
        if selected is None:
            self._edge_index += 1
        return selected

    def handle_ffmpeg_error(self, details):
        if 'Error opening input: Server returned 5XX' not in details:
            return False

        self._edge_index += 1
        self.logger.warning('CamSoda returned HTTP 5xx while opening the playlist')
        return True

    def getStatus(self):
        self.lastInfo = {}
        url = 'https://www.camsoda.com/api/v1/chat/react/' + self.username
        response = self.session.get(url, headers=self.headers, timeout=10)
        if response.status_code == 403:
            return Status.RATELIMIT
        if response.status_code != 200:
            return Status.UNKNOWN

        try:
            info = response.json()
        except ValueError:
            return Status.UNKNOWN
        if not isinstance(info, dict):
            return Status.UNKNOWN
        if info.get('error') == 'No username found.':
            return Status.NOTEXIST
        stream_data = info.get('stream')
        if not isinstance(stream_data, dict):
            return Status.UNKNOWN

        self.lastInfo = info
        if stream_data.get('edge_servers'):
            return Status.PUBLIC
        if stream_data.get('private_servers'):
            return Status.PRIVATE
        if 'token' in stream_data:
            return Status.OFFLINE
        return Status.UNKNOWN
