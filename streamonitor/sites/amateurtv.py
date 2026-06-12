from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from streamonitor.bot import Bot
from streamonitor.enums import Status


class AmateurTV(Bot):
    site = 'AmateurTV'
    siteslug = 'ATV'

    @staticmethod
    def _append_variant(url, height):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query['variant'] = str(height)
        return urlunsplit(parts._replace(query=urlencode(query)))

    def getWebsiteURL(self):
        return f'https://www.amateur.tv/model/{self.username}'

    def getPlaylistVariants(self, url):
        sources = []

        video_technologies = self.lastInfo.get('videoTechnologies') or {}
        hls_url = video_technologies.get('fmp4-hls')
        if not hls_url:
            self.logger.error('ATV response did not include an fMP4 HLS playlist URL')
            return None

        for resolution in self.lastInfo.get('qualities') or []:
            try:
                width, height = resolution.split('x', maxsplit=1)
                width = int(width)
                height = int(height)
            except (TypeError, ValueError):
                self.logger.warning(f'Ignoring invalid ATV resolution: {resolution}')
                continue

            sources.append({
                'url': self._append_variant(hls_url, height),
                'resolution': (width, height),
                'frame_rate': None,
                'bandwidth': None
            })

        if len(sources) == 0:
            sources.append({
                'url': hls_url,
                'resolution': (0, 0),
                'frame_rate': None,
                'bandwidth': None
            })
        return sources

    def getVideoUrl(self):
        return self.getWantedResolutionPlaylist(None)

    def getStatus(self):
        headers = self.headers | {
            'Content-Type': 'application/json',
            'Referer': 'https://amateur.tv/'
        }
        r = self.session.get(f'https://www.amateur.tv/v3/readmodel/show/{self.username}/en', headers=headers)

        if r.status_code != 200:
            return Status.UNKNOWN

        self.lastInfo = r.json()

        if self.lastInfo.get('message') == 'NOT_FOUND':
            return Status.NOTEXIST
        if self.lastInfo.get('result') == 'KO':
            return Status.UNKNOWN
        if self.lastInfo.get('status') == 'online':
            if self.lastInfo.get('privateChatStatus') is None:
                return Status.PUBLIC
            else:
                return Status.PRIVATE
        if self.lastInfo.get('status') == 'offline':
            return Status.OFFLINE
        return Status.UNKNOWN
