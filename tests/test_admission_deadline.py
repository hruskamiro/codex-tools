import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from codex_tools import structured


class AdmissionDeadlineTests(unittest.TestCase):
    def test_deadline_drains_active_request_without_starting_queued_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'schema.json').write_text('{"type":"object"}')
            tasks = []
            for index in range(3):
                (root/f'{index}.txt').write_text(str(index))
                tasks.append(dict(id=f't{index}', prompt=f'{index}.txt', schema='schema.json'))
            manifest = dict(version=1, defaults=dict(model='test'), tasks=tasks)
            deadline = time.time() + 60
            wall = [deadline - 1]
            starts, finished = [], []
            def run_task(**kwargs):
                starts.append(kwargs['prompt'])
                wall[0] = deadline + 1
                time.sleep(.02)
                kwargs['run_dir'].mkdir(parents=True)
                structured.atomic_write_json(kwargs['run_dir']/'run.json', dict(
                    status='success', usage={'input_tokens': 1}, duration_s=.02))
                finished.append(kwargs['prompt'])
            with patch.object(structured, 'run_task', side_effect=run_task), \
                 patch.object(structured.time, 'time', side_effect=lambda: wall[0]):
                result = structured.run_batch(manifest=manifest, manifest_path=root/'batch.json',
                    batch_dir=root/'runs', jobs=1,
                    admission_deadline=datetime.fromtimestamp(deadline, timezone.utc).isoformat())
            self.assertEqual(starts, ['0'])
            self.assertEqual(finished, ['0'])
            self.assertEqual(result['counts']['success'], 1)
            self.assertEqual(result['counts']['deferred-deadline'], 2)
            self.assertEqual(result['usage']['input_tokens'], 1)
            self.assertFalse(result['complete'])
            self.assertTrue(all(t.get('executed') is False for t in result['tasks'][1:]))

if __name__ == '__main__':
    unittest.main()
