"""Real OAuth/Flask route with the capabilities subprocess boundary injected."""
import subprocess
from unittest.mock import patch

from brain import mcp_bridge
from tests.test_remote_gateway import GatewayTestCase


class TestBackendFailure(GatewayTestCase):
    def test_failure_is_jsonrpc_and_backend_recovers(self):
        token = self.get_token(scope='brain.read')['access_token']
        request = {'jsonrpc': '2.0', 'id': 'recovery', 'method': 'tools/list'}
        headers = {'Authorization': f'Bearer {token}'}
        with patch.object(mcp_bridge, '_run_brain', return_value=subprocess.CompletedProcess(
                [], 1, '', '/private/path synthetic-sensitive-diagnostic')), self.assertNoLogs('brain.remote_gateway.app', level='ERROR'):
            failed = self.client.post('/mcp', headers=headers, json=request)
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.get_json(), {'jsonrpc': '2.0', 'id': 'recovery', 'error': {
            'code': -32603, 'message': 'Brain backend unavailable'}})
        # Real payload adapter, not a patched tools/list result.
        with patch.object(mcp_bridge, '_run_brain', return_value=subprocess.CompletedProcess(
                [], 0, '{"interfaces": {"mcp": {"tools": [{"name": "list_notes", "description": "Synthetic listing", "input_schema": {"type": "object"}}]}}}', '')):
            recovered = self.client.post('/mcp', headers=headers, json=request)
        self.assertEqual(recovered.status_code, 200)
        self.assertIn('list_notes', [t['name'] for t in recovered.get_json()['result']['tools']])

    def test_bridge_error_is_sanitized_and_notification_has_no_response(self):
        token = self.get_token(scope='brain.read')['access_token']
        headers = {'Authorization': f'Bearer {token}'}
        with patch.object(mcp_bridge, '_tools_list_payload', side_effect=mcp_bridge.BridgeError('/private/hidden')) as lookup:
            response = self.client.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'id': 0, 'method': 'tools/list'})
            self.assertEqual(response.get_json()['id'], 0)
            self.assertNotIn('/private', response.get_data(as_text=True))
            notification = self.client.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'method': 'tools/list'})
            self.assertEqual(notification.status_code, 202)
            self.assertEqual(notification.data, b'')
            self.assertEqual(lookup.call_count, 1)
