import hashlib
import hmac
import json
import os
import secrets
import time


class AuthError(Exception):
    pass


DEFAULT_USERS_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "odoo-receipt", "users.json")
PEPPER_ENV = "ODOO_RECEIPT_PEPPER"
PBKDF2_ITERATIONS = 240000
SESSION_TTL = 8 * 3600


_PEPPER = b""


def set_pepper(value):
    """Set the server-wide pepper. Call once at startup, before any
    password is hashed or verified."""
    global _PEPPER
    _PEPPER = (value or "").encode("utf-8") if isinstance(value, str) \
        else (value or b"")


def _pepper():
    return _PEPPER


def hash_password(password, iterations=PBKDF2_ITERATIONS, pepper=None):
    """Return a pbkdf2-hmac-sha256 record for the password.

    The record is a mapping ready to store in the user file. The pepper is a
    server-wide secret that is mixed into the hash, so the user file alone
    is not enough to verify a password offline. It comes from the
    ODOO_RECEIPT_PEPPER environment variable; a random one is written to a
    pepper file next to the user file when the variable is not set.
    """
    salt = secrets.token_bytes(16)
    if pepper is None:
        pepper = _pepper()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8") + pepper, salt, iterations)
    return {"algo": "pbkdf2_sha256", "iterations": iterations,
            "salt": salt.hex(), "hash": digest.hex()}


def load_or_create_pepper(folder):
    """Return the pepper bytes, creating a random pepper file if needed.

    The pepper lives in a file named pepper.txt inside the config folder.
    It must never be committed or restored from a public backup: without
    it the stored password hashes cannot be verified.
    """
    env = os.environ.get(PEPPER_ENV)
    if env:
        return env.encode("utf-8")
    if not folder:
        folder = "."
    path = os.path.join(folder, "pepper.txt")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
            if value:
                return value.encode("utf-8")
    except OSError:
        pass
    value = secrets.token_hex(32)
    os.makedirs(folder, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value.encode("utf-8")


def verify_password(password, record):
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        iterations = int(record["iterations"])
    except (KeyError, TypeError, ValueError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8") + _pepper(), salt, iterations)
    return hmac.compare_digest(digest, expected)


class UserStore:
    """JSON file with the bridge users and their password hashes."""

    def __init__(self, path):
        self.path = path
        self.data = {"users": {}}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            data = None
        except (OSError, ValueError):
            raise AuthError("cannot read the user file %s" % self.path)
        if isinstance(data, dict) and isinstance(data.get("users"), dict):
            self.data = data
        return self.data

    def save(self):
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2)
        os.replace(temp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def is_empty(self):
        return not self.load().get("users")

    def is_setup_complete(self):
        return bool(self.load().get("setup_complete"))

    def mark_setup_complete(self):
        if not self.load().get("setup_complete"):
            self.data["setup_complete"] = True
            self.save()

    def get(self, username):
        users = self.load().get("users") or {}
        return users.get(str(username or "").strip().lower())

    def list(self):
        users = self.load().get("users") or {}
        return [{"username": name, "role": user.get("role", "pos")}
                for name, user in sorted(users.items())]

    def create(self, username, password, role):
        name = str(username or "").strip().lower()
        if not name or any(c.isspace() for c in name):
            raise AuthError("the username must be one word")
        if len(password or "") < 8:
            raise AuthError("the password must be at least 8 characters")
        if role not in ("admin", "pos"):
            raise AuthError("the role must be admin or pos")
        users = self.load().setdefault("users", {})
        if name in users:
            raise AuthError("the user exists already")
        users[name] = {"role": role, "password": hash_password(password)}
        self.save()
        return {"username": name, "role": role}

    def set_password(self, username, password):
        user = self.get(username)
        if not user:
            raise AuthError("unknown user")
        if len(password or "") < 8:
            raise AuthError("the password must be at least 8 characters")
        user["password"] = hash_password(password)
        self.save()

    def set_role(self, username, role):
        user = self.get(username)
        if not user:
            raise AuthError("unknown user")
        if role not in ("admin", "pos"):
            raise AuthError("the role must be admin or pos")
        user["role"] = role
        self.save()

    def delete(self, username):
        name = str(username or "").strip().lower()
        users = self.load().get("users") or {}
        if name not in users:
            raise AuthError("unknown user")
        del users[name]
        self.save()

    def authenticate(self, username, password):
        user = self.get(username)
        if not user or not verify_password(password or "",
                                           user.get("password") or {}):
            return None
        return {"username": str(username).strip().lower(),
                "role": user.get("role", "pos")}


class SessionStore:
    """In-memory session store with expiry."""

    def __init__(self, ttl=SESSION_TTL):
        self.ttl = ttl
        self.sessions = {}

    def create(self, username, role):
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.sessions = {key: value for key, value in self.sessions.items()
                         if value["expires"] > now}
        self.sessions[token] = {"username": username, "role": role,
                                "expires": now + self.ttl}
        return token

    def get(self, token):
        session = self.sessions.get(token or "")
        if not session:
            return None
        if session["expires"] <= time.time():
            self.destroy(token)
            return None
        return session

    def destroy(self, token):
        self.sessions.pop(token or "", None)

    def destroy_all(self, username):
        self.sessions = {key: value
                         for key, value in self.sessions.items()
                         if value["username"] != username}
