import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from codex_tools import mobile_typeset as mobile, relay_transport as relay, typeset


class MobileTypesetTests(unittest.TestCase):
    def test_structural_order_and_fences_containing_table_syntax(self):
        text = 'Before.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nBetween.\n\n````md\n```\n| X | Y |\n|---|---|\n````\n\nAfter.'
        blocks = mobile.split_blocks(text)
        self.assertEqual([b.kind for b in blocks], ['prose', 'table', 'prose', 'code', 'prose'])
        self.assertIn('| X | Y |', blocks[3].text)
        self.assertEqual(typeset.fenced_code_blocks(blocks[3].text), ['```\n| X | Y |\n|---|---|'])
        latex = typeset.markdown_to_latex('~~~bash\necho hi\n~~~', code_mode='verbatim')
        self.assertIn('echo hi', latex)
        self.assertIn('Verbatim', latex)

    def test_nested_nodes_and_display_math_are_never_cut(self):
        nested = '- Item\n\n  ```bash\n  echo nested\n  ```\n\n> Quote\n>\n> | A | B |\n> |---|---|\n> | 1 | 2 |'
        self.assertEqual([b.kind for b in mobile.split_blocks(nested)], ['prose'])
        formula = '$$\n' + ('x+y\n\n' * 600) + '$$'
        blocks = mobile.split_blocks(formula)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, formula)
        indented = mobile.split_blocks('    echo hi\n    echo bye')[0]
        self.assertEqual(indented.kind, 'code')
        self.assertEqual(typeset.fenced_code_blocks(indented.text), ['echo hi\necho bye\n'])

    def test_long_prose_chunks_and_resource_bounds(self):
        text = '\n\n'.join('Paragraph ' + str(i) + ': ' + 'word '*70 for i in range(30))
        blocks = mobile.split_blocks(text)
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(b.kind == 'prose' for b in blocks))
        for i in range(30):
            self.assertEqual(sum(('Paragraph ' + str(i) + ':') in b.text for b in blocks), 1)
        with self.assertRaises(ValueError): mobile.split_blocks('x' * (mobile.MAX_SOURCE + 1))
        with self.assertRaises(ValueError): mobile.split_blocks('\n\n'.join('```\na\n```' for _ in range(129)))

    def test_layout_dimensions_and_separate_cache_keys(self):
        for layout, width in [('viewer', '150mm'), ('phone-portrait', '68mm'), ('phone-wide', '180mm')]:
            doc = typeset.document_for('Hello', layout_mode=layout)
            self.assertIn('minipage}{'+width+'}', doc)
        keys = {typeset.cache_key('Hello', layout_mode=layout) for layout in ('viewer','phone-portrait','phone-wide')}
        self.assertEqual(len(keys), 3)
        with self.assertRaises(ValueError): typeset.cache_key('Hello', layout_mode='arbitrary')

    def test_only_server_selected_blocks_and_wide_tables_or_code(self):
        text = 'Before\n\n| A | B |\n|---|---|\n| 1 | 2 |'
        with patch.object(relay, 'agent_record_text', return_value=(text, '')):
            manifest = relay.mobile_manifest(None, 'agent', 'a'*64)
            self.assertEqual([b['kind'] for b in manifest['blocks']], ['prose','table'])
            with patch.object(typeset, 'render_pdf') as render:
                relay.render_mobile_block(None, 'agent', 'a'*64, 1, 'phone-wide')
                self.assertEqual(render.call_args.kwargs['layout_mode'], 'phone-wide')
                self.assertEqual(render.call_args.args[0], manifest['blocks'][1]['text'])
                for index, layout in [(0,'phone-wide'), (2,'phone-portrait'), (1,'arbitrary')]:
                    with self.assertRaises(ValueError): relay.render_mobile_block(None, 'agent', 'a'*64, index, layout)
                self.assertEqual(render.call_count, 1)

    def test_authenticated_routes_reject_arbitrary_markdown_paths_and_layouts(self):
        handler = relay.make_handler(object(), {'identity': {'token': 'secret'}})
        instance = handler.__new__(handler)
        instance.headers = Message()
        responses, served = [], []
        instance._json = lambda value, status=200: responses.append((value,status))
        instance._attachment = lambda *args: served.append(args)
        base = '/v1/agents/agent/typeset/'+'a'*64+'/blocks'
        instance.path = base
        with patch.object(relay, 'mobile_manifest', return_value={'version':1}) as manifest, patch.object(relay, 'render_mobile_block') as render:
            instance.do_GET()
            self.assertEqual(responses[-1][1], 401)
            manifest.assert_not_called()
            instance.headers['Authorization'] = 'Bearer secret'
            instance.do_GET()
            manifest.assert_called_once()
            for suffix in ('?markdown=arbitrary', '?layout=phone-wide', '/0?path=/etc/passwd', '/0?layout=phone-wide&layout=phone-wide', '/0?layout=evil'):
                instance.path = base + suffix
                if suffix.endswith('evil'):
                    render.side_effect = ValueError('bad layout')
                instance.do_GET()
                self.assertEqual(responses[-1][1], 400)
            render.side_effect = None
            with tempfile.TemporaryDirectory() as directory:
                pdf = Path(directory)/'block.pdf'
                pdf.write_bytes(b'%PDF-1.4\n')
                render.return_value = typeset.TypesetResult(True,'b'*64,pdf)
                instance.path = base+'/1?layout=phone-wide'
                instance.do_GET()
                self.assertEqual(render.call_args.args[-2:], (1,'phone-wide'))
                self.assertEqual(served[-1][1], 'application/pdf')


if __name__ == '__main__': unittest.main()
