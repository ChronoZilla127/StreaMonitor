import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests.exceptions import SSLError, Timeout

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from streamonitor.downloaders import ffmpeg
from streamonitor.enums import Status
from streamonitor.sites.streamate import StreaMate


class StreamateFfmpegTests(unittest.TestCase):
    def make_bot(self):
        bot = StreaMate.__new__(StreaMate)
        bot.username = 'test'
        bot.logger = Mock()
        bot.cookies = None
        bot.session = Mock()
        bot.lastInfo = {'old': 'manifest'}
        bot.next_download_attempt_at = 0
        return bot

    def download(self, bot, error=''):
        def launch(**kwargs):
            kwargs['stderr'].write(error.encode())
            return Mock(returncode=1 if error else 0,
                        poll=Mock(return_value=1 if error else 0))

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ffmpeg.subprocess, 'Popen', side_effect=launch) as popen:
                result = ffmpeg.getVideoFfmpeg(
                    bot, 'https://example.test/live.m3u8', str(Path(folder) / 'show.mp4')
                )
        return result, popen.call_args.kwargs['args']

    def test_sm_referer_is_an_input_option(self):
        result, args = self.download(self.make_bot())
        self.assertTrue(result)
        self.assertEqual(args[args.index('-referer') + 1], 'https://streamate.com/')
        self.assertLess(args.index('-referer'), args.index('-i'))

    def test_401_discards_url_defers_and_fetches_fresh_manifest(self):
        bot = self.make_bot()
        before = monotonic()
        result, _ = self.download(bot, 'Error opening input: Server returned 401 Unauthorized')
        self.assertFalse(result)
        self.assertEqual(bot.lastInfo, {})
        self.assertGreaterEqual(bot.next_download_attempt_at, before + 5)
        self.assertLessEqual(bot.next_download_attempt_at, monotonic() + 5)
        self.assertEqual(bot.sleep_on_error, 5)
        bot.logger.warning.assert_called_once()
        bot.logger.error.assert_not_called()
        new_manifest = {'formats': {'mp4-hls': {'encodings': [{
            'location': 'https://example.test/fresh.m3u8',
            'videoWidth': 1280, 'videoHeight': 720,
        }]}}}
        bot.session.get.return_value = Mock(status_code=200, json=Mock(return_value=new_manifest))
        self.assertEqual(bot.getStatus(), Status.PUBLIC)
        self.assertEqual(bot.sleep_on_error, StreaMate.sleep_on_error)
        self.assertEqual(bot.getVideoUrl(), 'https://example.test/fresh.m3u8')

    def test_other_sm_errors_keep_normal_error_reporting(self):
        bot = self.make_bot()
        result, _ = self.download(bot, 'Connection timed out')
        self.assertFalse(result)
        self.assertEqual(bot.next_download_attempt_at, 0)
        bot.logger.error.assert_called_once()

    def test_other_sites_do_not_get_sm_options_or_handler(self):
        bot = SimpleNamespace(headers={'User-Agent': 'test'}, cookies=None, logger=Mock())
        result, args = self.download(bot, 'Server returned 401 Unauthorized')
        self.assertFalse(result)
        self.assertNotIn('-referer', args)
        bot.logger.error.assert_called_once()

    def test_nonpublic_manifest_cannot_reuse_old_url(self):
        bot = self.make_bot()
        bot.session.get.return_value = Mock(status_code=403)
        self.assertEqual(bot.getStatus(), Status.PRIVATE)
        self.assertEqual(bot.lastInfo, {})
        self.assertEqual(bot.session.get.call_args.kwargs['headers']['Referer'], bot.ffmpeg_referer)
        self.assertEqual(bot.session.get.call_args.kwargs['timeout'], (10, 20))

    def test_expired_certificate_retries_with_verification_enabled(self):
        bot = self.make_bot()
        manifest = {'formats': {}}
        bot.session.get.side_effect = [
            SSLError('certificate verify failed: certificate has expired'),
            Mock(status_code=200, json=Mock(return_value=manifest)),
        ]
        self.assertEqual(bot.getStatus(), Status.PUBLIC)
        self.assertEqual(bot.lastInfo, manifest)
        self.assertEqual(bot.session.get.call_count, 2)
        for call in bot.session.get.call_args_list:
            self.assertTrue(call.kwargs['verify'])
            self.assertTrue(call.args[0].startswith('https://manifest-server.naiadsystems.com/'))

    def test_expired_certificates_stop_after_four_attempts(self):
        bot = self.make_bot()
        error = SSLError('certificate verify failed: certificate has expired')
        bot.session.get.side_effect = error
        with self.assertRaises(SSLError) as raised:
            bot.getStatus()
        self.assertIs(raised.exception, error)
        self.assertEqual(bot.session.get.call_count, 4)
        self.assertEqual(bot.lastInfo, {})
        for call in bot.session.get.call_args_list:
            self.assertTrue(call.kwargs['verify'])

    def test_other_connection_errors_are_not_retried(self):
        for error in (SSLError('certificate verify failed: hostname mismatch'),
                      SSLError('unable to get local issuer certificate'),
                      Timeout('read timed out')):
            with self.subTest(error=str(error)):
                bot = self.make_bot()
                bot.session.get.side_effect = error
                with self.assertRaises(type(error)):
                    bot.getStatus()
                bot.session.get.assert_called_once()
                self.assertEqual(bot.lastInfo, {})


if __name__ == '__main__':
    unittest.main()
