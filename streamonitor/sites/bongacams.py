import requests

from streamonitor.bot import Bot
from streamonitor.enums import Status


class BongaCams(Bot):
    site = 'BongaCams'
    siteslug = 'BC'
    playlist_attempts = 3
    request_timeout = (10, 20)

    def getWebsiteURL(self):
        return 'https://bongacams.com/' + self.username

    def getPlaylistUrl(self):
        server = self.lastInfo['localData']['videoServerUrl']
        return f'https:{server}/hls/stream_{self.username}/playlist.m3u8'

    def _room_headers(self):
        return self.headers | {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://de.bongacams.net/' + self.username,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
        }

    def _refresh_room_data(self):
        try:
            response = self.session.post(
                'https://de.bongacams.net/tools/amf.php',
                data={'method': 'getRoomData', 'args[]': [self.username, 'false']},
                headers=self._room_headers(),
                timeout=self.request_timeout,
            )
            if response.status_code != 200:
                return None
            room_data = response.json()
        except (requests.RequestException, ValueError) as e:
            self.logger.warning(f'BongaCams room request failed: {e}')
            return None

        self.lastInfo = room_data
        if room_data.get('status') == 'error':
            return Status.NOTEXIST
        performer = room_data.get('performerData') or {}
        canonical_username = performer.get('username')
        if canonical_username and self.username != canonical_username:
            self.username = canonical_username
            self.logger = self.getLogger()
        if performer.get('showType') in ('private', 'group'):
            return Status.PRIVATE
        if not (room_data.get('localData') or {}).get('videoServerUrl'):
            return Status.OFFLINE
        return Status.PUBLIC

    def _fetch_playlist(self, url):
        try:
            response = self.session.get(
                url, headers=self.headers, timeout=self.request_timeout
            )
        except requests.RequestException as e:
            self.logger.warning(f'BongaCams playlist request failed: {e}')
            return None
        if response.status_code != 200 or '#EXTM3U' not in response.text[:128]:
            return None
        return response

    def getVideoUrl(self):
        for attempt in range(1, self.playlist_attempts + 1):
            if attempt > 1:
                if self._refresh_room_data() != Status.PUBLIC:
                    return None
                self._sleep(1)
            master_url = self.getPlaylistUrl()
            master = self._fetch_playlist(master_url)
            if master is None:
                continue
            selected_url = self.getWantedResolutionPlaylist(
                master_url, m3u_data=master.text
            )
            if selected_url and self._fetch_playlist(selected_url) is not None:
                return selected_url
            self.logger.warning(
                f'BongaCams selected playlist expired '
                f'(attempt {attempt}/{self.playlist_attempts}); refreshing room data'
            )
        self.logger.warning('BongaCams has no playable playlist after retries')
        return None

    def getStatus(self):
        status = self._refresh_room_data()
        if status != Status.PUBLIC:
            return status or Status.UNKNOWN
        return (
            Status.PUBLIC
            if self._fetch_playlist(self.getPlaylistUrl())
            else Status.OFFLINE
        )
