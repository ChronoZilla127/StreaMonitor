import urllib.parse
import warnings

import requests
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning

from streamonitor.bot import Bot
from streamonitor.enums import Status


class MyFreeCams(Bot):
    site = 'MyFreeCams'
    siteslug = 'MFC'
    status_request_attempts = 3
    status_request_timeout = (10, 20)

    def __init__(self, username):
        super().__init__(username)
        self.attrs = {}
        self.videoUrl = None

    def getWebsiteURL(self):
        return "https://www.myfreecams.com/#" + self.username

    def getVideoUrl(self, refresh=False):
        if not refresh:
            return self.videoUrl

        if 'data-cam-preview-model-id-value' not in self.attrs:
            return None

        sid = self.attrs['data-cam-preview-server-id-value']
        mid = 100000000 + int(self.attrs['data-cam-preview-model-id-value'])
        a = 'a_' if self.attrs['data-cam-preview-is-wzobs-value'] == 'true' else ''
        playlist_url = f"https://previews.myfreecams.com/hls/NxServer/{sid}/ngrp:mfc_{a}{mid}.f4v_mobile_mhp1080_previewurl/playlist.m3u8"
        try:
            r = self.session.get(playlist_url, timeout=20)
        except requests.exceptions.SSLError:
            try:
                # MFC preview CDN can serve an expired certificate; keep the bypass scoped here.
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', InsecureRequestWarning)
                    r = self.session.get(playlist_url, timeout=20, verify=False)
            except requests.exceptions.RequestException as e:
                self.logger.warning(f'Failed to fetch MFC preview playlist: {e}')
                return None
        except requests.exceptions.RequestException as e:
            self.logger.warning(f'Failed to fetch MFC preview playlist: {e}')
            return None
        if r.status_code != 200:
            return None
        return self.getWantedResolutionPlaylist(playlist_url, m3u_data=r.text)

    def getStatus(self):
        url = f'https://share.myfreecams.com/{self.username}'
        for attempt in range(1, self.status_request_attempts + 1):
            try:
                r = self.session.get(url, timeout=self.status_request_timeout)
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt == self.status_request_attempts:
                    self.logger.warning(
                        f'MFC status request failed after {attempt} attempts: {e}'
                    )
                    return Status.UNKNOWN

                delay = 2 ** (attempt - 1)
                self.logger.warning(
                    f'MFC status request failed (attempt {attempt}/'
                    f'{self.status_request_attempts}); retrying in {delay}s: {e}'
                )
                self._sleep(delay)

        if r.status_code == 404:
            # The MFC share endpoint also returns 404 for existing models while
            # they are offline, so this response cannot prove nonexistence.
            self.attrs = {}
            self.videoUrl = None
            return Status.OFFLINE
        if r.status_code != 200:
            return Status.UNKNOWN

        doc = r.content
        parsed_doc = BeautifulSoup(doc, 'html.parser')
        params = parsed_doc.find(class_='campreview')

        # Offline model pages do not necessarily include the tracking URL or a
        # model_id. Their absence does not mean that the account does not exist;
        # it only means there is no public preview to record.
        if not params:
            self.attrs = {}
            self.videoUrl = None
            return Status.OFFLINE

        startpos = doc.find(b'https://www.myfreecams.com/php/tracking.php?')
        if startpos < 0:
            self.logger.warning('MFC preview page is missing its tracking URL')
            return Status.UNKNOWN

        endpos = doc.find(b'"', startpos)
        if endpos < 0:
            self.logger.warning('MFC preview page has an invalid tracking URL')
            return Status.UNKNOWN

        url = urllib.parse.urlparse(doc[startpos:endpos])
        qs = urllib.parse.parse_qs(url.query)
        if b'model_id' not in qs:
            self.logger.warning('MFC preview page is missing model_id')
            return Status.UNKNOWN

        self.attrs = params.attrs
        self.videoUrl = self.getVideoUrl(refresh=True)
        if self.videoUrl:
            return Status.PUBLIC
        return Status.PRIVATE
