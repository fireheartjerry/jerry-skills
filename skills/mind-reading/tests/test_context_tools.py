import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from helpers import init_home, CLI, run_cli
from mr.context_tools import (PacketError, unique_json, merge_frame, packet, capsule,
                              validate_capsule, write_capsule, import_capsule,
                              handoff_text, capture_checkout, changes)


class ContextToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.paths,self.cfg=init_home(self.root/'home')

    def tearDown(self):
        self.tmp.cleanup()

    def remember(self,kind,text,**kw):
        from mr.working_memory import remember
        return remember(self.paths.home,dict(kind=kind,text=text,project='project-one',source_ref='user:turn-1',**kw))

    def test_unique_json_nested_duplicate_rejected(self):
        for raw in ('{"seed":"a","seed":"b"}','{"a":{"x":1,"x":2}}','{"bad":NaN}'):
            with self.assertRaises(PacketError):unique_json(raw)
        self.assertEqual(unique_json('{"a":[1,2]}'),{'a':[1,2]})

    def test_candidate_refresh_preserves_notebook_snapshot(self):
        from mr.refresh import prepare_candidate
        self.remember('goal','Finish the current task')
        clone,cfg,state=prepare_candidate(self.paths,self.cfg,self.root/'candidate')
        self.assertEqual((clone.home/'context/notebook.json').read_bytes(),
                         (self.paths.home/'context/notebook.json').read_bytes())
        self.assertEqual(state['status'],'building')
        self.assertFalse(clone.home.samefile(self.paths.home))

    def test_current_frame_wins_and_keeps_order(self):
        result=merge_frame({'goal':'old','verified_facts':['old fact','shared']},
                           {'goal':'new','verified_facts':['new fact','shared']},'project-one')
        self.assertEqual(result['goal'],'new')
        self.assertEqual(result['verified_facts'],['new fact','shared','old fact'])
        with self.assertRaises(PacketError):merge_frame({'project':'one'},{'project':'two'})

    def test_packet_preserves_literal_and_uses_notebook_without_persona(self):
        self.remember('goal','Finish the invoice download',confidence='S')
        self.remember('fact','CSV uses the shared parser',confidence='S')
        self.remember('assumption','Maybe the app is ready',confidence='G')
        result=packet(self.paths,self.cfg,{'seed':'ready rq?','project':'project-one','no_light':True})
        self.assertEqual(result['seed'],'ready rq?')
        self.assertEqual(result['context']['goal'],'Finish the invoice download')
        self.assertTrue(result['decision']['model_missing'])
        self.assertIn('Maybe the app is ready',result['slice'])
        self.assertNotIn('Maybe the app is ready',' '.join(result['context'].get('verified_facts',[])))

    def test_packet_newest_goal_and_project_isolation(self):
        self.remember('goal','Do the old task',confidence='S')
        result=packet(self.paths,self.cfg,{'seed':'go','project':'other','context':{'goal':'Current task'}})
        self.assertNotIn('Do the old task',result['slice'])
        result=packet(self.paths,self.cfg,{'seed':'go','project':'project-one','context':{'goal':'New current task'}})
        self.assertEqual(result['context']['goal'],'New current task')

    def test_packet_unknown_future_fields_and_input_types(self):
        for data in ({'seed':'go','later_turns':['secret']},{'seed':123},{'seed':'go','no_light':'yes'},
                     {'seed':'go','context':{'future':'no'}},{'version':True,'seed':'go'}):
            with self.assertRaises(ValueError):packet(self.paths,self.cfg,data)
        self.assertFalse((self.paths.home/'context/notebook.json').exists())

    def test_cold_start_explicit_preferences(self):
        self.remember('preference','Prefer a concise result first',key='report',value='concise',confidence='S')
        result=packet(self.paths,self.cfg,{'seed':'write the report','project':'project-one'})
        self.assertIn('Prefer a concise result first',result['slice'])
        self.assertFalse(self.paths.profile.exists())

    def test_conditional_habits_counterexamples_and_expiry_survive_notebook(self):
        pref=self.remember('preference','Give the full derivation',key='detail',value='full',
                           condition={'phase':'research'},expires_at='2099-01-01T00:00:00Z',confidence='S')
        self.remember('counterexample','Routine updates should still be brief',counter_to=[pref['id']],confidence='S')
        result=packet(self.paths,self.cfg,{'seed':'explain','project':'project-one','context':{'phase':'research'}})
        self.assertEqual(len(result['habits']['habits']),1)
        self.assertEqual(len(result['habits']['counterexamples']),1)

    def test_recurring_pattern_is_not_a_verified_project_fact(self):
        self.remember('fact','Usually the service is online',confidence='L')
        result=packet(self.paths,self.cfg,{'seed':'ready','project':'project-one'})
        self.assertNotIn('Usually the service is online',' '.join(result['context'].get('verified_facts',[])))

    def test_unrelated_counterexample_does_not_escape_its_condition(self):
        pref=self.remember('preference','Use detailed analysis',key='detail',value='deep',
                           condition={'phase':'research'},confidence='S')
        self.remember('counterexample','A very specific unrelated objection',counter_to=[pref['id']],confidence='S')
        result=packet(self.paths,self.cfg,{'seed':'build','project':'project-one','context':{'phase':'implementation'}})
        self.assertNotIn('A very specific unrelated objection',result['slice'])

    def test_capsule_keeps_closed_prerequisites_and_import_remaps_ids(self):
        from mr.working_memory import change,ready,query
        dep=self.remember('loop','Run regression checks',id='check')
        change(self.paths.home,dep['id'],'closed')
        self.remember('loop','Prepare the bundle',requires=['check'])
        data=capsule(self.paths.home,'project-one')
        self.assertEqual(len(data['records']),2)
        other=self.root/'portable';import_capsule(other,data)
        self.assertEqual(len(ready(other,'project-one')['ready']),1)
        self.assertTrue(all(r['id']!='check' for r in query(other,'project-one',include_inactive=True)))

    def test_import_never_replaces_an_active_mission(self):
        self.remember('goal','Transferred task')
        from mr.working_memory import remember,load
        other=self.root/'busy'
        remember(other,{'kind':'goal','text':'Current task','project':'project-one','source_ref':'user:new'})
        before=load(other)
        with self.assertRaises(ValueError):import_capsule(other,capsule(self.paths.home,'project-one'))
        self.assertEqual(load(other),before)

    def test_capsule_selective_export_and_tamper_rejection(self):
        self.remember('goal','Finish download')
        self.remember('assumption','Need to check mobile')
        selected=capsule(self.paths.home,'project-one',['goal'])
        self.assertEqual([r['kind'] for r in selected['records']],['goal'])
        self.assertEqual(validate_capsule(selected),selected)
        changed=copy.deepcopy(selected);changed['records'][0]['text']='replaced'
        with self.assertRaises(PacketError):validate_capsule(changed)
        with self.assertRaises(PacketError):capsule(self.paths.home,'project-one',['grants'])

    def test_capsule_private_file_no_overwrite(self):
        self.remember('goal','Finish download')
        dest=self.root/'handoff.json'
        result=write_capsule(self.paths.home,dest,'project-one')
        self.assertEqual(dest.stat().st_mode&0o777,0o600)
        self.assertEqual(result['records'],1)
        dest.write_text('unrelated')
        with self.assertRaises(ValueError):write_capsule(self.paths.home,dest,'project-one')

    def test_capsule_import_is_atomic_and_idempotent(self):
        self.remember('goal','Finish download')
        self.remember('fact','Old checkout was tested',confidence='S')
        data=capsule(self.paths.home,'project-one')
        other=self.root/'other'
        result=import_capsule(other,data)
        from mr.working_memory import query,load
        self.assertEqual(len(query(other,'project-one')),2)
        facts=[r for r in query(other,'project-one') if r['kind']=='fact']
        self.assertTrue(all(r['confidence']=='G' for r in facts))
        rev=load(other)['revision'];import_capsule(other,data)
        self.assertEqual(load(other)['revision'],rev)
        malformed=copy.deepcopy(data);malformed['records'].append({'id':'bad'})
        from mr.context_tools import _digest
        malformed['sha256']=_digest({k:v for k,v in malformed.items() if k!='sha256'})
        with self.assertRaises(ValueError):import_capsule(other,malformed)
        self.assertEqual(load(other)['revision'],rev)

    def test_handoff_and_changes_keep_unresolved_status(self):
        row=self.remember('assumption','Maybe needs approval')
        result=handoff_text(self.paths.home,'project-one')
        self.assertIn('assumption',result['text'])
        self.assertIn(row['source_ref'],result['text'])
        change=changes(self.paths.home,0)
        self.assertEqual(change['revision'],1)
        self.assertTrue(change['changes'])

    def test_inspect_checkout_is_read_only(self):
        root=self.root/'checkout';root.mkdir()
        subprocess.run(['git','init','-q','-b','test-context',str(root)],check=True)
        subprocess.run(['git','-C',str(root),'-c','user.name=Example','-c','user.email=example@example.invalid','commit','--allow-empty','-qm','initial'],check=True)
        (root/'README.md').write_text('A quoted example project.')
        before=subprocess.check_output(['git','-C',str(root),'status','--porcelain'])
        result=capture_checkout(root)
        self.assertEqual(result['project'],'checkout')
        self.assertIn('test-context',' '.join(result['verified_facts']))
        self.assertIn('A quoted example',result['repository_notes'])
        self.assertEqual(before,subprocess.check_output(['git','-C',str(root),'status','--porcelain']))

    def test_cli_packet_one_stdin_and_memory_no_model(self):
        import sys
        data={'kind':'goal','text':'Make the export usable','project':'project-one','source_ref':'user:one'}
        p=subprocess.run([sys.executable,'-I',str(CLI),'--home',str(self.paths.home),'--json','memory','add'],input=json.dumps(data),capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        p=subprocess.run([sys.executable,'-I',str(CLI),'--home',str(self.paths.home),'--json','packet'],input=json.dumps({'seed':'finish it','project':'project-one'}),capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertEqual(json.loads(p.stdout)['context']['goal'],'Make the export usable')

    def test_explain_context_record_honors_expiry_and_conditions(self):
        import sys
        expired=self.remember('fact','Previously available',confidence='S',expires_at='2000-01-01T00:00:00Z')
        p=subprocess.run([sys.executable,'-I',str(CLI),'--home',str(self.paths.home),'--json',
                          'memory','explain','--id',expired['id'],'--project','project-one'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertFalse(json.loads(p.stdout)['applies'])


if __name__=='__main__':unittest.main()
