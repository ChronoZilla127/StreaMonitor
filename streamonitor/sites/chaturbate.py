import re
from copy import deepcopy
from urllib.parse import urljoin

import m3u8
import requests
from streamonitor.bot import Bot
from streamonitor.downloaders.chaturbate import getVideoChaturbate
from streamonitor.enums import Status, Gender


class Chaturbate(Bot):
    site = 'Chaturbate'
    siteslug = 'CB'
    bulk_update = True

    _GENDER_MAP = {
        'f': Gender.FEMALE,
        'm': Gender.MALE,
        's': Gender.TRANS,
        'c': Gender.BOTH,
    }

    def __init__(self, username):
        super().__init__(username)
        self.sleep_on_offline = 30
        self.sleep_on_error = 5
        self.getVideo = getVideoChaturbate
        self.chaturbate_master = None
    
    def getWebsiteURL(self):
        return "https://www.chaturbate.com/" + self.username
    
    def getVideoUrl(self):
        self.chaturbate_master = None
        if self.bulk_update:
            self.sc = self.getStatus()
            if self.sc != Status.PUBLIC:
                return None
        url = self.lastInfo.get('url')
        if not url:
            return None
        if self.lastInfo.get('cmaf_edge'):
            url = url.replace('playlist.m3u8', 'playlist_sfm4s.m3u8')
            url = re.sub('live-.+amlst', 'live-c-fhls/amlst', url)

        try:
            response = self.session.get(
                url, headers=self.headers, cookies=self.cookies, timeout=(10, 20)
            )
            response.raise_for_status()
            base_url = response.url
            master = self._load_m3u8(response.text)
            if not master.is_variant and master.segments:
                return base_url
            selected_url = self.getWantedResolutionPlaylist(base_url, m3u_data=master)
            if selected_url is None or not master.is_variant:
                return selected_url

            selected = next(p for p in master.playlists
                            if urljoin(base_url, p.uri) == selected_url)
            group = selected.stream_info.audio
            if not group:
                return selected_url
            renditions = [m for m in master.media
                          if m.type == 'AUDIO' and m.group_id == group]
            if not renditions:
                raise ValueError('The selected audio group is missing')
            audio = next((m for m in renditions if m.default == 'YES'), None)
            if audio is None:
                audio = next((m for m in renditions if m.autoselect == 'YES'), renditions[0])
            if not audio.uri:  # Audio is carried in the video playlist.
                return selected_url

            # Keep a single program, with absolute remote child URLs. The remote
            # master may be single-use; FFmpeg must not open it a second time.
            filtered = m3u8.M3U8()
            filtered.is_variant = True
            filtered.version = master.version
            filtered.is_independent_segments = master.is_independent_segments
            video, audio = deepcopy(selected), deepcopy(audio)
            video.uri = selected_url
            audio.uri = urljoin(base_url, audio.uri)
            video.media = m3u8.model.MediaList([audio])
            video.stream_info.video = None
            video.stream_info.subtitles = None
            video.stream_info.closed_captions = None
            filtered.playlists.append(video)
            filtered.media.append(audio)
            filtered.session_keys = deepcopy(master.session_keys)
            for key in filtered.session_keys:
                if key.uri:
                    key.uri = urljoin(base_url, key.uri)
            self.chaturbate_master = filtered.dumps()
            return selected_url
        except (requests.RequestException, ValueError, StopIteration, KeyError):
            self.logger.error('Failed to prepare Chaturbate video/audio playlist')
            return None
    
    @staticmethod
    def _parseStatus(status):
        if status == "public":
            return Status.PUBLIC
        elif status in ["private", "hidden"]:
            return Status.PRIVATE
        else:
            return Status.OFFLINE

    def getStatus(self):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        data = {"room_slug": self.username, "bandwidth": "high"}

        self.lastInfo = {}
        try:
            r = self.session.post("https://chaturbate.com/get_edge_hls_url_ajax/", headers=headers, data=data, timeout=(10, 20))
            r.raise_for_status()
            self.lastInfo = r.json()
            status = self._parseStatus(self.lastInfo['room_status'])
            if status == status.PUBLIC and not self.lastInfo['url']:
                status = status.RESTRICTED
        except (requests.RequestException, ValueError, KeyError):
            self.lastInfo = {}
            status = Status.RATELIMIT

        self.ratelimit = status == Status.RATELIMIT
        return status

    @classmethod
    def getStatusBulk(cls, streamers):
        for streamer in streamers:
            if not isinstance(streamer, Chaturbate):
                continue

        session = requests.Session()
        session.headers.update(cls.headers)
        r = session.get("https://chaturbate.com/affiliates/api/onlinerooms/?format=json&wm=DkfRj", timeout=10)

        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            print('Failed to parse JSON response')
            return
        data_map = {str(model['username']).lower(): model for model in data}

        for streamer in streamers:
            model_data = data_map.get(streamer.username.lower())
            if not model_data:
                streamer.setStatus(Status.OFFLINE)
                continue
            if model_data.get('gender'):
                streamer.gender = cls._GENDER_MAP.get(model_data.get('gender'))
            if model_data.get('country'):
                streamer.country = model_data.get('country', '').upper()
            status = cls._parseStatus(model_data['current_show'])
            if status == status.PUBLIC:
                if streamer.sc in [status.PUBLIC, Status.RESTRICTED]:
                    continue
                status = streamer.getStatus()
            if status == Status.UNKNOWN:
                print(f'[{streamer.siteslug}] {streamer.username}: Bulk update got unknown status: {status}')
            streamer.setStatus(status)
