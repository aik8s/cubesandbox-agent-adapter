from __future__ import annotations

import os
import unittest
from unittest import mock

from adapter import healthcheck


class FakeResponse:
    status = 200

    def read(self, limit):
        assert limit == 4096
        return b'{"status":"ok"}'

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class HealthcheckTest(unittest.TestCase):
    def test_http_readiness_uses_loopback(self):
        with mock.patch.dict(os.environ, {"CUBE_ADAPTER_PORT": "19090"}, clear=True):
            with mock.patch("adapter.healthcheck.urlopen", return_value=FakeResponse()) as opened:
                self.assertTrue(healthcheck.check("ready"))
        self.assertEqual(opened.call_args.args[0], "http://127.0.0.1:19090/readyz")

    def test_mtls_uses_in_process_tcp_probe(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        with mock.patch.dict(
            os.environ,
            {
                "CUBE_ADAPTER_PORT": "18080",
                "CUBE_ADAPTER_TLS_CLIENT_CA_FILE": "/run/client-ca.crt",
            },
            clear=True,
        ):
            with mock.patch("adapter.healthcheck.socket.create_connection", return_value=connection) as connect:
                self.assertTrue(healthcheck.check("live"))
        connect.assert_called_once_with(("127.0.0.1", 18080), timeout=3.0)


if __name__ == "__main__":
    unittest.main()
