from functools import partial
from urllib.parse import quote

from streamonitor.downloaders.fmp4s_wss import getVideoWSSVR
from streamonitor.enums import Status
from streamonitor.sites.dreamcam_vr import DreamCamVR


class CamSodaVR(DreamCamVR):
    site = 'CamSoda VR'
    siteslug = 'CSVR'
    aliases = ['camsodavr']
    origin = 'https://vr.camsoda.com'

    def __init__(self, username):
        super().__init__(username)
        self.status_lookup_error = None
        self.getVideo = partial(getVideoWSSVR, origin=self.origin, protocol_version='3.0.0')

    def getWebsiteURL(self):
        return f'{self.origin}/{quote(self.username, safe="")}'

    def status(self):
        if self.sc == Status.UNKNOWN and self.status_lookup_error:
            return f'CamSoda VR status unavailable ({self.status_lookup_error})'
        return super().status()

    def getStatus(self):
        self.lastInfo = {}
        self.status_lookup_error = None
        self.ratelimit = False
        response = self.session.get(
            f'{self.origin}/api/v1/chat/react/{quote(self.username, safe="")}',
            headers={'Accept': 'application/json'},
            timeout=15,
        )
        if response.status_code in (403, 429):
            self.ratelimit = True
            if self.sc == Status.PRIVATE:
                return Status.PRIVATE
            if response.status_code == 429:
                return Status.RATELIMIT
        if response.status_code == 404:
            return Status.NOTEXIST
        if response.status_code != 200:
            self.status_lookup_error = f'HTTP {response.status_code}'
            return Status.UNKNOWN

        self.lastInfo = response.json()
        if self.lastInfo.get('error') == 'No username found.':
            return Status.NOTEXIST
        if self.lastInfo.get('mode') in ('private', 'exclusive'):
            return Status.PRIVATE
        if 'stream' not in self.lastInfo:
            return Status.UNKNOWN
        return Status.PUBLIC if self.getVideoUrl() else Status.OFFLINE

    def getVideoUrl(self):
        if self.lastInfo.get('mode') != 'public':
            return None
        stream = self.lastInfo.get('stream') or {}
        vr = stream.get('vr') or {}
        url = vr.get('public_url')
        if vr.get('status') == 'online' and isinstance(url, str) and url.startswith(('fmp4s://', 'wss://')):
            return url
        return None
