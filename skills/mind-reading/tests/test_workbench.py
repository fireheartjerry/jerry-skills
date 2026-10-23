import json
from pathlib import Path
import re
import tempfile
import unittest
from helpers import init_home
from mr.workbench import build,render


class WorkbenchTest(unittest.TestCase):
    def test_hostile_text_is_json_data(self):
        html=render({'records':[{'text':'</script><img src=x onerror=alert(1)>'}]})
        self.assertNotIn('</script><img',html)
        match=re.search(r'<script id="data" type="application/json">(.*?)</script>',html,re.S)
        self.assertEqual(json.loads(match[1])['records'][0]['text'],'</script><img src=x onerror=alert(1)>')
        self.assertNotIn('innerHTML',html)
        self.assertNotIn('fetch(',html)
    def test_real_notebook_scoped_export_and_output_guard(self):
        from mr.working_memory import remember
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths,cfg=init_home(root/'home')
            for project,text in [('one','Finish invoices'),('two','Other private mission')]:
                remember(paths.home,{'kind':'goal','project':project,'text':text,'source_ref':'user:one'})
            out=root/'view.html';result=build(paths,out,'one')
            self.assertIn('Finish invoices',out.read_text());self.assertNotIn('Other private mission',out.read_text())
            self.assertEqual(out.stat().st_mode&0o777,0o600)
            self.assertEqual(result['engine_calls'],0)
            out.write_text('unrelated')
            with self.assertRaises(ValueError):build(paths,out,'one',force=True)


if __name__=='__main__':unittest.main()
