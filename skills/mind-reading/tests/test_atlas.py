"""Private atlas safety and provenance; no model calls or imported personal data."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import quiet
from mr.atlas import AtlasError, MARKER, build_atlas
from mr.config import Paths, write_json


def payload_from_file(path):
    document = Path(path).read_text(encoding='utf-8')
    match = re.search(r'<script type="application/json" id="atlas-data">(.*?)</script>', document, re.S)
    return json.loads(match.group(1)), document


def sidecars(paths, malicious=''):
    paths.ensure_tree()
    seed = 'fix the export' + malicious
    addition = 'also check every download path before calling it fixed' + malicious
    write_json(paths.model / 'episodes.json', {'version': 1, 'split_id': 'fictional-train', 'episodes': [
        {'id': 'echo-1', 'seed': seed, 'addition': addition, 'signals': ['scope', 'quality'], 'kinds': ['fix'],
         'source_ids': ['fictional:session-a:turn-1', 'fictional:session-a:turn-2'],
         'session_id': 'fictional:session-a', 'time': '2026-01-02', 'project': 'fictional-project'}]})
    write_json(paths.model / 'evidence.json', {'version': 1, 'training': {'split_id': 'fictional-train'}, 'items': [
        {'id': 'trait-1', 'kind': 'trait', 'text': 'Check sibling download paths' + malicious,
         'confidence': 'strong', 'evidence_class': 'stated_rule', 'session_count': 2, 'project_count': 1,
         'source_ids': ['fictional:session-a:turn-2', 'fictional:session-b:turn-3'], 'evidence': [
             {'quote': addition, 'origin': 'typed', 'source_id': 'fictional:session-a:turn-2'},
             {'quote': 'agent suggestion is not user evidence', 'origin': 'drafted', 'source_id': 'fictional:draft:1'}]}]})


class AtlasTests(unittest.TestCase):
    def test_exact_words_and_provenance_come_from_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'home')
            sidecars(paths)
            output = Path(tmp) / 'atlas.html'
            result = build_atlas(paths, output)
            data, document = payload_from_file(output)
            self.assertEqual(result['engine_calls'], 0)
            self.assertEqual((result['episodes'], result['claims'], result['typed_quotes']), (1, 1, 1))
            self.assertEqual(data['episodes'][0]['addition'], 'also check every download path before calling it fixed')
            self.assertEqual(data['claims'][0]['quotes'][0]['source_id'], 'fictional:session-a:turn-2')
            self.assertEqual(len(data['claims'][0]['quotes']), 1)
            self.assertEqual(data['claims'][0]['confidence'], 'strong')
            self.assertIn('Local word matching, not a prediction', document)
            self.assertIn('aria-live="polite"', document)
            self.assertIn('prefers-reduced-motion', document)
            self.assertNotIn('innerHTML', document)
            self.assertNotIn('fetch(', document)
            self.assertNotRegex(document, r'<(?:script|link|img)\b[^>]*(?:src|href)=["\']https?://')
            if os.name == 'posix':
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_malicious_transcript_is_inert_but_roundtrips_exactly(self):
        injection = '</script><img src=x onerror=alert(1)><script>alert(2)</script>\u2028&"'
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'home')
            sidecars(paths, injection)
            output = Path(tmp) / 'atlas.html'
            build_atlas(paths, output)
            data, document = payload_from_file(output)
            self.assertTrue(data['episodes'][0]['seed'].endswith(injection))
            self.assertTrue(data['claims'][0]['quotes'][0]['quote'].endswith(injection))
            self.assertNotIn(injection, document)
            self.assertNotIn('<img src=x', document)
            self.assertEqual(document.count('</script>'), 2)
            self.assertIn('\\u003c/script\\u003e', document)

    def test_missing_sidecars_render_honest_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'missing')
            output = Path(tmp) / 'atlas.html'
            result = build_atlas(paths, output)
            data, document = payload_from_file(output)
            self.assertEqual(result['episodes'], 0)
            self.assertEqual(result['claims'], 0)
            self.assertEqual(data['counts']['typed_quotes'], 0)
            self.assertIn('No intent echoes yet.', document)
            self.assertIn('The evidence ledger is empty.', document)
            self.assertFalse(paths.home.exists())

    def test_unlinked_episode_and_empty_typed_quote_are_not_presented_as_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'home')
            paths.ensure_tree()
            write_json(paths.model / 'episodes.json', {'episodes': [{'seed': 'fix it', 'addition': 'also check the siblings'}]})
            write_json(paths.model / 'evidence.json', {'items': [{'text': 'an unverified claim', 'evidence': [
                {'quote': '', 'origin': 'typed', 'source_id': 'empty-quote'}]}]})
            output = Path(tmp) / 'atlas.html'
            result = build_atlas(paths, output)
            data, _ = payload_from_file(output)
            self.assertEqual(result['episodes'], 0)
            self.assertEqual(result['typed_quotes'], 0)
            self.assertEqual(data['claims'][0]['confidence'], 'unknown')

    def test_existing_unrelated_output_is_preserved_even_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'home')
            output = Path(tmp) / 'atlas.html'
            output.write_text('personal work')
            for force in (False, True):
                with self.assertRaises(AtlasError):
                    build_atlas(paths, output, force=force)
            self.assertEqual(output.read_text(), 'personal work')

    def test_rebuild_is_deterministic_and_changed_own_atlas_needs_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'home')
            sidecars(paths)
            output = Path(tmp) / 'atlas.html'
            first = build_atlas(paths, output)
            same = build_atlas(paths, output)
            self.assertEqual(first['evidence_hash'], same['evidence_hash'])
            self.assertFalse(same['rebuilt'])
            write_json(paths.model / 'episodes.json', {'version': 1, 'episodes': []})
            with self.assertRaises(AtlasError):
                build_atlas(paths, output)
            updated = build_atlas(paths, output, force=True)
            self.assertNotEqual(first['evidence_hash'], updated['evidence_hash'])
            self.assertEqual(updated['episodes'], 0)
            self.assertIn(MARKER, output.read_text())

    def test_symlink_output_and_non_html_output_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'home')
            outside = Path(tmp) / 'personal.html'
            outside.write_text('personal work')
            if os.name == 'posix':
                linked = Path(tmp) / 'linked.html'
                linked.symlink_to(outside)
                with self.assertRaises(AtlasError):
                    build_atlas(paths, linked, force=True)
                self.assertEqual(outside.read_text(), 'personal work')
            with self.assertRaises(AtlasError):
                build_atlas(paths, paths.profile)

    def test_malformed_sidecar_fails_before_output_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'home')
            paths.model.mkdir(parents=True)
            (paths.model / 'episodes.json').write_text('not json')
            output = Path(tmp) / 'atlas.html'
            with self.assertRaises(AtlasError):
                build_atlas(paths, output)
            self.assertFalse(output.exists())

    @unittest.skipUnless(shutil.which('node'), 'optional JavaScript behavior check needs Node')
    def test_actual_inline_script_filters_searches_and_switches_views(self):
        # This is a local DOM contract harness, not a browser or a visual layout check.
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / 'home')
            sidecars(paths, '</script><img src=x onerror=alert(1)>')
            output = Path(tmp) / 'atlas.html'
            build_atlas(paths, output)
            payload, document = payload_from_file(output)
            script = re.search(r'<script>(.*?)</script>', document, re.S).group(1)
            harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const nodes=new Map(),created=[];
class Element {
 constructor(tag){this.tag=tag;this.children=[];this._text='';this.attributes={};this.value='';}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this._text='';this.children=children;}
 setAttribute(key,value){this.attributes[key]=String(value);}
}
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);},
createElement(tag){created.push(tag);return new Element(tag);}};
document.getElementById('atlas-data').textContent=JSON.stringify(input.payload);
vm.runInNewContext(input.script,{document});
const el=id=>document.getElementById(id);
assert.equal(el('result-count').textContent,'1 / 1 echoes');
assert.ok(el('episode-list').textContent.includes(input.payload.episodes[0].addition));
el('query').value='download';el('query').oninput();assert.equal(el('result-count').textContent,'1 / 1 echoes');
el('query').value='down';el('query').oninput();assert.equal(el('result-count').textContent,'0 / 1 echoes');
el('query').value='nonexistenttoken';el('query').oninput();assert.equal(el('result-count').textContent,'0 / 1 echoes');
assert.ok(el('episode-list').textContent.includes('No echoes match this search.'));
el('query').value='';el('kind').value='research';el('kind').onchange();assert.equal(el('result-count').textContent,'0 / 1 echoes');
el('kind').value='fix';el('signal').value='quality';el('signal').onchange();assert.equal(el('result-count').textContent,'1 / 1 echoes');
el('ledger-tab').onclick();assert.equal(el('result-count').textContent,'1 / 1 claims');
assert.equal(el('kind').disabled,true);assert.equal(el('echoes').hidden,true);assert.equal(el('ledger-tab').attributes['aria-pressed'],'true');
assert.ok(el('claim-list').textContent.includes(input.payload.claims[0].quotes[0].quote));
el('confidence').value='weak';el('confidence').onchange();assert.equal(el('result-count').textContent,'0 / 1 claims');
el('confidence').value='strong';el('confidence').onchange();assert.equal(el('result-count').textContent,'1 / 1 claims');
el('echo-tab').onclick();assert.equal(el('kind').disabled,false);assert.equal(el('result-count').textContent,'1 / 1 echoes');
assert.equal(created.includes('img'),false);console.log('Atlas DOM contract: PASS');
nodes.clear();input.payload.episode_split_id='different-split';
document.getElementById('atlas-data').textContent=JSON.stringify(input.payload);
vm.runInNewContext(input.script,{document});assert.ok(el('split-id').textContent.includes('Source splits differ'));
'''
            result = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps({'payload': payload, 'script': script}),
                                    text=True, capture_output=True, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Atlas DOM contract: PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
