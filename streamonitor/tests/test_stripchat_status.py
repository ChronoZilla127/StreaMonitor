import json
import unittest
from unittest.mock import patch

import requests

from streamonitor.enums import Status
from streamonitor.sites.stripchat import StripChat


def response(status, data=None):
    result = requests.Response()
    result.status_code = status
    result._content = b'' if data is None else json.dumps(data).encode()
    if data is not None:
        result.headers['Content-Type'] = 'application/json'
    return result


def public_model():
    return response(200, {
        'user': {'user': {'id': 123, 'username': 'example', 'status': 'public'}},
        'cam': {'isCamAvailable': True, 'isCamActive': True},
    })


class StripChatStatusTests(unittest.TestCase):
    def setUp(self):
        self.static = patch.object(StripChat, '_static_data', {})
        self.static.start()
        self.addCleanup(self.static.stop)

    def create(self, username):
        model = StripChat(username)
        self.addCleanup(model.session.close)
        return model

    @patch.object(requests.Session, 'get', return_value=response(418))
    def test_rejected_lookup_does_not_mark_account_missing(self, get):
        model = self.create('example')
        self.assertIsNone(model.room_id)
        self.assertEqual(model.sc, Status.NOTRUNNING)
        self.assertEqual(model.getStatus(), Status.UNKNOWN)

    @patch.object(requests.Session, 'get', return_value=public_model())
    def test_numeric_add_resolves_name_and_keeps_using_id(self, get):
        model = self.create('123')
        self.assertEqual(model.username, 'example')
        self.assertEqual(model.room_id, '123')
        self.assertEqual(model.getStatus(), Status.PUBLIC)
        for call in get.call_args_list:
            self.assertIn('/models/123/cam?', call.args[0])

    @patch.object(requests.Session, 'get', return_value=response(404, {'error': 'Not Found'}))
    def test_confirmed_missing_account_stays_missing(self, get):
        self.assertEqual(self.create('example').sc, Status.NOTEXIST)

    @patch.object(requests.Session, 'get')
    def test_unresolved_account_recovers_during_bulk_polling(self, get):
        get.return_value = response(418)
        model = self.create('example')
        model.running = True
        model._next_status_lookup_at = 0
        get.side_effect = [response(200, {'id': 123}), public_model()]
        StripChat.getStatusBulk([model])
        self.assertEqual(model.room_id, '123')
        self.assertEqual(model.sc, Status.PUBLIC)

    @patch.object(requests.Session, 'get')
    def test_name_add_resolves_id_before_fetching_cam(self, get):
        get.side_effect = [response(200, {'id': 123}), public_model()]
        model = self.create('example')
        self.assertEqual(model.room_id, '123')
        self.assertIn('/users/user-ids/example?', get.call_args_list[0].args[0])
        self.assertEqual(model.getStatus(), Status.PUBLIC)
        self.assertIn('/models/123/cam?', get.call_args_list[1].args[0])
        self.assertEqual(model.export()['room_id'], '123')

    @patch.object(requests.Session, 'get', return_value=response(418))
    def test_bulk_polling_respects_lookup_cooldown(self, get):
        model = self.create('example')
        StripChat.getStatusBulk([model])
        self.assertEqual(get.call_count, 1)

    @patch.object(requests.Session, 'get', return_value=response(200, []))
    def test_unexpected_json_is_a_temporary_failure(self, get):
        model = self.create('example')
        self.assertEqual(model.getStatus(), Status.UNKNOWN)


if __name__ == '__main__':
    unittest.main()
