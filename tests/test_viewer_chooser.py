from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_tools import relay_cache, viewer


class ViewerChooserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = relay_cache.RelayCache(Path(self.temporary.name) / "relay.sqlite3")
        agent = relay_cache.CachedAgent(
            agent_id="same-id", session_id="session", thread_id="thread",
            rollout_id="rollout", title="Remote conversation", title_source="index",
            cwd="/work/project", source="cli", created_at="2026-10-07T10:00:00Z",
            updated_at="2026-10-07T11:00:00Z", origin_path="/sessions/remote.jsonl", records=(),
        )
        for host, name in (("one", "First machine"), ("two", "Second machine"), ("local", "Local")):
            self.cache.replace_host_snapshot(host, name, [agent])
        self.state = SimpleNamespace(relay_cache=self.cache, titles={}, session_paths=Mock(return_value=[]))

    def test_remote_only_is_metadata_only_and_host_scoped(self):
        with patch.object(self.cache, "records", side_effect=AssertionError("No transcript reads")):
            result = viewer.handle_sessions(self.state, {"source": ["relay"], "host": ["two"]})
        self.state.session_paths.assert_not_called()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["sessions"][0]["hostId"], "two")
        self.assertTrue(result["sessions"][0]["cached"])

    def test_same_agent_on_different_hosts_is_not_deduplicated(self):
        result = viewer.handle_sessions(self.state, {"source": ["all"]})
        self.assertEqual({item["hostId"] for item in result["sessions"]}, {"one", "two"})

    def test_remote_search_includes_peer_and_workdir(self):
        result = viewer.handle_sessions(self.state, {"source": ["relay"], "q": ["second machine"]})
        self.assertEqual([item["hostId"] for item in result["sessions"]], ["two"])
        result = viewer.handle_sessions(self.state, {"source": ["relay"], "q": ["/work/project"]})
        self.assertEqual(result["total"], 2)

    def test_api_default_stays_local_for_terminal_picker(self):
        with patch.object(self.cache, "hosts", side_effect=AssertionError("Local only")):
            self.assertEqual(viewer.handle_sessions(self.state, {})["sessions"], [])
        self.state.session_paths.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser behavior checks")
    def test_browser_shortcut_and_routes(self):
        shared = viewer._RESOURCE_LINKS_JS.split("const LINK_HINT_DELAY_MS", 1)[0]
        view_href = viewer.APP_JS.split("function conversationId(item)", 1)[1].split("function selectedSessionLink()", 1)[0]
        handlers = [script.rsplit('window.addEventListener("keydown", (event) => {', 1)[1].split('}, true);', 1)[0]
                    for script in (viewer.VIEW_JS, viewer.TYPESET_JS)]
        script = """
const assert = require('node:assert/strict');
let identity = new URLSearchParams('source=relay&host=peer-one');
const identityParams = () => identity;
const location = {pathname: '/t/relay/peer-one/agent/42'};
let opened = [];
const window = {open: (...args) => opened.push(args)};
SHARED
assert.equal(conversationChooserHref(), '/launcher?view=latex&source=relay&host=peer-one');
openConversationChooser();
assert.deepEqual(opened[0].slice(1), ['_blank', 'noopener']);
identity = new URLSearchParams('source=local');
location.pathname = '/v/local/agent';
assert.equal(conversationChooserHref(), '/launcher?view=markdown');
const state = {defaultView: 'latex'};
const els = {tailCount: {value: '24'}, includeTools: {checked: false}};
function conversationId(item) VIEW_HREF
assert.equal(viewHref({id:'agent',hostId:'peer'}), '/t/relay/peer/agent?tail=24');
assert.equal(viewHref({id:'agent'}), '/t/local/agent?tail=24');
let jumped = 0;
const scrollToLatestAssistant = () => jumped++;
for (const body of HANDLERS) {
  const handler = eval('(event) => {' + body + '}');
  const event = {key:'L',code:'KeyL',shiftKey:true,preventDefault(){}};
  const before = opened.length;
  handler(event);
  assert.equal(opened.length, before + 1);
  handler({...event, repeat:true});
  handler({...event, ctrlKey:true});
  handler({...event, target:{closest:()=>true}});
  assert.equal(opened.length, before + 1);
  handler({...event, key:'l', shiftKey:false});
}
assert.equal(jumped, 2);
""".replace("SHARED", shared).replace("VIEW_HREF", view_href).replace("HANDLERS", json.dumps(handlers))
        subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)
