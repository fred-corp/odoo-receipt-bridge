"""Unit tests for the password hashing, sessions, and user store."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth  # noqa: E402


class PasswordTest(unittest.TestCase):
    def setUp(self):
        auth.set_pepper("unit-test-pepper")

    def test_hash_has_salt_and_iterations(self):
        record = auth.hash_password("unit-test-password")
        self.assertEqual(record["algo"], "pbkdf2_sha256")
        self.assertEqual(record["iterations"], auth.PBKDF2_ITERATIONS)
        self.assertEqual(len(record["salt"]), 32)
        self.assertEqual(len(record["hash"]), 64)

    def test_salt_is_random_per_password(self):
        self.assertNotEqual(auth.hash_password("unit-test-password")["salt"],
                            auth.hash_password("unit-test-password")["salt"])

    def test_verify_roundtrip(self):
        record = auth.hash_password("unit-test-password")
        self.assertTrue(auth.verify_password("unit-test-password", record))
        self.assertFalse(auth.verify_password("unit-test-password2", record))
        self.assertFalse(auth.verify_password("", record))

    def test_pepper_changes_the_hash(self):
        record = auth.hash_password("unit-test-password")
        auth.set_pepper("other-pepper")
        self.assertFalse(auth.verify_password("unit-test-password", record))

    def test_password_not_stored_in_plaintext(self):
        record = auth.hash_password("unit-test-password")
        self.assertNotIn("unit-test-password", str(record))
        self.assertNotIn("unit-test-password", str(record).encode().hex())


class SessionTest(unittest.TestCase):
    def test_create_and_get(self):
        store = auth.SessionStore(ttl=60)
        token = store.create("alice", "admin")
        session = store.get(token)
        self.assertEqual(session["username"], "alice")
        self.assertEqual(session["role"], "admin")

    def test_token_is_long_and_random(self):
        store = auth.SessionStore(ttl=60)
        token1 = store.create("alice", "pos")
        token2 = store.create("alice", "pos")
        self.assertNotEqual(token1, token2)
        self.assertGreaterEqual(len(token1), 32)

    def test_expiry(self):
        store = auth.SessionStore(ttl=-1)
        token = store.create("alice", "pos")
        self.assertIsNone(store.get(token))

    def test_destroy(self):
        store = auth.SessionStore(ttl=60)
        token = store.create("alice", "pos")
        store.destroy(token)
        self.assertIsNone(store.get(token))

    def test_destroy_all_for_user(self):
        store = auth.SessionStore(ttl=60)
        t1 = store.create("alice", "pos")
        t2 = store.create("bob", "pos")
        store.destroy_all("alice")
        self.assertIsNone(store.get(t1))
        self.assertIsNotNone(store.get(t2))


class UserStoreTest(unittest.TestCase):
    def setUp(self):
        auth.set_pepper("unit-test-pepper")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "users.json")
        self.store = auth.UserStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_authenticate(self):
        self.store.create("Alice", "unit-test-password", "admin")
        self.assertIsNotNone(self.store.authenticate("alice", "unit-test-password"))
        self.assertIsNotNone(self.store.authenticate("  ALICE ", "unit-test-password"))

    def test_wrong_password_denied(self):
        self.store.create("alice", "unit-test-password", "pos")
        self.assertIsNone(self.store.authenticate("alice", "wrong"))
        self.assertIsNone(self.store.authenticate("nobody", "unit-test-password"))

    def test_short_password_rejected(self):
        with self.assertRaises(auth.AuthError):
            self.store.create("alice", "short", "pos")

    def test_duplicate_user_rejected(self):
        self.store.create("alice", "unit-test-password", "pos")
        with self.assertRaises(auth.AuthError):
            self.store.create("alice", "otherpass1", "pos")

    def test_role_change_and_delete(self):
        self.store.create("alice", "unit-test-password", "pos")
        self.store.set_role("alice", "admin")
        self.assertEqual(self.store.get("alice")["role"], "admin")
        self.store.delete("alice")
        self.assertIsNone(self.store.get("alice"))

    def test_file_permissions_are_owner_only(self):
        self.store.create("alice", "unit-test-password", "pos")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_setup_complete_flag(self):
        self.assertFalse(self.store.is_setup_complete())
        self.store.mark_setup_complete()
        self.assertTrue(self.store.is_setup_complete())
        self.store.clear_setup_complete()
        self.assertFalse(self.store.is_setup_complete())


class PepperFileTest(unittest.TestCase):
    def test_load_or_create_pepper(self):
        with tempfile.TemporaryDirectory() as folder:
            pepper1 = auth.load_or_create_pepper(folder)
            self.assertTrue(pepper1)
            self.assertEqual(os.stat(os.path.join(folder, "pepper.txt"))
                             .st_mode & 0o777, 0o600)
            pepper2 = auth.load_or_create_pepper(folder)
            self.assertEqual(pepper1, pepper2)

    def test_env_pepper_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            os.environ[auth.PEPPER_ENV] = "from-env"
            try:
                self.assertEqual(auth.load_or_create_pepper(folder),
                                 b"from-env")
            finally:
                del os.environ[auth.PEPPER_ENV]


if __name__ == "__main__":
    unittest.main()
