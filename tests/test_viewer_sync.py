from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_tools import search, viewer


class ViewerSyncTests(unittest.TestCase):
    def test_revision_detects_edits_as_well_as_appends(self):
        records = [dict(line_no=1, role='assistant', timestamp='now', text='One')]
        initial = viewer.conversation_update_state(records)
        self.assertEqual(initial, viewer.conversation_update_state([search.TextRecord("now", "assistant", "One", 1, "message")]))
        records[0]['text'] = 'Edited'
        self.assertNotEqual(initial['revision'], viewer.conversation_update_state(records)['revision'])
        records.append(dict(line_no=2, role='user', timestamp='later', text='Next'))
        self.assertEqual(viewer.conversation_update_state(records)['totalCount'], 2)

    def test_local_check_does_not_render_or_discover_attachments(self):
        record = dict(line_no=1, role='user', timestamp='now', text='Hello')
        with patch.object(viewer, 'resolve_session_path', return_value=Path('/fake')), \
             patch.object(viewer, 'read_session', return_value=SimpleNamespace(records=[record])) as read, \
             patch.object(viewer, 'attachment_link_payload') as attachments, \
             patch.object(viewer, 'handle_typeset') as typeset:
            result = viewer.handle_session_updates(None, {'tools': ['1']})
        self.assertEqual(result['totalCount'], 1)
        self.assertIsNone(result['refreshWarning'])
        self.assertTrue(read.call_args.kwargs['include_tools'])
        attachments.assert_not_called()
        typeset.assert_not_called()

    def test_remote_check_passes_refresh_and_preserves_stale_warning(self):
        query = {'source': ['relay'], 'refresh': ['1']}
        with patch.object(viewer, 'relay_session_snapshot', return_value=({}, [], 'Peer offline')) as snapshot:
            result = viewer.handle_session_updates(None, query)
        snapshot.assert_called_once_with(None, query)
        self.assertEqual(result['refreshWarning'], 'Peer offline')

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for browser controller checks')
    def test_quiet_checks_manual_badge_auto_follow_races_and_recovery(self):
        script = r'''
const assert = require('node:assert/strict');
const controls = new Map();
function control() { return {classList:{toggle(){}},setAttribute(){},hidden:true,textContent:'',innerHTML:''}; }
for (const id of ['syncToggle','syncMode','syncBadge','syncStatus']) controls.set(id, control());
const document = {hidden:false, getElementById:id=>controls.get(id),addEventListener(){}};
let scheduled = [], busy = [], refreshes = 0;
const window = {clearTimeout(){},setTimeout:(fn,ms)=>{scheduled.push(ms);return 1;}};
const location = {search:'',href:'http://localhost/v/local/test'};
const history = {replaceState:(_a,_b,url)=>{location.href=String(url);}};
const params = new URLSearchParams();
const identityParams = ()=>new URLSearchParams({id:'test'});
let loadingConversation = false;
let lastConversationData = {totalCount:2,updateState:{revision:'a'}};
let next = {totalCount:3,revision:'b'};
let fail = false, fetches = 0, onFetch = ()=>{};
const fetch = async ()=>{fetches++;onFetch(); if(fail) throw Error('offline');return {ok:true,json:async()=>next};};
async function refreshConversation(options) {
  refreshes++;
  busy.push(true);
  assert.equal(options.jumpToLatest,true);
  lastConversationData = {totalCount:next.totalCount,updateState:{revision:next.revision}};
  busy.push(false);
}
'''+viewer._CONVERSATION_SYNC_JS+r'''
(async()=>{
  await checkConversationUpdates();
  assert.equal(refreshes,0);
  assert.deepEqual(busy,[]); // Probes never animate the refresh control.
  assert.equal(controls.get('syncBadge').hidden,false);
  assert.equal(controls.get('syncBadge').textContent,'1');
  await toggleConversationSync();
  assert.equal(refreshes,1);
  await checkConversationUpdates();
  assert.equal(refreshes,1); // Unchanged auto checks do not render or jump.
  next = {totalCount:4,revision:'c'};
  await checkConversationUpdates();
  assert.equal(refreshes,2);
  assert.equal(controls.get('syncBadge').hidden,true);
  await toggleConversationSync();
  next = {totalCount:4,revision:'edited'};
  await checkConversationUpdates();
  assert.equal(controls.get('syncBadge').textContent,'·');
  assert.equal(refreshes,2);
  fail = true;
  await checkConversationUpdates();
  assert.equal(syncOffline,true);
  assert.equal(scheduled.at(-1),4000);
  assert.equal(lastConversationData.updateState.revision,'c');
  fail = false;
  await checkConversationUpdates();
  assert.equal(syncOffline,false);
  assert.equal(scheduled.at(-1),2000);
  const before = fetches;
  document.hidden = true;
  await checkConversationUpdates();
  assert.equal(fetches,before);
  document.hidden = false;
  syncChanged = false;
  onFetch = ()=>{lastConversationData={totalCount:4,updateState:{revision:'replacement'}};};
  await checkConversationUpdates();
  assert.equal(syncChanged,false); // Ignore a check overtaken by another refresh.
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
