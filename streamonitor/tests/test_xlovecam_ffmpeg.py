import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from streamonitor.downloaders import ffmpeg
from streamonitor.sites.xlovecam import XLoveCam


class XLoveCamFfmpegTests(unittest.TestCase):
    def make_bot(self):
        bot = XLoveCam.__new__(XLoveCam)
        bot.cookies = None
        bot.logger = Mock()
        return bot

    def download(self, bot, error=''):
        def launch(**kwargs):
            kwargs['stderr'].write(error.encode())
            return Mock(returncode=1 if error else 0,
                        poll=Mock(return_value=1 if error else 0))

        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'show.mp4'
            # An existing partial recording must survive the error handler.
            filename.write_bytes(b'partial recording')
            with patch.object(ffmpeg.subprocess, 'Popen', side_effect=launch) as process:
                result = ffmpeg.getVideoFfmpeg(bot, 'https://example.test/live.m3u8', str(filename))
            self.assertEqual(filename.read_bytes(), b'partial recording')
        return result, process.call_args.kwargs['args']

    def test_adts_failure_switches_next_attempt_to_audio_encoding(self):
        bot = self.make_bot()
        result, args = self.download(bot, '[aac_adtstoasc] Error parsing ADTS frame header!')
        self.assertFalse(result)
        self.assertEqual(args[args.index('-c:a') + 1], 'copy')
        self.assertEqual(bot.ffmpeg_audio_codec, 'aac')
        bot.logger.warning.assert_called_once()
        bot.logger.error.assert_not_called()

        result, args = self.download(bot)
        self.assertTrue(result)
        self.assertEqual(args[args.index('-c:a') + 1], 'aac')
        self.assertEqual(args[args.index('-c:v') + 1], 'copy')

    def test_encoding_failure_is_reported_normally(self):
        bot = self.make_bot()
        bot.ffmpeg_audio_codec = 'aac'
        self.assertFalse(self.download(bot, 'Error parsing ADTS frame header!')[0])
        bot.logger.error.assert_called_once()
        bot.logger.warning.assert_not_called()

    def test_unrelated_failure_does_not_enable_encoding(self):
        bot = self.make_bot()
        self.assertFalse(self.download(bot, 'Connection timed out')[0])
        self.assertEqual(bot.ffmpeg_audio_codec, 'copy')
        bot.logger.error.assert_called_once()

    def test_readrate_is_disabled_only_for_xlc(self):
        other = SimpleNamespace(headers={'User-Agent': 'test'}, cookies=None, logger=Mock())
        with patch.object(ffmpeg, 'FFMPEG_READRATE', 1.3):
            _, xlc_args = self.download(self.make_bot())
            _, other_args = self.download(other)
        self.assertNotIn('-readrate', xlc_args)
        self.assertIn('-readrate', other_args)
        self.assertEqual(other_args[other_args.index('-c:a') + 1], 'copy')


if __name__ == '__main__':
    unittest.main()
