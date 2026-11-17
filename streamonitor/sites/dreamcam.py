from requests.exceptions import ConnectionError, Timeout

from streamonitor.bot import Bot
from streamonitor.downloaders.dreamcam_hls import getVideoDreamCam
from streamonitor.enums import Status


class DreamCam(Bot):
    site = 'DreamCam'
    siteslug = 'DC'

    _stream_type = 'video2D'

    def __init__(self, username):
        super().__init__(username)
        self.getVideo = getVideoDreamCam

    def getVideoUrl(self):
        for stream in self.lastInfo['streams']:
            if stream['streamType'] == self._stream_type:
                if stream['status'] == 'online':
                    return stream['url']
        return None

    def getStatus(self):
        try:
            r = self.session.get(
                'https://bss.dreamcamtrue.com/api/clients/v1/broadcasts/models/' + self.username,
                headers=self.headers,
                timeout=(4, 5),
            )
        except (ConnectionError, Timeout) as error:
            self.logger.debug('DreamCam status request failed (%s)', type(error).__name__)
            return Status.UNKNOWN
        if r.status_code != 200:
            return Status.UNKNOWN

        self.lastInfo = r.json()

        if self.lastInfo["broadcastStatus"] in ["public"]:
            return Status.PUBLIC
        if self.lastInfo["broadcastStatus"] in ["private"]:
            return Status.PRIVATE
        if self.lastInfo["broadcastStatus"] in ["away", "offline"]:
            return Status.OFFLINE
        self.logger.warn(f'Got unknown status: {self.lastInfo["broadcastStatus"]}')
        return Status.UNKNOWN
