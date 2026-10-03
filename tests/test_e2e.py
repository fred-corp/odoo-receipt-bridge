"""End-to-end tests: the bridge web server against a fake Odoo and printer.

Run with: python tests/test_e2e.py
The tests run in alphabetical order (test_01 ... test_25) against one
server instance, mirroring a fresh install followed by normal use.
"""

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
ODOO_PORT = 18999
PRINTER_PORT = 9100
BRIDGE_PORT = 18222
BASE = "http://127.0.0.1:%d" % BRIDGE_PORT


def printer_server():
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", PRINTER_PORT))
    sock.listen(5)

    def handle(conn):
        conn.recv(4096)
        conn.sendall(b"\x12\x00\x00\x00")
        conn.close()

    while True:
        conn, _ = sock.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class E2ETest(unittest.TestCase):
    odoo_proc = None
    bridge_proc = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config = os.path.join(cls.tmp.name, "config.json")
        env = dict(os.environ, ODOO_RECEIPT_PEPPER="e2e-test-pepper")
        cls.odoo_proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "tests", "fake_odoo.py"),
             str(ODOO_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        time.sleep(0.7)
        cls.bridge_proc = subprocess.Popen(
            [sys.executable, os.path.join(SRC, "odoo_receipt.py"),
             "--config", cls.config, "serve", "--port", str(BRIDGE_PORT)],
            cwd=SRC, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env)
        cls._wait_for_server()
        cls.jar = http.cookiejar.CookieJar()
        cls.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cls.jar))

    @classmethod
    def _wait_for_server(cls, tries=40):
        for _ in range(tries):
            try:
                urllib.request.urlopen(BASE + "/health", timeout=1).read()
                return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("the bridge did not start")

    @classmethod
    def tearDownClass(cls):
        for proc in (cls.bridge_proc, cls.odoo_proc):
            if proc and proc.poll() is None:
                proc.terminate()
        cls.tmp.cleanup()

    def post(self, path, payload, opener=None):
        op = opener or self.opener
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            response = op.open(req)
            return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                return error.code, json.loads(body)
            except ValueError:
                return error.code, {"raw": body.decode("utf-8", "replace")}

    def get(self, path, opener=None, redirect=True):
        op = opener or self.opener
        if not redirect:
            jar = None
            for handler in op.handlers:
                if isinstance(handler, urllib.request.HTTPCookieProcessor):
                    jar = handler.cookiejar
            handlers = [h for h in (NoRedirect(),)]
            if jar:
                handlers.append(urllib.request.HTTPCookieProcessor(jar))
            op = urllib.request.build_opener(*handlers)
        try:
            response = op.open(BASE + path)
            return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def login(self, username, password):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        status, data = self.post("/login", {"username": username,
                                           "password": password},
                                 opener=opener)
        return status, data, opener

    # ---- first run, quickstart -------------------------------------

    def test_01_health(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertIn('"ok"', body)

    def test_02_first_run_redirects_to_quickstart(self):
        status, body = self.get("/", redirect=False)
        self.assertEqual(status, 303)
        self.assertEqual(body, "")
        status, body = self.get("/quickstart", redirect=False)
        self.assertEqual(status, 200)

    def test_03_quickstart_prefills_config(self):
        status, body = self.get("/quickstart", redirect=False)
        self.assertEqual(status, 200)
        self.assertNotIn("__CONFIG__", body)
        self.assertIn("current-config", body)

    def test_04_create_admin(self):
        status, data = self.post("/quickstart/admin",
                                 {"username": "boss",
                                  "password": "test-admin-password"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, data = self.post("/quickstart/admin",
                                 {"username": "boss",
                                  "password": "test-admin-password"})
        self.assertEqual(status, 400)

    def test_05_quickstart_odoo_ok(self):
        status, data = self.post(
            "/quickstart/odoo",
            {"url": "http://127.0.0.1:%d" % ODOO_PORT, "db": "fakedb",
             "user": "fake", "api_key": "fakepw"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["warning"])
        self.assertIn("login OK", data["note"])

    def test_06_quickstart_odoo_failure_is_warning(self):
        status, data = self.post(
            "/quickstart/odoo",
            {"url": "http://127.0.0.1:1", "db": "fakedb",
             "user": "fake", "api_key": "fakepw"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["warning"])
        status, data = self.post(
            "/quickstart/odoo",
            {"url": "http://127.0.0.1:%d" % ODOO_PORT, "db": "fakedb",
             "user": "fake", "api_key": "fakepw"})
        self.assertEqual(status, 200)
        self.assertFalse(data["warning"])

    def test_07_quickstart_printer(self):
        status, data = self.post(
            "/quickstart/printer",
            {"transport": "net",
             "target": "127.0.0.1:%d" % PRINTER_PORT})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["warning"])

    # ---- login, roles, wizard lock ---------------------------------

    def test_08_login_bad_credentials(self):
        status, data, _ = self.login("boss", "test-wrong-password")
        self.assertEqual(status, 403)
        self.assertFalse(data["ok"])

    def test_09_login(self):
        status, data, opener = self.login("boss", "test-admin-password")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["role"], "admin")
        status, body = self.get("/", opener=opener, redirect=False)
        self.assertEqual(status, 200)
        # log the shared opener in too: later tests use self.post()
        status, data = self.post("/login",
                                 {"username": "boss",
                                  "password": "test-admin-password"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_10_quickstart_locked_after_setup(self):
        status, body = self.get("/quickstart", redirect=False)
        self.assertEqual(status, 303)

    def test_11_add_pos_user(self):
        status, data = self.post("/users/add", {"username": "cashier",
                                                "password": "test-pos-password",
                                                "role": "pos"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_12_pos_cannot_reset_setup(self):
        _, _, opener = self.login("cashier", "test-pos-password")
        status, data = self.post("/setup/reset", {}, opener=opener)
        self.assertEqual(status, 403)
        self.assertFalse(data["ok"])

    def test_13_pos_cannot_read_settings(self):
        _, _, opener = self.login("cashier", "test-pos-password")
        status, data = self.post("/settings", {}, opener=opener)
        self.assertEqual(status, 403)

    def test_14_pos_gets_no_settings_link(self):
        _, _, opener = self.login("cashier", "test-pos-password")
        status, body = self.get("/", opener=opener, redirect=False)
        self.assertEqual(status, 200)
        self.assertNotIn("/settings", body)

    def test_15_admin_can_reset_setup(self):
        status, data = self.post("/setup/reset", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.post("/setup/reset", {})

    def test_16_rerun_wizard_keeps_users(self):
        status, data = self.post("/quickstart/admin",
                                 {"username": "boss2",
                                  "password": "test-admin2-password"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, data, _ = self.login("boss2", "test-admin2-password")
        self.assertEqual(status, 200)
        status, data, _ = self.login("cashier", "test-pos-password")
        self.assertEqual(status, 200)

    # ---- settings ----------------------------------------------------

    def test_17_settings_page(self):
        status, data, opener = self.login("boss", "test-admin-password")
        status, body = self.get("/settings", opener=opener, redirect=False)
        self.assertEqual(status, 200)
        self.assertIn("rerun-setup", body)
        self.assertIn("toast", body)
        self.assertIn("menu-btn", body)

    def test_18_settings_json_read(self):
        _, _, opener = self.login("boss", "test-admin-password")
        status, data = self.post("/settings", {}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        settings = data["settings"]
        self.assertEqual(settings["url"],
                         "http://127.0.0.1:%d" % ODOO_PORT)
        self.assertEqual(settings["db"], "fakedb")

    def test_19_save_receipt_settings(self):
        _, _, opener = self.login("boss", "test-admin-password")
        status, data = self.post("/settings/receipt", {
            "shop_name": "My Shop", "shop_address_lines": ["1 Main St"],
            "shop_phone": "01", "footer_lines": ["Thanks"],
            "width": 42, "left_margin": 2, "timezone": "Europe/Brussels",
            "show_unit_price": True, "show_notes": False,
            "show_barcode": True, "decimal_comma": False,
            "price_mode": "total", "internal_receipts": "off"}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        _, data = self.post("/settings", {}, opener=opener)
        s = data["settings"]
        self.assertEqual(s["shop_name"], "My Shop")
        self.assertEqual(s["width"], 42)
        self.assertTrue(s["show_barcode"])

    def test_19b_settings_odoo_save_and_test(self):
        _, _, opener = self.login("boss", "test-admin-password")
        status, data = self.post("/settings/odoo", {
            "url": "http://127.0.0.1:%d" % ODOO_PORT, "db": "fakedb",
            "user": "fake", "api": "jsonrpc", "timeout": 30}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["warning"])
        self.assertIn("login OK", data["note"])

    def test_19c_settings_odoo_bad_url_is_warning(self):
        _, _, opener = self.login("boss", "test-admin-password")
        status, data = self.post("/settings/odoo", {
            "url": "http://127.0.0.1:1", "db": "fakedb",
            "user": "fake", "api": "jsonrpc", "timeout": 30}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["warning"])
        status, data = self.post("/settings/odoo", {
            "url": "http://127.0.0.1:%d" % ODOO_PORT, "db": "fakedb",
            "user": "fake", "api": "jsonrpc", "timeout": 30}, opener=opener)
        self.assertFalse(data["warning"])

    def test_19d_empty_api_key_keeps_stored_key(self):
        _, _, opener = self.login("boss", "test-admin-password")
        status, data = self.post("/settings/odoo", {
            "url": "http://127.0.0.1:%d" % ODOO_PORT, "db": "fakedb",
            "user": "fake", "api_key": "", "api": "jsonrpc",
            "timeout": 30}, opener=opener)
        self.assertEqual(status, 200)
        self.assertFalse(data["warning"])
        _, data = self.post("/settings", {}, opener=opener)
        self.assertTrue(data["settings"]["has_api_key"])
        self.assertGreater(data["settings"]["api_key_length"], 0)
        blob = json.dumps(data)
        self.assertNotIn("fakepw", blob)

    # ---- orders ------------------------------------------------------

    def test_20_anonymous_redirected_to_login(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        status, body = self.get("/", opener=opener, redirect=False)
        self.assertEqual(status, 303)

    def test_21_orders_fetch(self):
        _, _, opener = self.login("boss", "test-admin-password")
        status, body = self.get("/orders?limit=5", opener=opener,
                                redirect=False)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["orders"]), 1)
        self.assertEqual(data["orders"][0]["name"], "S00042")

    def test_22_logout_revokes_session(self):
        _, _, opener = self.login("boss", "test-admin-password")
        status, data = self.post("/logout", {}, opener=opener)
        self.assertEqual(status, 200)
        status, _ = self.get("/", opener=opener, redirect=False)
        self.assertEqual(status, 303)

    # ---- security ----------------------------------------------------

    def test_23_no_secrets_in_settings_view(self):
        _, _, opener = self.login("boss", "test-admin-password")
        _, data = self.post("/settings", {}, opener=opener)
        blob = json.dumps(data)
        self.assertNotIn("fakepw", blob)
        for user in data.get("users", []):
            self.assertEqual(sorted(user.keys()), ["role", "username"])

    def test_24_user_file_has_only_hashes(self):
        users_path = os.path.join(self.tmp.name, "users.json")
        blob = open(users_path).read()
        self.assertNotIn("test-admin-password", blob)
        self.assertNotIn("test-pos-password", blob)
        self.assertIn("pbkdf2_sha256", blob)

    def test_25_unknown_paths_are_404(self):
        _, _, opener = self.login("boss", "test-admin-password")
        status, data = self.post("/nope", {}, opener=opener)
        self.assertEqual(status, 404)

    def test_26_wrong_print_token_rejected(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        status, body = self.get("/orders?limit=1", opener=opener,
                                redirect=False)
        self.assertEqual(status, 303)


def method_number(name):
    digits = "".join(ch for ch in name.split("_")[1] if ch.isdigit())
    return int(digits) if digits else 0


def ordered_suite():
    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = (
        lambda a, b: method_number(a) - method_number(b))
    return loader.loadTestsFromTestCase(E2ETest)


if __name__ == "__main__":
    threading.Thread(target=printer_server, daemon=True).start()
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(ordered_suite())
