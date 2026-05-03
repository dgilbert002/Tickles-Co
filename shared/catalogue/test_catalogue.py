"""
Module: test_catalogue
Purpose: Smoke tests for the catalogue package.
Location: /opt/tickles/shared/catalogue/test_catalogue.py
"""

import unittest
from dataclasses import fields

from shared.catalogue.models import (
    ChannelHierarchy,
    CollectorSource,
    SourceHierarchy,
    WatchedChannel,
    WatchedUser,
)


class TestCollectorSource(unittest.TestCase):
    def test_defaults(self) -> None:
        s = CollectorSource(source_type="discord", source_name="ChartHackers", source_slug="charthackers")
        self.assertTrue(s.is_enabled)
        self.assertEqual(s.display_name, "ChartHackers (discord)")

    def test_fields(self) -> None:
        names = {f.name for f in fields(CollectorSource)}
        self.assertIn("source_type", names)
        self.assertIn("connection_config", names)


class TestWatchedChannel(unittest.TestCase):
    def test_defaults(self) -> None:
        c = WatchedChannel(channel_slug="test", channel_name="test-channel")
        self.assertTrue(c.collect_text)
        self.assertTrue(c.collect_images)
        self.assertTrue(c.collect_charts)
        self.assertFalse(c.collect_videos)
        self.assertEqual(c.display_name, "#test-channel")


class TestWatchedUser(unittest.TestCase):
    def test_display_name_full(self) -> None:
        u = WatchedUser(platform_handle="trader_j", display_name="Trader J")
        self.assertEqual(u.display_name_full, "Trader J (@trader_j)")

    def test_display_name_full_same(self) -> None:
        u = WatchedUser(platform_handle="trader_j", display_name="trader_j")
        self.assertEqual(u.display_name_full, "trader_j")


class TestHierarchy(unittest.TestCase):
    def test_nested(self) -> None:
        src = CollectorSource(id=1, source_type="discord", source_name="X", source_slug="x")
        ch = WatchedChannel(id=1, channel_slug="general", channel_name="General")
        usr = WatchedUser(id=1, platform_handle="u1", display_name="User 1")
        hier = SourceHierarchy(
            source=src,
            channels=[ChannelHierarchy(channel=ch, users=[usr])],
        )
        self.assertEqual(hier.source.source_name, "X")
        self.assertEqual(hier.channels[0].channel.channel_name, "General")
        self.assertEqual(hier.channels[0].users[0].display_name, "User 1")


if __name__ == "__main__":
    unittest.main()
