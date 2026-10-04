"""Running behind a hosting platform's proxy, such as Railway.

The platform says which port in $PORT, puts its own proxy in front, and only
that proxy's headers say who the visitor is and whether they came over https.
"""

from __future__ import annotations

import os
import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillerai.cli import build_parser
from fillerai.web.server import Handler


def _handler(headers: dict[str, str], trust: bool) -> Handler:
    handler = Handler.__new__(Handler)
    handler.headers = Message()
    for name, value in headers.items():
        handler.headers[name] = value
    handler.client_address = ("10.0.0.7", 51000)
    handler.trust_proxy = trust
    return handler


class PortTest(unittest.TestCase):
    def test_port_comes_from_the_environment_when_set(self):
        with mock.patch.dict(os.environ, {"PORT": "6123"}):
            args = build_parser().parse_args(["serve"])
        self.assertEqual(args.port, 6123)

    def test_without_it_the_port_is_8000_and_the_host_stays_local(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = build_parser().parse_args(["serve"])
        self.assertEqual((args.port, args.host, args.trust_proxy),
                         (8000, "127.0.0.1", False))

    def test_an_explicit_port_still_wins(self):
        with mock.patch.dict(os.environ, {"PORT": "6123"}):
            args = build_parser().parse_args(["serve", "--port", "9000"])
        self.assertEqual(args.port, 9000)

    def test_trusting_the_proxy_can_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"FILLERAI_TRUST_PROXY": "1"}):
            args = build_parser().parse_args(["serve"])
        self.assertTrue(args.trust_proxy)


class ProxyTest(unittest.TestCase):
    FORWARDED = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9",
                 "X-Forwarded-Proto": "https"}

    def test_untrusted_headers_are_ignored(self):
        handler = _handler(self.FORWARDED, trust=False)
        self.assertEqual(handler._visitor_address(), "10.0.0.7")
        self.assertFalse(handler._forwarded_https())

    def test_trusted_proxy_gives_the_address_it_appended(self):
        handler = _handler(self.FORWARDED, trust=True)
        self.assertEqual(handler._visitor_address(), "203.0.113.9")
        self.assertTrue(handler._forwarded_https())

    def test_x_real_ip_is_preferred(self):
        handler = _handler({**self.FORWARDED, "X-Real-IP": "198.51.100.4"}, trust=True)
        self.assertEqual(handler._visitor_address(), "198.51.100.4")

    def test_no_forwarding_headers_falls_back_to_the_connection(self):
        handler = _handler({}, trust=True)
        self.assertEqual(handler._visitor_address(), "10.0.0.7")
        self.assertFalse(handler._forwarded_https())


if __name__ == "__main__":
    unittest.main()
