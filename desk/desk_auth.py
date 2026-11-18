"""Single-owner password login and revocable sessions, kept outside the checkout."""
import contextlib
import hashlib
import hmac
import os
import pathlib
import secrets
import sqlite3
import threading
import time
import unicodedata

if not hasattr(hashlib, "scrypt"):  # the owner password is hashed with scrypt, which needs Python built on OpenSSL
    raise SystemExit("support desk: this Python has no hashlib.scrypt, so it can't hash the owner password. "
                     "Use a Python 3.10+ built with OpenSSL, such as Homebrew's python3 or one from python.org.")


class AuthError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class PasswordAuth:
    WINDOW = 300
    MAX_ATTEMPTS = 8
    REMEMBER_SECONDS = 30 * 86400
    SESSION_SECONDS = 8 * 3600

    def __init__(self, path, clock=time.time):
        self.path = pathlib.Path(path)
        self.clock = clock
        self._init_lock = threading.Lock()
        self._hash_slots = threading.BoundedSemaphore(2)
        self._ready = False

    @contextlib.contextmanager
    def _database(self):
        try:
            with self._init_lock:
                if not self._ready:
                    self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    os.fchmod(fd, 0o600)
                    os.close(fd)
                    with contextlib.closing(sqlite3.connect(self.path, timeout=5)) as db:
                        db.executescript("""
                            CREATE TABLE IF NOT EXISTS password (
                                id INTEGER PRIMARY KEY CHECK (id = 1), salt BLOB NOT NULL, digest BLOB NOT NULL
                            );
                            CREATE TABLE IF NOT EXISTS sessions (
                                digest TEXT PRIMARY KEY, origin TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL
                            );
                            CREATE TABLE IF NOT EXISTS attempts (created REAL NOT NULL);
                        """)
                    self._ready = True
            with contextlib.closing(sqlite3.connect(self.path, timeout=5)) as db:
                with db:
                    yield db
        except (OSError, sqlite3.Error):
            raise AuthError(503, 'Sign-in is temporarily unavailable. Check the desk on your Mac.') from None

    def configured(self):
        with self._database() as db:
            return db.execute('SELECT 1 FROM password WHERE id = 1').fetchone() is not None

    @staticmethod
    def _password(value, setup=False):
        if not isinstance(value, str):
            raise AuthError(400, 'Enter a password.')
        password = unicodedata.normalize('NFC', value)
        if not password or len(password) > 256:
            raise AuthError(400, 'Use a password between 7 and 256 characters.' if setup else 'Enter a valid password.')
        if setup and len(password) < 7:
            raise AuthError(400, 'Use at least 7 characters.')
        return password.encode('utf-8')

    def _hash(self, password, salt):
        if not self._hash_slots.acquire(blocking=False):
            raise AuthError(429, 'Another sign-in is being checked. Try again in a moment.', 2)
        try:
            return hashlib.scrypt(password, salt=salt, n=131072, r=8, p=1, dklen=32, maxmem=256 * 1024 * 1024)
        finally:
            self._hash_slots.release()

    def setup(self, password, confirmation, origin, remember=False):
        raw = self._password(password, setup=True)
        if not isinstance(confirmation, str) or not hmac.compare_digest(raw, self._password(confirmation, setup=True)):
            raise AuthError(400, 'The passwords do not match.')
        if self.configured():
            raise AuthError(409, 'A password is already set. Sign in to continue.')
        salt = secrets.token_bytes(32)
        digest = self._hash(raw, salt)
        with self._database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM password WHERE id = 1').fetchone():
                raise AuthError(409, 'A password is already set. Sign in to continue.')
            db.execute('INSERT INTO password VALUES (1, ?, ?)', (salt, digest))
            return self._new_session(db, origin, remember)

    def login(self, password, origin, remember=False):
        raw = self._password(password)
        now = self.clock()
        with self._database() as db:
            db.execute('BEGIN IMMEDIATE')
            credentials = db.execute('SELECT salt, digest FROM password WHERE id = 1').fetchone()
            if credentials is None:
                raise AuthError(503, 'Set the password on your Mac before signing in here.')
            db.execute('DELETE FROM attempts WHERE created <= ?', (now - self.WINDOW,))
            attempts = db.execute('SELECT COUNT(*), MIN(created) FROM attempts').fetchone()
            if attempts[0] >= self.MAX_ATTEMPTS:
                retry = max(1, int(attempts[1] + self.WINDOW - now) + 1)
                raise AuthError(429, 'Too many sign-in attempts. Try again in a few minutes.', retry)
            db.execute('INSERT INTO attempts VALUES (?)', (now,))
        if not hmac.compare_digest(self._hash(raw, credentials[0]), credentials[1]):
            raise AuthError(401, 'That password did not match. Try again.')
        with self._database() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT digest FROM password WHERE id = 1').fetchone()
            if latest is None or not hmac.compare_digest(credentials[1], latest[0]):
                raise AuthError(401, 'The password changed. Sign in again.')
            db.execute('DELETE FROM attempts')
            return self._new_session(db, origin, remember)

    def _new_session(self, db, origin, remember):
        now = self.clock()
        lifetime = self.REMEMBER_SECONDS if remember else self.SESSION_SECONDS
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        db.execute('DELETE FROM sessions WHERE expires <= ?', (now,))
        db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', (digest, origin, now + lifetime, now))
        db.execute('DELETE FROM sessions WHERE digest IN (SELECT digest FROM sessions ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET 32)')
        return token, lifetime

    def authenticated(self, token, origin):
        if not isinstance(token, str) or len(token) != 43 or not all(c.isalnum() or c in '-_' for c in token):
            return False
        if not token.isascii():
            return False
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with self._database() as db:
            session = db.execute('SELECT expires FROM sessions WHERE digest = ? AND origin = ?', (digest, origin)).fetchone()
            return session is not None and session[0] > self.clock()

    def logout(self, token):
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        with self._database() as db:
            db.execute('DELETE FROM sessions WHERE digest = ?', (digest,))

    def reset_password(self, password, confirmation):
        """Local terminal recovery also revokes every existing session."""
        raw = self._password(password, setup=True)
        if not isinstance(confirmation, str) or not hmac.compare_digest(raw, self._password(confirmation, setup=True)):
            raise AuthError(400, 'The passwords do not match.')
        salt = secrets.token_bytes(32)
        digest = self._hash(raw, salt)
        with self._database() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO password VALUES (1, ?, ?)', (salt, digest))
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM attempts')


def data_dir():
    """Where the desk keeps its password database and page key, outside the checkout.
    SUPPORT_DESK_DATA_DIR moves it, for example to run a second desk for a test."""
    default = pathlib.Path.home() / 'Library' / 'Application Support' / 'support-desk'
    return pathlib.Path(os.environ.get('SUPPORT_DESK_DATA_DIR') or default).expanduser()


def cache_dir():
    """Where the desk keeps the emails it has read. SUPPORT_DESK_CACHE_DIR moves it; with only
    SUPPORT_DESK_DATA_DIR set, it sits inside that folder, so one variable keeps a test desk apart."""
    if os.environ.get('SUPPORT_DESK_CACHE_DIR'):
        return pathlib.Path(os.environ['SUPPORT_DESK_CACHE_DIR']).expanduser()
    if os.environ.get('SUPPORT_DESK_DATA_DIR'):
        return data_dir() / 'cache'
    return pathlib.Path.home() / 'Library' / 'Caches' / 'support-desk'


def auth_path():
    return pathlib.Path(os.environ.get('SUPPORT_DESK_AUTH_PATH') or data_dir() / 'auth.sqlite3').expanduser()


if __name__ == '__main__':
    import getpass
    import sys
    if sys.argv[1:] != ['set-password']:
        raise SystemExit('Usage: python3 desk/desk_auth.py set-password')
    try:
        PasswordAuth(auth_path()).reset_password(getpass.getpass('New password: '), getpass.getpass('Confirm password: '))
    except AuthError as error:
        raise SystemExit(error.message)
    print('Password saved. Existing devices have been signed out.')
