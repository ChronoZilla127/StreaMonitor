import json
import random
import re
import ssl
import time
import urllib.parse
from threading import Lock

import requests
import websocket

from streamonitor.bot import Bot
from streamonitor.enums import Status


class MyFreeCams(Bot):
    site = 'MyFreeCams'
    siteslug = 'MFC'
    ffmpeg_allowed_extensions = 'ALL'
    ffmpeg_allowed_segment_extensions = 'ALL'
    ffmpeg_extension_picky = False
    server_config_url = 'https://www.myfreecams.com/_js/serverconfig.js'
    fcs_timeout = 15
    _server_config = None
    _server_config_time = 0
    _maintenance = False
    _maintenance_checked_at = None
    _maintenance_lock = Lock()

    def _check_maintenance(self):
        # Share the probe across channels, including while the site is down.
        cls = self.__class__
        with cls._maintenance_lock:
            if (cls._maintenance_checked_at is not None
                    and time.monotonic() - cls._maintenance_checked_at < self.sleep_on_maintenance):
                return cls._maintenance
            try:
                response = self.session.get('https://www.myfreecams.com/', timeout=(10, 20))
                text = response.text.lower()
                confirmed = response.status_code in (200, 503) and (
                    'myfreecams server maintenance' in text
                    or 'temporarily offline for maintenance' in text
                )
                if confirmed:
                    cls._maintenance = True
                elif response.status_code == 200:
                    if cls._maintenance:
                        # The old configuration may no longer be valid after maintenance.
                        cls._server_config = None
                        cls._server_config_time = 0
                        self.logger.info('MFC maintenance ended; checking channels and resuming recordings automatically')
                    cls._maintenance = False
            except requests.RequestException:
                # An unreachable page cannot confirm that maintenance has ended.
                pass
            cls._maintenance_checked_at = time.monotonic()
            return cls._maintenance

    def __init__(self, username):
        super().__init__(username)
        self.videoUrl = None
        self.model_info = None

    def getWebsiteURL(self):
        return 'https://www.myfreecams.com/#' + self.username

    def _get_server_config(self):
        cls = self.__class__
        if cls._server_config and time.time() - cls._server_config_time < 3600:
            return cls._server_config
        try:
            response = self.session.get(
                self.server_config_url,
                headers={'Referer': 'https://www.myfreecams.com/'},
                timeout=(10, 20),
            )
            response.raise_for_status()
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if not match:
                raise ValueError('serverconfig does not contain JSON')
            cls._server_config = json.loads(match.group(0))
            cls._server_config_time = time.time()
        except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
            self.logger.warning(f'Failed to fetch MFC server configuration: {e}')
        return cls._server_config

    @staticmethod
    def _frames(buffer):
        frames = []
        offset = 0
        while len(buffer) - offset >= 6 and buffer[offset:offset + 6].strip().isdigit():
            length = int(buffer[offset:offset + 6])
            end = offset + 6 + length
            if len(buffer) < end:
                break
            parts = buffer[offset + 6:end].split(' ', 5)
            if len(parts) >= 5 and parts[0].isdigit():
                frames.append((int(parts[0]), parts[5] if len(parts) > 5 else ''))
            offset = end
        return frames, buffer[offset:]

    def _lookup_model(self):
        config = self._get_server_config()
        if not config:
            return None
        servers = [
            host for host, protocol in (config.get('websocket_servers') or {}).items()
            if protocol == 'rfc6455'
        ]
        random.shuffle(servers)
        for host in servers[:2]:
            ws = None
            try:
                ws = websocket.create_connection(
                    f'wss://{host}.myfreecams.com/fcsl',
                    timeout=self.fcs_timeout,
                    origin='https://www.myfreecams.com',
                    sslopt={'context': self._websocket_ssl_context(),
                            'cert_reqs': ssl.CERT_REQUIRED, 'check_hostname': True},
                )
                ws.send('hello fcserver\n\0')
                ws.send(
                    f'1 0 0 20071025 0 '
                    f'{random.randint(10_000_000, 99_999_999)}@guest:guest\n'
                )
                ws.send(f'10 0 0 20 0 {self.username.lower()}\n')
                buffer = ''
                deadline = time.time() + self.fcs_timeout
                while time.time() < deadline:
                    incoming = ws.recv()
                    if isinstance(incoming, bytes):
                        incoming = incoming.decode('utf-8', 'replace')
                    buffer += incoming or ''
                    frames, buffer = self._frames(buffer)
                    for message_type, payload in frames:
                        if message_type != 10:
                            continue
                        decoded = urllib.parse.unquote(payload).strip()
                        if not decoded.startswith('{'):
                            return {}
                        user = json.loads(decoded)
                        if user.get('lv') != 4:
                            return {}
                        return {
                            'uid': user.get('uid'),
                            'vs': user.get('vs'),
                            'camserv': (user.get('u') or {}).get('camserv'),
                        }
            except (OSError, ValueError, json.JSONDecodeError, websocket.WebSocketException) as e:
                detail = str(e).split(' -+-+- ', 1)[0]
                self.logger.warning(f'MFC websocket lookup failed on {host}: {detail}')
            finally:
                if ws:
                    ws.close()
        return None

    @staticmethod
    def _websocket_ssl_context():
        # Include Windows/system roots as well as Requests' CA bundle. The
        # websocket client otherwise builds its own, separate trust store.
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=requests.certs.where())
        return context

    @staticmethod
    def _server_candidates(config, camserv):
        key = str(camserv) if camserv is not None else ''
        result = []
        for mapping, prefix in (
            ('h5video_servers', 'mfc_'),
            ('ngvideo_servers', 'mfc_'),
            ('wzobs_servers', 'mfc_a_'),
        ):
            name = (config.get(mapping) or {}).get(key, '')
            match = re.search(r'(\d+)$', name)
            if match:
                result.append((int(match.group(1)), prefix))
        if not result and key.isdigit():
            number = int(key)
            if number >= 3000:
                number -= 1000
            elif number >= 1690:
                number -= 700
            elif number > 500:
                number -= 500
            result.append((number, 'mfc_'))
        return list(dict.fromkeys(result))

    def _playlist_candidates(self):
        if not self.model_info or not self.model_info.get('uid'):
            return []
        room = int(self.model_info['uid']) + 100_000_000
        config = self._get_server_config() or {}
        urls = []
        for server, preferred in self._server_candidates(
            config, self.model_info.get('camserv')
        ):
            alternate = 'mfc_a_' if preferred == 'mfc_' else 'mfc_'
            for prefix in (preferred, alternate):
                urls.append(
                    'https://edgevideo.myfreecams.com/llhls/NxServer/'
                    f'{server}/ngrp:{prefix}{room}.f4v_cmaf/playlist_sfm4s.m3u8'
                )
                urls.append(
                    'https://edgevideo.myfreecams.com/hls/NxServer/'
                    f'{server}/ngrp:{prefix}{room}.f4v_mobile/playlist.m3u8'
                )
        return urls

    def getVideoUrl(self, refresh=False):
        if not refresh:
            return self.videoUrl
        if not self.model_info or self.model_info.get('vs') != 0:
            return None
        for url in self._playlist_candidates():
            try:
                response = self.session.get(
                    url,
                    headers={'Referer': 'https://www.myfreecams.com/'},
                    timeout=15,
                )
            except requests.RequestException:
                continue
            if response.status_code == 200 and '#EXTM3U' in response.text[:128]:
                selected = self.getWantedResolutionPlaylist(url, m3u_data=response.text)
                if selected:
                    return selected
        return None

    def getStatus(self):
        if self.__class__._maintenance and self._check_maintenance():
            self.model_info = None
            self.videoUrl = None
            return Status.MAINTENANCE
        info = self._lookup_model()
        if info is None:
            if self._check_maintenance():
                self.model_info = None
                self.videoUrl = None
                return Status.MAINTENANCE
            return Status.UNKNOWN
        if not info:
            self.model_info = None
            self.videoUrl = None
            return Status.NOTEXIST
        self.model_info = info
        state = info.get('vs')
        if state == 0:
            self.videoUrl = self.getVideoUrl(refresh=True)
            return Status.PUBLIC if self.videoUrl else Status.UNKNOWN
        self.videoUrl = None
        if state in (12, 13, 14):
            return Status.PRIVATE
        return Status.OFFLINE
