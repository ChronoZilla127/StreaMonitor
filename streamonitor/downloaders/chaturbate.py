"""Serve a selected HLS master in memory while FFmpeg records its remote tracks."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from secrets import token_urlsafe
from threading import Thread

from streamonitor.downloaders.ffmpeg import getVideoFfmpeg


def recording_error(details):
    """A lost HLS window cannot be repaired by continuing the same recording."""
    if re.search(r'skipping \d+ segments ahead|Failed to open segment|Error when loading (?:first )?segment', details, re.I):
        return 'Chaturbate lost HLS segments; closing this file and acquiring a fresh session'
    return None


@contextmanager
def selected_master_url(master):
    payload = master.encode('utf-8')
    path = '/' + token_urlsafe(24) + '/master.m3u8'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != path:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass  # Do not log signed URLs or the private local path.

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}{path}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def getVideoChaturbate(bot, url, filename):
    # Real live sources already pace themselves; limiting reads can expire HLS
    # segments before FFmpeg has fetched them.
    bot.ffmpeg_disable_readrate = True
    bot.ffmpeg_copy_timestamps = True
    bot.ffmpeg_recording_error = recording_error
    bot.ffmpeg_output_maps = None
    try:
        if bot.chaturbate_master is None:
            return getVideoFfmpeg(bot, url, filename)
        with selected_master_url(bot.chaturbate_master) as local_url:
            bot.ffmpeg_output_maps = ('0:v:0', '0:a:0')
            return getVideoFfmpeg(bot, local_url, filename)
    finally:
        bot.chaturbate_master = None
        bot.ffmpeg_output_maps = None
        bot.ffmpeg_disable_readrate = False
        bot.ffmpeg_copy_timestamps = False
        bot.ffmpeg_recording_error = None
