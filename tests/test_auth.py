"""golem login without OpenRouter: PKCE, key storage, the localhost callback, and the exchange.
The real login needs a person to approve in a browser, so it is not run here."""

import http.server
import os
import stat
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from golem import auth


class PkceTest(unittest.TestCase):
    def test_rfc_7636_example(self):
        self.assertEqual(
            auth.challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )

    def test_fresh_pairs_are_valid_and_differ(self):
        (v1, c1), (v2, _c2) = auth.pkce_pair(), auth.pkce_pair()
        self.assertNotEqual(v1, v2)
        self.assertTrue(43 <= len(v1) <= 128)
        self.assertEqual(c1, auth.challenge_for(v1))

    def test_authorize_url_carries_the_challenge_and_callback(self):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(
                auth.authorize_url("CH", "http://localhost:5/callback", "st")
            ).query
        )
        self.assertEqual(
            {key: value[0] for key, value in query.items()},
            {
                "code_challenge": "CH",
                "code_challenge_method": "S256",
                "key_label": "golem",
                "callback_url": "http://localhost:5/callback",
                "state": "st",
            },
        )
        self.assertNotIn("callback_url", auth.authorize_url("CH", None, None))


class KeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": self.home}, clear=False
        )
        self.env.start()
        os.environ.pop("OPENROUTER_API_KEY", None)

    def tearDown(self):
        self.env.stop()

    def test_stored_key_is_private_and_used(self):
        path = auth.store("sk-or-v1-test\n")
        self.assertEqual(path, Path(self.home) / "golem" / "openrouter.key")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(auth.api_key(), "sk-or-v1-test")
        self.assertEqual(auth.key_source(), str(path))

    def test_environment_wins_over_the_stored_key(self):
        auth.store("sk-or-v1-stored")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-env"}):
            self.assertEqual(auth.api_key(), "sk-or-v1-env")
            self.assertEqual(auth.key_source(), "OPENROUTER_API_KEY")

    def test_no_key_at_all(self):
        self.assertIsNone(auth.api_key())


class CallbackTest(unittest.TestCase):
    def serve(self, query: str):
        server = http.server.HTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        )
        port = server.server_address[1]
        thread = threading.Thread(
            target=lambda: urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?{query}", timeout=5
            ).read()
        )
        thread.start()
        try:
            return auth.wait_for_code(server, "good-state", timeout=5)
        finally:
            thread.join()
            server.server_close()

    def test_code_with_the_right_state_is_accepted(self):
        self.assertEqual(self.serve("code=abc123&state=good-state"), "abc123")

    def test_wrong_state_is_refused(self):
        with self.assertRaisesRegex(auth.LoginError, "state"):
            self.serve("code=abc123&state=forged")

    def test_exchange_sends_the_verifier_and_returns_the_key(self):
        with mock.patch.object(
            auth, "_post", return_value={"key": "sk-or-v1-new"}
        ) as post:
            self.assertEqual(auth.exchange(" code \n", "verifier"), "sk-or-v1-new")
        post.assert_called_once_with(
            auth.EXCHANGE_URL,
            {
                "code": "code",
                "code_verifier": "verifier",
                "code_challenge_method": "S256",
            },
        )

    def test_headless_login_stores_the_key(self):
        home = tempfile.mkdtemp()
        with (
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}),
            mock.patch.object(auth, "_post", return_value={"key": "sk-or-v1-h"}),
        ):
            path = auth.login(
                headless=True, say=lambda _text: None, ask=lambda _prompt: "pasted-code"
            )
        self.assertEqual(path.read_text().strip(), "sk-or-v1-h")


if __name__ == "__main__":
    unittest.main()
