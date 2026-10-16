import functools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import monotonic
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest
from unittest.mock import Mock, patch

import m3u8
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from streamonitor.bot import Bot
from streamonitor.downloaders import chaturbate, ffmpeg
from streamonitor.enums import Status
from streamonitor.sites.chaturbate import Chaturbate


MASTER = '''#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="main",DEFAULT=YES,AUTOSELECT=YES,URI="audio/index.m3u8?session=abc"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="other",NAME="other",URI="unwanted/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=320x180,AUDIO="sound"
low/index.m3u8?session=abc
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360,AUDIO="sound"
high/index.m3u8?session=abc
'''


class ChaturbateAudioTests(unittest.TestCase):
    def make_bot(self, document=MASTER):
        bot = Chaturbate.__new__(Chaturbate)
        bot.bulk_update = False
        bot.lastInfo = {'url': 'https://origin.test/master.m3u8'}
        bot.session = Mock()
        bot.session.get.return_value = Mock(
            text=document, url='https://cdn.test/session/master.m3u8'
        )
        bot.cookies = None
        bot.logger = Mock()
        return bot

    def select(self, bot, resolution=360, preference='exact'):
        with patch('streamonitor.bot.WANTED_RESOLUTION', resolution), patch(
            'streamonitor.bot.WANTED_RESOLUTION_PREFERENCE', preference
        ):
            return bot.getVideoUrl()

    def test_selected_master_keeps_only_selected_video_and_audio(self):
        bot = self.make_bot()
        self.assertEqual(self.select(bot), 'https://cdn.test/session/high/index.m3u8?session=abc')
        filtered = m3u8.loads(bot.chaturbate_master)
        self.assertEqual(len(filtered.playlists), 1)
        self.assertEqual(filtered.playlists[0].stream_info.resolution, (640, 360))
        self.assertEqual(filtered.playlists[0].stream_info.audio, 'sound')
        self.assertEqual(len(filtered.media), 1)
        self.assertEqual(filtered.media[0].uri, 'https://cdn.test/session/audio/index.m3u8?session=abc')
        bot.session.get.assert_called_once()

    def test_resolution_preferences_are_preserved(self):
        for preference, resolution, expected in [
            ('exact', 240, None), ('closest', 240, 'low'),
            ('exact_or_least_higher', 240, 'high'),
            ('exact_or_highest_lower', 240, 'low'),
        ]:
            with self.subTest(preference=preference):
                bot = self.make_bot()
                selected = self.select(bot, resolution, preference)
                if expected is None:
                    self.assertIsNone(selected)
                    self.assertIsNone(bot.chaturbate_master)
                else:
                    self.assertIn('/' + expected + '/', selected)

    def test_multiplexed_variant_uses_existing_downloader(self):
        bot = self.make_bot(MASTER.replace(',AUDIO="sound"', ''))
        self.assertIn('/high/', self.select(bot))
        self.assertIsNone(bot.chaturbate_master)

    def test_missing_audio_group_fails_instead_of_silently_losing_audio(self):
        bot = self.make_bot(MASTER.replace('AUDIO="sound"', 'AUDIO="missing"'))
        self.assertIsNone(self.select(bot))
        self.assertIsNone(bot.chaturbate_master)

    def test_media_playlist_stays_usable(self):
        bot = self.make_bot('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nseg.ts\n')
        self.assertEqual(self.select(bot), 'https://cdn.test/session/master.m3u8')
        self.assertIsNone(bot.chaturbate_master)

    def test_private_status_clears_previous_master(self):
        bot = self.make_bot()
        bot.bulk_update = True
        bot.chaturbate_master = 'stale'
        bot.getStatus = Mock(return_value=Status.PRIVATE)
        self.assertIsNone(bot.getVideoUrl())
        self.assertIsNone(bot.chaturbate_master)
        bot.session.get.assert_not_called()

    def test_failed_master_request_cannot_reuse_previous_master(self):
        bot = self.make_bot()
        bot.chaturbate_master = 'stale'
        bot.session.get.side_effect = requests.Timeout()
        self.assertIsNone(self.select(bot))
        self.assertIsNone(bot.chaturbate_master)

    def test_status_failure_discards_old_url(self):
        bot = self.make_bot()
        bot.username = 'test'
        bot.session.post.side_effect = requests.Timeout()
        self.assertEqual(bot.getStatus(), Status.RATELIMIT)
        self.assertEqual(bot.lastInfo, {})

    def test_local_server_is_closed_even_if_downloader_raises(self):
        bot = self.make_bot()
        self.select(bot)
        address = []

        def fail(bot, url, filename):
            address.append(url)
            self.assertEqual(requests.get(url, timeout=2).status_code, 200)
            self.assertTrue(bot.ffmpeg_disable_readrate)
            self.assertTrue(bot.ffmpeg_copy_timestamps)
            self.assertEqual(bot.ffmpeg_output_maps, ('0:v:0', '0:a:0'))
            raise RuntimeError('test failure')

        with patch.object(chaturbate, 'getVideoFfmpeg', side_effect=fail):
            with self.assertRaises(RuntimeError):
                chaturbate.getVideoChaturbate(bot, 'unused', 'unused.mp4')
        with self.assertRaises(requests.ConnectionError):
            requests.get(address[0], timeout=2)
        self.assertIsNone(bot.chaturbate_master)
        self.assertIsNone(bot.ffmpeg_output_maps)
        self.assertFalse(bot.ffmpeg_disable_readrate)
        self.assertFalse(bot.ffmpeg_copy_timestamps)
        self.assertIsNone(bot.ffmpeg_recording_error)

    def test_lost_segments_close_recording_and_report_failure(self):
        for still_running in (True, False):
            with self.subTest(still_running=still_running), tempfile.TemporaryDirectory() as directory:
                bot = self.make_bot()
                self.select(bot)
                process = Mock(returncode=0, poll=Mock(return_value=None if still_running else 0))

                def launch(**kwargs):
                    kwargs['stderr'].write(b'[hls] skipping 21 segments ahead, expired from playlists\n')
                    kwargs['stderr'].flush()
                    return process

                with patch.object(ffmpeg.subprocess, 'Popen', side_effect=launch):
                    self.assertFalse(chaturbate.getVideoChaturbate(bot, 'unused', str(Path(directory) / 'show.mp4')))
                if still_running:
                    process.communicate.assert_called_once_with(b'q', timeout=5)
                bot.logger.warning.assert_called_once()
                self.assertTrue((Path(directory) / 'show.mp4.stderr.log').exists())

    def test_generic_downloader_options_are_opt_in(self):
        from types import SimpleNamespace
        for cb in (False, True):
            bot = SimpleNamespace(headers=Bot.headers, cookies=None, logger=Mock())
            if cb:
                bot.ffmpeg_disable_readrate = True
                bot.ffmpeg_copy_timestamps = True
                bot.ffmpeg_output_maps = ('0:v:0', '0:a:0')
            with tempfile.TemporaryDirectory() as directory, patch.object(
                ffmpeg, 'FFMPEG_READRATE', 1.3
            ), patch.object(ffmpeg.subprocess, 'Popen', return_value=Mock(returncode=0, poll=Mock(return_value=0))) as launch:
                self.assertTrue(ffmpeg.getVideoFfmpeg(bot, 'https://test/live.m3u8', str(Path(directory) / 'show.mp4')))
                args = launch.call_args.kwargs['args']
                self.assertEqual('-readrate' in args, not cb)
                self.assertEqual('-map' in args, cb)
                self.assertEqual('-copyts' in args, cb)
                self.assertEqual('-start_at_zero' in args, cb)


FFMPEG = os.environ.get('TEST_FFMPEG') or shutil.which('ffmpeg')
FFPROBE = os.environ.get('TEST_FFPROBE') or shutil.which('ffprobe')


@unittest.skipUnless(FFMPEG and FFPROBE, 'Set TEST_FFMPEG and TEST_FFPROBE for real HLS integration')
class RealHlsTests(unittest.TestCase):
    def test_records_only_selected_quality_with_audible_continuous_audio(self):
        self.record_fixture(live=False)

    def test_sliding_live_playlists_preserve_audio_and_continuity(self):
        self.record_fixture(live=True)

    def test_different_playlist_starts_keep_original_relative_clock(self):
        self.record_fixture(live=False, late_video=True)

    def record_fixture(self, live, late_video=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def run(*args, cwd=None):
                return subprocess.run(args, check=True, capture_output=True, timeout=30, cwd=cwd)

            for name, size in [('low', '320x180'), ('high', '640x360')]:
                (root / name).mkdir()
                run(FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=25',
                    '-t', '6', '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25',
                    '-output_ts_offset', '1000',
                    '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod',
                    '-hls_segment_type', 'fmp4', str(root / name / 'index.m3u8'), cwd=root / name)
            (root / 'audio').mkdir()
            run(FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                '-t', '6', '-c:a', 'aac', '-f', 'hls', '-hls_time', '1',
                '-output_ts_offset', '1000',
                '-hls_playlist_type', 'vod', '-hls_segment_type', 'fmp4',
                str(root / 'audio' / 'index.m3u8'), cwd=root / 'audio')
            if late_video:
                source = root / 'high' / 'index.m3u8'
                playlist = m3u8.loads(source.read_text())
                playlist.segments = m3u8.model.SegmentList(playlist.segments[1:])
                playlist.media_sequence = 1
                source.write_text(playlist.dumps())
            (root / 'master.m3u8').write_text(MASTER, encoding='utf-8')
            fetched = []
            started = monotonic()

            class Handler(SimpleHTTPRequestHandler):
                def do_GET(self):
                    fetched.append(self.path)
                    if self.path == '/master.m3u8' and fetched.count(self.path) > 1:
                        self.send_error(403)
                        return
                    child = self.path.split('?', 1)[0].lstrip('/')
                    if live and child in ('audio/index.m3u8', 'high/index.m3u8'):
                        playlist = m3u8.loads((root / child).read_text())
                        available = min(len(playlist.segments), 3 + int(monotonic() - started))
                        first = max(0, available - 4)
                        total = len(playlist.segments)
                        playlist.segments = m3u8.model.SegmentList(playlist.segments[first:available])
                        playlist.media_sequence = first
                        playlist.playlist_type = None
                        playlist.is_endlist = available == total
                        payload = playlist.dumps().encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                        self.send_header('Content-Length', str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    return super().do_GET()

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=directory))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                bot = Chaturbate.__new__(Chaturbate)
                bot.bulk_update = False
                bot.session = requests.Session()
                bot.lastInfo = {'url': f'http://127.0.0.1:{server.server_port}/master.m3u8'}
                bot.cookies = None
                bot.logger = Mock()
                with patch('streamonitor.bot.WANTED_RESOLUTION', 360), patch(
                    'streamonitor.bot.WANTED_RESOLUTION_PREFERENCE', 'exact'
                ):
                    url = bot.getVideoUrl()
                self.assertIsNotNone(url)
                output = root / 'result.mp4'
                with patch.object(ffmpeg, 'FFMPEG_PATH', FFMPEG), patch.object(
                    ffmpeg, 'SEGMENT_TIME', None
                ), patch.object(ffmpeg, 'CONTAINER', 'mp4'):
                    self.assertTrue(chaturbate.getVideoChaturbate(bot, url, str(output)), bot.logger.mock_calls)
                data = json.loads(run(FFPROBE, '-v', 'error', '-show_streams', '-show_packets',
                                      '-of', 'json', str(output)).stdout)
                video = next(s for s in data['streams'] if s['codec_type'] == 'video')
                audio = next(s for s in data['streams'] if s['codec_type'] == 'audio')
                self.assertEqual((video['width'], video['height']), (640, 360))
                self.assertEqual(audio['codec_name'], 'aac')
                self.assertFalse(any('/low/' in p or '/unwanted/' in p for p in fetched))
                self.assertEqual(fetched.count('/master.m3u8'), 1)
                starts, ends = [], []
                for stream in (video, audio):
                    packets = [p for p in data['packets'] if p['stream_index'] == stream['index']]
                    times = [float(p['dts_time']) for p in packets]
                    self.assertGreater(len(times), 100)
                    self.assertTrue(all(0 < b - a < .1 for a, b in zip(times, times[1:])))
                    starts.append(float(packets[0]['pts_time']))
                    ends.append(float(packets[-1]['pts_time']) + float(packets[-1]['duration_time']))
                expected_start_difference = 1 if late_video else 0
                self.assertLess(abs(starts[0] - starts[1] - expected_start_difference), .1)
                self.assertLess(abs(ends[0] - ends[1]), .1)
                self.assertGreater(min(ends), 5.8)
                decoded = run(FFMPEG, '-v', 'error', '-i', str(output), '-map', '0:a:0',
                              '-f', 's16le', '-ac', '1', 'pipe:1').stdout
                import array
                samples = array.array('h', decoded)
                self.assertGreater(max(abs(s) for s in samples), 1000)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
