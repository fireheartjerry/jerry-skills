import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from helpers import SCRIPTS
from mr.trace_git import canonical_remote,discover_repositories,inspect_repository,observations_before


class GitObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'repo';self.root.mkdir()
        self.git('init','-q','-b','example')
        self.git('config','user.name','Fictional Author');self.git('config','user.email','example@example.invalid')
    def tearDown(self):self.tmp.cleanup()
    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*args],text=True).strip()
    def commit(self,message):
        self.git('add','.');self.git('commit','-qm',message);return self.git('rev-parse','HEAD')
    def test_remote_credentials_removed(self):
        self.assertEqual(canonical_remote('https://secret:token@example.invalid/team/project.git?auth=private'),'example.invalid/team/project')
        self.assertEqual(canonical_remote('git@example.invalid:team/project.git'),'example.invalid/team/project')
    def test_commit_revert_and_stats_are_observations(self):
        (self.root/'code.py').write_text('print(1)\n');first=self.commit('Add implementation')
        self.git('revert','--no-edit',first)
        before=self.git('status','--porcelain');data=inspect_repository(self.root)
        self.assertEqual(data['counts']['commits'],2)
        original=next(c for c in data['commits'] if c['id']==first)
        self.assertTrue(original['explicitly_reverted_by_observed_history'])
        self.assertEqual(original['changes']['extensions'],{'.py':1})
        self.assertNotIn('example@example.invalid',json.dumps(data))
        self.assertEqual(before,self.git('status','--porcelain'))
    def test_worktree_deduplication(self):
        (self.root/'a').write_text('a');self.commit('first')
        other=Path(self.tmp.name)/'other';self.git('worktree','add','-qb','other',str(other))
        self.assertEqual(len(discover_repositories(Path(self.tmp.name))),1)
    def test_causal_view_drops_retention_outcomes(self):
        repository={'commits':[{'id':'a','project':'p','committed_at':'2026-01-01T00:00:00Z','reachable_at_snapshot':True,'explicitly_reverted_by_observed_history':False},
                               {'id':'b','committed_at':'2026-02-01T00:00:00Z'}]}
        visible=observations_before(repository,'2026-01-15T00:00:00Z')
        self.assertEqual([r['id'] for r in visible],['a'])
        self.assertNotIn('reachable_at_snapshot',visible[0])
        self.assertNotIn('explicitly_reverted_by_observed_history',visible[0])

    def test_iso_fractions_and_offsets_are_compared_as_instants(self):
        repo={'commits':[{'id':'a','committed_at':'2026-01-01T00:00:00Z'}]}
        self.assertEqual(len(observations_before(repo,'2026-01-01T00:00:00.001Z')),1)
        self.assertEqual(len(observations_before(repo,'2025-12-31T19:00:00-05:00')),1)
        with self.assertRaises(ValueError): observations_before(repo,'2026-01-01T00:00:00')


if __name__=='__main__':unittest.main()
