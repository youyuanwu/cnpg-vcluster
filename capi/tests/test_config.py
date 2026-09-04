from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.lib.config import ConfigError, load_env_file, parse_duration


class ConfigTests(unittest.TestCase):
    def test_loads_comments_quotes_and_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.env"
            path.write_text('# comment\nONE=value\nTWO="two values"\n', encoding="utf-8")
            values = load_env_file(path, overrides={"ONE": "override"})
        self.assertEqual(values, {"ONE": "override", "TWO": "two values"})

    def test_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.env"
            path.write_text("ONE=1\nONE=2\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_env_file(path, overrides={})

    def test_rejects_unquoted_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.env"
            path.write_text("ONE=two values\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_env_file(path, overrides={})

    def test_parse_duration(self) -> None:
        self.assertEqual(parse_duration("5m"), 300)
        self.assertEqual(parse_duration("2h"), 7200)
        with self.assertRaises(ConfigError):
            parse_duration("1.5m")
