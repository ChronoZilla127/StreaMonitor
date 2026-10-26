import json
import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from streamonitor.sites.myfreecams import MyFreeCams


class MyFreeCamsTlsTests(unittest.TestCase):
    def make_bot(self):
        bot = MyFreeCams.__new__(MyFreeCams)
        bot.username = 'test'
        bot.logger = Mock()
        bot._get_server_config = Mock(return_value={
            'websocket_servers': {'wchat1': 'rfc6455', 'wchat2': 'rfc6455'},
        })
        return bot

    def test_system_roots_and_requests_bundle_are_loaded(self):
        with patch('streamonitor.sites.myfreecams.ssl.create_default_context') as default:
            context = MyFreeCams._websocket_ssl_context()
        default.assert_called_once_with()
        import requests
        context.load_verify_locations.assert_called_once_with(cafile=requests.certs.where())

    def test_context_requires_certificates_and_hostname_verification(self):
        context = MyFreeCams._websocket_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)

    def test_certificate_failure_retries_securely_and_resolves_model(self):
        bot = self.make_bot()
        payload = json.dumps({'lv': 4, 'uid': 123, 'vs': 0, 'u': {'camserv': 456}})
        frame = f'10 0 0 0 0 {payload}'
        ws = Mock()
        ws.recv.return_value = f'{len(frame):06d}{frame}'
        error = ssl.SSLCertVerificationError('unable to get local issuer certificate')
        with patch('streamonitor.sites.myfreecams.websocket.create_connection',
                   side_effect=[error, ws]) as connect:
            self.assertEqual(bot._lookup_model(), {'uid': 123, 'vs': 0, 'camserv': 456})
        self.assertEqual(connect.call_count, 2)
        for call in connect.call_args_list:
            options = call.kwargs['sslopt']
            self.assertEqual(options['cert_reqs'], ssl.CERT_REQUIRED)
            self.assertTrue(options['check_hostname'])
            self.assertEqual(options['context'].verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(options['context'].check_hostname)
        ws.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
