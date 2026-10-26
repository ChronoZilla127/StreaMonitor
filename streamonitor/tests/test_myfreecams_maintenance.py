import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from streamonitor.enums import Status
from streamonitor.sites.myfreecams import MyFreeCams


class MyFreeCamsMaintenanceTests(unittest.TestCase):
    def setUp(self):
        state = patch.multiple(MyFreeCams, _maintenance=False,
                               _maintenance_checked_at=None,
                               _server_config=None, _server_config_time=0)
        state.start()
        self.addCleanup(state.stop)
        self.clock = patch('streamonitor.sites.myfreecams.time.monotonic', return_value=100).start()
        self.addCleanup(patch.stopall)

    def make_bot(self):
        with patch.object(MyFreeCams, 'cache_file_list'):
            bot = MyFreeCams('maintenance_test')
        bot.logger = Mock()
        bot.session = Mock()
        bot._lookup_model = Mock(return_value=None)
        bot.model_info = {'uid': 1, 'vs': 0}
        bot.videoUrl = 'https://example.test/stale.m3u8'
        return bot

    @staticmethod
    def page(code=503, text='<title>MyFreeCams Server Maintenance</title>'):
        return Mock(status_code=code, text=text)

    def test_confirmed_maintenance_clears_stale_stream(self):
        bot = self.make_bot()
        bot.session.get.return_value = self.page()
        self.assertEqual(bot.getStatus(), Status.MAINTENANCE)
        self.assertIsNone(bot.model_info)
        self.assertIsNone(bot.videoUrl)
        self.assertIn('mantenimiento', bot.status_messages[Status.MAINTENANCE])

    def test_channels_share_probe_and_skip_websockets_during_maintenance(self):
        first, second = self.make_bot(), self.make_bot()
        first.session.get.return_value = self.page()
        self.assertEqual(first.getStatus(), Status.MAINTENANCE)
        self.assertEqual(second.getStatus(), Status.MAINTENANCE)
        self.assertEqual(first.getStatus(), Status.MAINTENANCE)
        first._lookup_model.assert_called_once()
        second._lookup_model.assert_not_called()
        first.session.get.assert_called_once()
        second.session.get.assert_not_called()

    def test_generic_503_is_not_called_maintenance(self):
        bot = self.make_bot()
        bot.session.get.return_value = self.page(text='Service unavailable')
        self.assertEqual(bot.getStatus(), Status.UNKNOWN)

    def test_unreachable_homepage_is_not_called_maintenance(self):
        bot = self.make_bot()
        bot.session.get.side_effect = requests.Timeout('timeout')
        self.assertEqual(bot.getStatus(), Status.UNKNOWN)

    def test_confirmed_maintenance_survives_inconclusive_probes(self):
        for response in (self.page(text='Service unavailable'),
                         self.page(code=403, text='Forbidden'),
                         requests.Timeout('timeout')):
            with self.subTest(response=response):
                bot = self.make_bot()
                MyFreeCams._maintenance = True
                MyFreeCams._maintenance_checked_at = None
                bot.session.get.side_effect = [response]
                self.assertEqual(bot.getStatus(), Status.MAINTENANCE)
                bot._lookup_model.assert_not_called()

    def test_recovery_refreshes_config_and_resolves_public_channel(self):
        bot = self.make_bot()
        bot.session.get.return_value = self.page(code=200, text='Welcome to MyFreeCams')
        MyFreeCams._maintenance = True
        MyFreeCams._server_config = {'old': 'config'}
        bot._lookup_model.return_value = {'uid': 1, 'vs': 0}
        bot.getVideoUrl = Mock(return_value='https://example.test/fresh.m3u8')
        self.assertEqual(bot.getStatus(), Status.PUBLIC)
        self.assertFalse(MyFreeCams._maintenance)
        self.assertIsNone(MyFreeCams._server_config)
        self.assertEqual(bot.videoUrl, 'https://example.test/fresh.m3u8')

    def test_monitor_retries_after_60_seconds_and_starts_recording(self):
        bot = self.make_bot()
        bot.running = True
        bot.session.get.side_effect = [self.page(), self.page(200, 'Welcome')]
        bot._lookup_model.side_effect = [None, {'uid': 1, 'vs': 0}]
        bot.getVideoUrl = Mock(return_value='https://example.test/fresh.m3u8')
        bot.genOutFilename = Mock(return_value='test.mp4')
        bot.cache_file_list = Mock()

        def wait(seconds):
            self.clock.return_value += seconds

        def record(*args):
            bot.quitting = True
            bot.running = False
            return True

        bot._sleep = Mock(side_effect=wait)
        bot.getVideo = Mock(side_effect=record)
        bot.run()
        bot._sleep.assert_called_once_with(60)
        bot.getVideo.assert_called_once_with(bot, 'https://example.test/fresh.m3u8', 'test.mp4')
        self.assertFalse(MyFreeCams._maintenance)
        bot.logger.exception.assert_not_called()


if __name__ == '__main__':
    unittest.main()
