from requests.exceptions import SSLError

from streamonitor.bot import Bot
from streamonitor.enums import Status


class StreaMate(Bot):
    site = 'StreaMate'
    siteslug = 'SM'
    aliases = ['pornhublive']
    ffmpeg_referer = 'https://streamate.com/'
    authorization_retry_delay = 5

    def handle_ffmpeg_error(self, details):
        if 'Server returned 401 Unauthorized' not in details:
            return False
        # The next Bot.run iteration fetches a new manifest via getStatus().
        self.lastInfo = {}
        self.sleep_on_error = self.authorization_retry_delay
        self.defer_download_attempt(self.authorization_retry_delay)
        self.logger.warning(
            'SM video authorization failed (HTTP 401); discarding the previous '
            f'URL and retrying with a fresh manifest after {self.authorization_retry_delay}s'
        )
        return True

    def getWebsiteURL(self):
        return "https://streamate.com/cam/" + self.username

    def getPlaylistVariants(self, url, m3u_data=None):
        sources = []
        # formats: mp4-rtmp, mp4-hls, mp4-ws
        for source in self.lastInfo['formats']['mp4-hls']['encodings']:
            sources.append({
                'url': source['location'],
                'resolution': (source['videoWidth'], source['videoHeight']),
                'frame_rate': None,
                'bandwidth': None
            })
        return sources

    def getVideoUrl(self):
        return self.getWantedResolutionPlaylist(None)

    def getStatus(self):
        # Restore the normal error delay after the authorization retry wait.
        self.sleep_on_error = type(self).sleep_on_error
        headers = self.headers | {
            'Content-Type': 'application/json',
            'Referer': self.ffmpeg_referer
        }
        self.lastInfo = {}
        url = 'https://manifest-server.naiadsystems.com/live/s:' + self.username + '.json?last=load&format=mp4-hls'
        # Some backends can still serve an expired certificate during renewal.
        # A failed TLS handshake is discarded; retry with verification enabled.
        for attempt in range(4):
            try:
                r = self.session.get(url, headers=headers, timeout=(10, 20), verify=True)
                break
            except SSLError as error:
                if 'certificate has expired' not in str(error).lower() or attempt == 3:
                    raise
                self.logger.warning(
                    'SM manifest certificate expired; retrying with HTTPS '
                    f'verification enabled ({attempt + 2}/4)'
                )

        if r.status_code == 200:
            self.lastInfo = r.json()
        return Status(r.status_code)
