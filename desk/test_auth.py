"""Authentication boundary checks use synthetic data and an isolated local HTTP server."""
import concurrent.futures
import hashlib
import http.client
import http.server
import importlib
import json
import os
import pathlib
import secrets
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from desk_auth import AuthError, PasswordAuth


PASSWORD = 'a synthetic password for tests'
PUBLIC_HOST = 'support.example.com'
PUBLIC_ORIGIN = 'https://' + PUBLIC_HOST


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = [1000.0]
        self.auth = PasswordAuth(pathlib.Path(self.folder.name) / 'auth.sqlite3', clock=lambda: self.now[0])

    def test_password_hash_sessions_restart_and_revocation(self):
        self.assertFalse(self.auth.configured())
        token, lifetime = self.auth.setup(PASSWORD, PASSWORD, PUBLIC_ORIGIN, True)
        self.assertEqual(lifetime, 30 * 86400)
        self.assertTrue(self.auth.authenticated(token, PUBLIC_ORIGIN))
        self.assertFalse(self.auth.authenticated(token, 'http://localhost:4150'))
        self.assertFalse(self.auth.authenticated(secrets.token_urlsafe(32), PUBLIC_ORIGIN))
        self.assertFalse(self.auth.authenticated('é' * 43, PUBLIC_ORIGIN))
        self.assertNotIn(PASSWORD.encode(), self.auth.path.read_bytes())
        self.assertEqual(self.auth.path.stat().st_mode & 0o777, 0o600)
        restarted = PasswordAuth(self.auth.path, clock=lambda: self.now[0])
        self.assertTrue(restarted.authenticated(token, PUBLIC_ORIGIN))
        restarted.logout(token)
        self.assertFalse(self.auth.authenticated(token, PUBLIC_ORIGIN))

    def test_expiry_and_password_recovery(self):
        token, lifetime = self.auth.setup(PASSWORD, PASSWORD, PUBLIC_ORIGIN)
        self.assertEqual(lifetime, 8 * 3600)
        self.now[0] += lifetime + 1
        self.assertFalse(self.auth.authenticated(token, PUBLIC_ORIGIN))
        token, _ = self.auth.login(PASSWORD, PUBLIC_ORIGIN, True)
        self.auth.reset_password('a replacement test password', 'a replacement test password')
        self.assertFalse(self.auth.authenticated(token, PUBLIC_ORIGIN))
        with self.assertRaises(AuthError):
            self.auth.login(PASSWORD, PUBLIC_ORIGIN)
        token, _ = self.auth.login('a replacement test password', PUBLIC_ORIGIN)
        self.assertTrue(self.auth.authenticated(token, PUBLIC_ORIGIN))

    def test_concurrent_setup_has_one_owner(self):
        def setup():
            try:
                self.auth.setup(PASSWORD, PASSWORD, PUBLIC_ORIGIN)
                return 200
            except AuthError as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: setup(), range(2))), [200, 409])
        with sqlite3.connect(self.auth.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM password').fetchone()[0], 1)

    def test_rate_limit_persists_and_expires(self):
        self.auth.setup(PASSWORD, PASSWORD, PUBLIC_ORIGIN)
        with mock.patch.object(self.auth, '_hash', return_value=b'incorrect hash'):
            for _ in range(self.auth.MAX_ATTEMPTS):
                with self.assertRaises(AuthError) as failed:
                    self.auth.login('wrong password', PUBLIC_ORIGIN)
                self.assertEqual(failed.exception.status, 401)
        restarted = PasswordAuth(self.auth.path, clock=lambda: self.now[0])
        with self.assertRaises(AuthError) as limited:
            restarted.login(PASSWORD, PUBLIC_ORIGIN)
        self.assertEqual(limited.exception.status, 429)
        self.assertGreater(limited.exception.retry_after, 0)
        self.now[0] += self.auth.WINDOW + 1
        token, _ = restarted.login(PASSWORD, PUBLIC_ORIGIN)
        self.assertTrue(restarted.authenticated(token, PUBLIC_ORIGIN))

    def test_invalid_setup_and_unavailable_database_fail_closed(self):
        for password, confirmation in [('short6', 'short6'), (PASSWORD, PASSWORD + '!'), ('x' * 257, 'x' * 257)]:
            with self.assertRaises(AuthError):
                self.auth.setup(password, confirmation, PUBLIC_ORIGIN)
        self.assertFalse(self.auth.configured())
        self.auth.path.write_bytes(b'not a database')
        with self.assertRaises(AuthError) as failure:
            PasswordAuth(self.auth.path).configured()
        self.assertEqual(failure.exception.status, 503)

    def test_seven_character_password_setup_login_and_reset(self):
        password = 'test7pw'
        token, _ = self.auth.setup(password, password, PUBLIC_ORIGIN)
        self.assertTrue(self.auth.authenticated(token, PUBLIC_ORIGIN))
        self.auth.logout(token)
        token, _ = self.auth.login(password, PUBLIC_ORIGIN)
        self.assertTrue(self.auth.authenticated(token, PUBLIC_ORIGIN))
        with self.assertRaises(AuthError):
            self.auth.reset_password('short6', 'short6')
        replacement = 'new7pwd'
        self.auth.reset_password(replacement, replacement)
        self.assertFalse(self.auth.authenticated(token, PUBLIC_ORIGIN))
        token, _ = self.auth.login(replacement, PUBLIC_ORIGIN)
        self.assertTrue(self.auth.authenticated(token, PUBLIC_ORIGIN))


class HandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.home = pathlib.Path(cls.folder.name)
        with mock.patch('pathlib.Path.home', return_value=cls.home), mock.patch('shutil.which', return_value=None), mock.patch.dict(os.environ, {'SUPPORT_DESK_AUTH_PATH': str(cls.home / 'initial.sqlite3'), 'SUPPORT_DESK_DATA_DIR': str(cls.home / 'data'), 'SUPPORT_DESK_PUBLIC_ORIGIN': PUBLIC_ORIGIN}):
            cls.app = importlib.import_module('server')
        cls.seed = cls.home / 'seed.sqlite3'
        PasswordAuth(cls.seed).reset_password(PASSWORD, PASSWORD)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.folder.name)
        self.addCleanup(self.case.cleanup)
        self.path = pathlib.Path(self.case.name) / 'auth.sqlite3'
        shutil.copyfile(self.seed, self.path)
        self.auth = PasswordAuth(self.path)
        self.patches = [mock.patch.object(self.app, 'AUTH', self.auth),
                        mock.patch.object(self.app, 'PUBLIC_ORIGIN', PUBLIC_ORIGIN),
                        mock.patch.object(self.app, 'state', return_value={'version': 'synthetic', 'private': 'dummy customer data'}),
                        mock.patch.object(self.app, 'act', return_value=(200, 'Synthetic action'))]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), self.app.Handler)
        self.port = self.http.server_port
        self.origin = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        options = dict(headers or {})
        if method == 'POST':
            options.setdefault('Origin', self.origin)
            options.setdefault('Content-Type', 'application/json')
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.request(method, path, body, options)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self, remote=False, remember=True):
        headers = {'Host': PUBLIC_HOST, 'Origin': PUBLIC_ORIGIN} if remote else {}
        status, response_headers, _ = self.request('POST', '/auth/login', {'password': PASSWORD, 'remember': remember}, headers)
        self.assertEqual(status, 200)
        return response_headers['Set-Cookie'].split(';', 1)[0], response_headers['Set-Cookie']

    def test_every_private_entry_point_requires_a_session(self):
        for path in ['/api/state', '/api/messages?id=0123456789abcdef', '/static/app.js', '/static/index.html']:
            self.assertEqual(self.request('GET', path)[0], 401, path)
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/login')
        self.assertNotIn(self.app.TOKEN.encode(), body)
        self.assertEqual(self.request('POST', '/api/action', {'action': 'check-now'}, {'X-Desk-Token': self.app.TOKEN})[0], 401)
        self.app.state.assert_not_called()
        self.app.act.assert_not_called()

    def test_login_character_loads_before_sign_in(self):
        status, headers, page = self.request('GET', '/login')
        self.assertEqual(status, 200)
        self.assertIn(b'<desk-bot ', page)
        self.assertNotIn(b'{{', page)  # every placeholder (nonce, mode, desk name, location) is filled in
        nonce = page.split(b'nonce="', 1)[1].split(b'"', 1)[0].decode()
        self.assertIn("'nonce-" + nonce + "'", headers['Content-Security-Policy'])
        for path in ['/static/bot.js', '/static/bot-appearance.js', '/static/bot3d.js', '/static/bot2d.js',
                     '/static/bot-still.png', '/static/vendor/three-0.186.1/three.webgpu.min.js',
                     '/static/vendor/three-0.186.1/three.core.js', '/static/vendor/three-0.186.1/three.tsl.min.js',
                     '/static/vendor/three-0.186.1/RoomEnvironment.js']:
            self.assertEqual(self.request('GET', path)[0], 200, path)
        self.assertEqual(self.request('GET', '/api/state')[0], 401)
        self.assertEqual(self.request('GET', '/static/app.js')[0], 401)
        self.app.state.assert_not_called()

    def test_login_cookie_authenticated_data_logout_and_replay(self):
        cookie, full_cookie = self.login()
        self.assertIn('HttpOnly', full_cookie)
        self.assertIn('SameSite=Lax', full_cookie)
        self.assertIn('Max-Age=2592000', full_cookie)
        status, _, body = self.request('GET', '/api/state', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['version'], 'synthetic')
        status, _, body = self.request('GET', '/', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn(self.app.TOKEN.encode(), body)
        self.assertNotIn(b'{{', body)
        status, headers, _ = self.request('POST', '/auth/logout', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.request('GET', '/api/state', headers={'Cookie': cookie})[0], 401)

    def test_public_cookie_origin_and_host_guards(self):
        cookie, full_cookie = self.login(remote=True)
        self.assertTrue(cookie.startswith('__Host-desk_session='))
        self.assertIn('Secure', full_cookie)
        self.assertIn('Path=/', full_cookie)
        self.assertNotIn('Domain=', full_cookie)
        self.assertEqual(self.request('GET', '/api/state', headers={'Host': PUBLIC_HOST, 'Cookie': cookie})[0], 200)
        self.assertEqual(self.request('GET', '/api/state', headers={'Cookie': cookie.replace('__Host-', '')})[0], 401)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.test', 'X-Forwarded-Host': PUBLIC_HOST})[0], 403)
        self.assertEqual(self.request('POST', '/auth/login', {'password': PASSWORD}, {'Origin': 'https://evil.test'})[0], 403)
        self.assertEqual(self.request('POST', '/auth/logout', headers={'Host': PUBLIC_HOST, 'Cookie': cookie, 'Origin': 'https://evil.test'})[0], 403)
        self.assertEqual(self.request('POST', '/auth/login', {'password': PASSWORD}, {'Origin': 'null'})[0], 403)

    def test_actions_need_session_origin_and_csrf_token(self):
        cookie, _ = self.login()
        headers = {'Cookie': cookie}
        self.assertEqual(self.request('POST', '/api/action', {'action': 'check-now'}, headers)[0], 403)
        headers['X-Desk-Token'] = 'é'
        self.assertEqual(self.request('POST', '/api/action', {'action': 'check-now'}, headers)[0], 403)
        headers['X-Desk-Token'] = self.app.TOKEN
        self.assertEqual(self.request('POST', '/api/action', {'action': 'check-now'}, headers)[0], 200)
        self.app.act.assert_called_once_with({'action': 'check-now'})

    def test_setup_is_one_time_and_local_only(self):
        self.auth = PasswordAuth(pathlib.Path(self.case.name) / 'unconfigured.sqlite3')
        self.app.AUTH = self.auth
        body = {'password': PASSWORD, 'confirmation': PASSWORD}
        status, _, html = self.request('GET', '/login')
        self.assertEqual(status, 200)
        self.assertIn(b'data-mode="setup"', html)
        self.assertEqual(self.request('POST', '/auth/setup', body, {'Host': PUBLIC_HOST, 'Origin': PUBLIC_ORIGIN})[0], 403)
        self.assertEqual(self.request('POST', '/auth/setup', body, {'CF-Connecting-IP': '192.0.2.1'})[0], 403)
        self.assertEqual(self.request('POST', '/auth/setup', body, {'X-Forwarded-For': '192.0.2.1'})[0], 403)
        _, _, remote_page = self.request('GET', '/login', headers={'Host': PUBLIC_HOST})
        self.assertIn(b'data-mode="locked"', remote_page)
        self.assertEqual(self.request('POST', '/auth/setup', body)[0], 200)
        self.assertEqual(self.request('POST', '/auth/setup', body)[0], 409)
        self.assertEqual(self.request('POST', '/signup', body)[0], 401)
        self.assertEqual(self.request('POST', '/register', body)[0], 401)

    def test_input_limits_and_session_cookie_without_remember(self):
        self.assertEqual(self.request('POST', '/auth/login', '[')[0], 400)
        self.assertEqual(self.request('POST', '/auth/login', '[]')[0], 400)
        self.assertEqual(self.request('POST', '/auth/login', {'password': 'x' * 5000})[0], 413)
        self.assertEqual(self.request('POST', '/auth/login', PASSWORD, {'Content-Type': 'text/plain'})[0], 400)
        _, cookie = self.login(remember=False)
        self.assertNotIn('Max-Age', cookie)


if __name__ == '__main__':
    unittest.main()
