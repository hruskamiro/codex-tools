from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from codex_tools import agents_tui, relay_cache


class PeerScopingTests(unittest.TestCase):
    def test_cached_routes_to_same_host_survive_alternate_route_updates_and_reopening(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.sqlite3"
            cache = relay_cache.RelayCache(path)
            cache.ensure_host("server", "Server", base_url="http://127.0.0.1:18781/")
            cache.ensure_host("server", "Server", base_url="http://127.0.0.1:18782")
            reopened = relay_cache.RelayCache(path)
            for url in ("http://127.0.0.1:18781", "http://127.0.0.1:18782/"):
                self.assertEqual(reopened.peer_host_id(url), "server")
            items = [agents_tui.AgentItem("server", "Server", {"agent_id": "one"}, "http://127.0.0.1:18782"),
                     agents_tui.AgentItem("other", "Other", {"agent_id": "two"}, "http://127.0.0.1:18780")]
            scoped = agents_tui.peer_agent_items(reopened, items, "http://127.0.0.1:18781")
            self.assertEqual(len(scoped), 1)
            self.assertEqual(scoped[0].host_id, "server")
            self.assertEqual(scoped[0].base_url, "http://127.0.0.1:18781")
            self.assertEqual(agents_tui.peer_agent_items(reopened, items, "http://unknown"), [])
            self.assertEqual(agents_tui.peer_agent_items(reopened, items, None), items)

    def test_old_host_cache_url_still_scopes_before_first_new_sync(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "cache.sqlite3")
            cache.ensure_host("server", "Server", base_url="http://127.0.0.1:18781")
            with cache.connect() as connection, connection:
                connection.execute("DELETE FROM relay_peer_hosts")
            self.assertEqual(cache.peer_host_id("http://127.0.0.1:18781"), "server")

    def test_pending_disconnect_confirmation_requires_explicit_second_ctrl_d(self):
        window = Mock()
        window.getmaxyx.return_value = (30, 120)
        for key, expected in (("\x04", True), ("n", False), ("\x1b", False)):
            window.get_wch.return_value = key
            self.assertEqual(agents_tui.confirm_pending_disconnect(window, 2, {"status": 0}), expected)
