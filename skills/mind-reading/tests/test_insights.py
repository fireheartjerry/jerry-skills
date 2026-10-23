import json
from pathlib import Path
import tempfile
import unittest
from helpers import CLI
from mr.insights import discover


class InsightsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.model=Path(self.tmp.name)
        self.rows=[self.row('one','ready?','Test the actual download too','invoices','s1'),
                   self.row('two','READY','Show the source quotes first','research','s2'),
                   self.row('three','fix invoice export','Check mobile navigation too','invoices','s3'),
                   self.row('four','repair gallery export','Verify actual files, then continue','gallery','s4'),
                   self.row('five','unrelated query','invoice fix only in addition','archive','s5')]
        self.save()
    def tearDown(self):self.tmp.cleanup()
    def row(self,rid,seed,addition,project,session):
        return dict(id=rid,seed=seed,addition=addition,project=project,session_id=session,
                    source_ids=['src:'+rid,'follow:'+rid],signals=['quality','followthrough'],
                    kinds=['fix'],time='2026-01-01T00:00:00Z',before=[])
    def save(self):
        (self.model/'episodes.json').write_text(json.dumps({'version':2,'episodes':self.rows}))
    def test_same_prompt_different_continuations(self):
        found=discover(self.model,'ready')['ambiguity_twins']
        self.assertEqual(len(found),1)
        self.assertEqual(len(found[0]['situations']),2)
        self.assertIn('src:one',str(found))
    def test_bridges_distinct_project_and_session(self):
        found=discover(self.model,'invoice export',project='invoices')['cross_project_bridges']
        self.assertTrue(found)
        self.assertTrue(all(v['anchor']['project']!=v['analogy']['project'] for v in found))
        self.assertTrue(all(v['query_terms'] for v in found))
        self.assertTrue(all(v['anchor']['id']!='five' for v in found))
    def test_later_words_do_not_create_anchor(self):
        self.rows=[self.rows[-1]];self.save()
        self.assertFalse(discover(self.model,'invoice')['cross_project_bridges'])
    def test_bounds_and_empty(self):
        result=discover(self.model,'',limit=0)
        self.assertEqual(result['ambiguity_twins'],[]);self.assertEqual(result['cross_project_bridges'],[])
        with self.assertRaises(ValueError):discover(self.model,limit=True)
        with self.assertRaises(ValueError):discover(self.model,query=123)


if __name__=='__main__':unittest.main()
