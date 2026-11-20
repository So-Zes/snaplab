import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
import http.client
import unittest
from unittest.mock import patch

from snaplab.harnesses import wsl, remote, remote_worker, wsl_network
from snaplab import i18n
from snaplab.proxy import Translator


class WslSecurityTest(unittest.TestCase):
    def test_private_and_transition_addresses_never_leave_broker(self):
        for host in ('127.0.0.1', '10.1.2.3', '172.16.0.1', '172.31.255.255',
                     '192.168.1.2', '169.254.1.1', '100.64.0.1', '100.127.255.255',
                     '0.0.0.0', '224.0.0.1', '192.0.0.8', '198.18.0.1'):
            answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (host, 443))]
            with self.subTest(host=host), patch('socket.getaddrinfo', return_value=answer), self.assertRaises(ValueError):
                wsl_network.public_addresses('example.com', 443)

    def test_tailnet_and_single_label_names_are_not_resolved(self):
        for name in ('worker-pc', 'agent.tail123.ts.net', 'agent.TS.NET.', 'host.docker.internal', 'box.local'):
            with self.subTest(name=name), patch('socket.getaddrinfo') as resolve, self.assertRaises(ValueError):
                wsl_network.public_addresses(name, 80)
            resolve.assert_not_called()

    def test_mixed_public_private_dns_answer_is_rejected(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80)) for ip in ('1.1.1.1', '10.0.0.1')]
        with patch('socket.getaddrinfo', return_value=answers), self.assertRaises(ValueError):
            wsl_network.public_addresses('example.com', 80)

    def test_connect_pins_vetted_numeric_ip(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        with patch('socket.getaddrinfo', return_value=answer) as resolve, patch('socket.socket') as sock:
            wsl_network.connect_public('example.com', 443)
            resolve.assert_called_once()
            sock.return_value.connect.assert_called_once_with(('1.1.1.1', 443))

    def test_public_address_owned_by_windows_is_rejected(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        with patch('socket.getaddrinfo', return_value=answer), self.assertRaises(ValueError):
            wsl_network.public_addresses('example.com', 443, {'1.1.1.1'})

    def test_arbitrary_ports_are_rejected(self):
        for port in (22, 445, 8445, 18445, 8444, 18440, 65535):
            with self.assertRaises(ValueError):
                wsl_network.connect_public('example.com', port)

    def test_stale_missing_and_failed_verification_disable_wsl(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(wsl, 'directory', return_value=Path(tmp)), patch.object(wsl, 'version', return_value='new'):
            self.assertFalse(wsl.ready())
            for state in ({}, {'version': 'old', 'isolation_ok': True}, {'version': 'new', 'isolation_ok': False}):
                Path(tmp, 'ready.json').write_text(json.dumps(state), encoding='utf-8')
                self.assertFalse(wsl.ready())

    def test_wsl_always_uses_strict_wrapper_and_no_systemd_escape(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, SNAPLAB_WSL='1'):
            command = remote_worker.isolated_command(['/bin/true'], Path(tmp), Path('/home/snaplab'), tmp)
            self.assertEqual(command[:3], ['/usr/bin/python3', '-I', '/opt/snaplab/wsl_runtime.py'])
            self.assertEqual(remote_worker.limited_command(command), command)

    def test_existing_sessions_keep_their_backend(self):
        app = type('App', (), {'cfg': {'codex': {'backend': 'wsl'}}})()
        self.assertEqual(remote.backend(app, {'execution_backend': 'ssh'}), 'ssh')
        self.assertEqual(remote.backend(app, {}), 'local')

    def test_all_languages_explain_disabled_workspace(self):
        for lang in i18n.codes():
            for key in ('label', 'description', 'not_ready'):
                self.assertTrue(i18n.has(lang, 'harness.wsl.' + key))

    def test_all_interface_addresses_not_just_dns_are_collected(self):
        import struct
        raw = struct.pack('<I', 3) + b''.join(socket.inet_aton(ip) + bytes(20)
                    for ip in ('10.0.0.2', '1.1.1.1', '127.0.0.1'))
        with patch.object(wsl, '_interface_table', return_value=raw):
            self.assertEqual(wsl.host_addresses(), ['1.1.1.1', '10.0.0.2', '127.0.0.1'])

    def test_interface_discovery_fails_closed(self):
        for output in (b'', bytes(28), b'\xff' * 28):
            with patch.object(wsl, '_interface_table', return_value=output), self.assertRaises(RuntimeError):
                wsl.host_addresses()

    def test_each_connection_refreshes_windows_interfaces(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        with patch('socket.getaddrinfo', return_value=answer), patch('socket.socket'), \
                patch.object(wsl, 'host_addresses', side_effect=[[], ['1.1.1.1']]) as discovery:
            wsl_network.connect_public('example.com', 443, discovery)
            with self.assertRaises(ValueError):
                wsl_network.connect_public('example.com', 443, discovery)
            self.assertEqual(discovery.call_count, 2)

    def test_new_local_interface_between_discovery_and_connect_gets_no_bytes(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        with patch('socket.getaddrinfo', return_value=answer), patch('socket.socket') as factory:
            sock = factory.return_value
            sock.getsockname.return_value = ('1.1.1.1', 12345)
            sock.getpeername.return_value = ('1.1.1.1', 443)
            with self.assertRaises(ValueError):
                wsl_network.connect_public('example.com', 443, [], reject_self=True)
            sock.sendall.assert_not_called()
            sock.recv.assert_not_called()
            sock.close.assert_called_once()

    def test_internet_tunnel_requires_job_token_and_explicit_enablement(self):
        with tempfile.TemporaryDirectory() as tmp:
            translator = Translator(port=0, api_cfg={'base_url': 'http://invalid', 'model': 'test'},
                                    slots=None, key_fn=lambda: None, token='synthetic-only',
                                    log_path=Path(tmp) / 'log')
            self.assertTrue(translator.start())
            try:
                for enabled, token in ((False, 'synthetic-only'), (True, ''), (True, 'wrong')):
                    with patch('socket.create_connection', wraps=socket.create_connection):
                        callback = unittest.mock.Mock(side_effect=ValueError('blocked'))
                        translator.egress = callback if enabled else None
                        client = http.client.HTTPConnection('127.0.0.1', translator.port, timeout=3)
                        client.request('CONNECT', '1.1.1.1:443', headers={'Proxy-Authorization': 'Bearer ' + token})
                        self.assertEqual(client.getresponse().status, 403)
                        client.close()
                        callback.assert_not_called()
                client = http.client.HTTPConnection('127.0.0.1', translator.port, timeout=3)
                client.request('CONNECT', '127.0.0.1:80', headers={'Proxy-Authorization': 'Bearer synthetic-only'})
                self.assertEqual(client.getresponse().status, 403)
                client.close()
                callback.assert_called_once_with('127.0.0.1', 80)
            finally:
                translator.stop()


if __name__ == '__main__':
    unittest.main()
