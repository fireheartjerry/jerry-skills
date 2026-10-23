"""An offline, inspectable notebook, generated from actual records."""
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path
from .preferences import _private_bytes, _safe_path

MARKER='<!-- mind-reading-workbench:v1 -->'


def payload(paths, project=None):
    from . import working_memory as memory
    from . import habits
    from .context_tools import handoff_text
    rows=memory.query(paths.home,project,include_inactive=True)
    state=memory.load(paths.home)
    return {'version':1,'project':project or '', 'revision':state['revision'],'records':rows,
            'frame':memory.frame(paths.home,project),'readiness':memory.ready(paths.home,project),
            'habits':habits.select(memory.query(paths.home,project),'',project=project,now=datetime.now(timezone.utc)),
            'handoff':handoff_text(paths.home,project)['text'],
            'notice':'Private offline snapshot. These records inform the agent; no predictions are simulated here.'}


def render(data):
    encoded=json.dumps(data,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    return PAGE.replace('__DATA__',encoded)


def build(paths, output, project=None, force=False):
    target=_safe_path(output)
    if target.suffix.lower() != '.html':
        raise ValueError('workbench output must be .html')
    text=render(payload(paths,project))
    if target.exists() and target.read_text()!=text:
        if not force or MARKER not in target.read_text()[:300]:
            raise ValueError('output exists; choose a new file or force only a generated workbench')
    _private_bytes(target,text.encode(),replace=force)
    return {'path':str(target),'private':True,'engine_calls':0,'sha256':hashlib.sha256(text.encode()).hexdigest()}


PAGE=r'''<!doctype html>
<!-- mind-reading-workbench:v1 -->
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline';script-src 'unsafe-inline';connect-src 'none'">
<title>What carries forward</title><style>
:root{color-scheme:light;--paper:#f7f5ef;--ink:#212820;--muted:#596553;--line:#bfc5b9;--accent:#a04321}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.55 system-ui,sans-serif}.wrap{max-width:1280px;margin:auto;padding:26px 5vw 60px}header{border-bottom:1px solid var(--ink);padding-bottom:40px}h1{font:400 clamp(40px,7vw,88px)/1.02 Georgia,serif;letter-spacing:-.045em;margin:34px 0 20px}h2{font:400 30px/1.2 Georgia,serif}h3{font:600 17px/1.4 system-ui,sans-serif;margin:0}.meta{font:12px/1.6 ui-monospace,monospace;color:var(--muted);overflow-wrap:anywhere}p{max-width:80ch}button,input,select{font:inherit;min-height:44px;color:inherit;border:1px solid var(--muted);background:transparent;border-radius:0;padding:9px 12px}button{cursor:pointer}button:hover{background:#e9ebe2}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid var(--accent);outline-offset:3px}.tools{display:grid;grid-template-columns:2fr 1fr auto;gap:16px;padding:26px 0;border-bottom:1px solid var(--line)}label{display:block;font-size:13px}label input,label select{display:block;width:100%;margin-top:6px}.layout{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(260px,1fr);gap:5vw}.row{border-bottom:1px solid var(--line);padding:22px 0}.row button{text-align:left;display:block;width:100%;border:0;padding:0;min-height:44px}.row p{margin:9px 0}.row p,.detail,.goal{white-space:pre-wrap;overflow-wrap:anywhere}.goal{font:25px/1.45 Georgia,serif}.tag{font:11px/1.5 ui-monospace,monospace;text-transform:uppercase;letter-spacing:.05em;color:var(--accent)}aside{min-width:0}.detail{padding:18px 0;border-top:2px solid var(--ink);font-size:15px}.detail strong{display:block;margin-top:18px}.quiet{color:var(--muted)}.empty{padding:30px 0;color:var(--muted)}.handoff{border-top:1px solid var(--ink);margin-top:40px;padding-top:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.7 ui-monospace,monospace;max-height:380px;overflow:auto}.status{min-height:28px;font-size:13px;color:var(--muted)}a{color:inherit}.skip{position:absolute;left:10px;top:-80px;background:white;padding:10px}.skip:focus{top:10px}[hidden]{display:none!important}@media(max-width:720px){.layout{grid-template-columns:1fr}.tools{grid-template-columns:1fr}.wrap{padding:20px 22px 40px}header{padding-bottom:22px}.goal{font-size:23px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
</style></head><body><a class="skip" href="#records">Skip to memory</a><div class="wrap">
<header><div class="meta">Mind reading / Working context / Offline snapshot</div><h1>What carries<br>forward.</h1><p class="quiet">The mission, the loose ends, and the decisions you should never have to repeat. Select any memory to see its source and status.</p><p id="scope" class="meta"></p><div id="goal" class="goal"></div></header>
<div class="tools"><label>Find a memory<input id="search" type="search" placeholder="Goal, decision, assumption…"></label><label>Kind<select id="kind"><option value="">All kinds</option></select></label><label>Visibility<select id="visibility"><option value="active">Active</option><option value="all">Include history</option></select></label></div>
<div class="layout"><main id="records" aria-label="Working memory"><p id="count" class="meta" aria-live="polite"></p><div id="list"></div></main><aside><h2>Why this is here</h2><div id="detail" class="detail"><p class="quiet">Choose a memory. Sources explain where it came from; they do not grant permission.</p></div><h2>What can move</h2><div id="ready"></div><div id="conflicts"></div></aside></div>
<section class="handoff"><h2>Pick up where you left off.</h2><p class="quiet">A portable brief for the next session. Copying it does not send it anywhere.</p><button id="copy">Copy handoff</button><p id="status" class="status" aria-live="polite"></p><pre id="handoff"></pre></section><footer class="meta">This page reads its embedded snapshot only. Refresh the CLI export to see newer changes. Unresolved assumptions remain guesses.</footer>
</div><script id="data" type="application/json">__DATA__</script><script>
'use strict';const data=JSON.parse(document.getElementById('data').textContent);const byId=id=>document.getElementById(id);const make=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
byId('scope').textContent=(data.project||'Global context')+' · Revision '+data.revision;byId('goal').textContent=data.frame.goal||'No active mission recorded.';byId('handoff').textContent=data.handoff;
for(const kind of [...new Set(data.records.map(r=>r.kind))].sort()){const n=make('option',kind);n.value=kind;byId('kind').append(n);}
function active(r){return r.status==='active'&&(!r.expires_at||Date.parse(r.expires_at)>Date.now());}
function detail(r){const box=byId('detail');box.replaceChildren(make('h3',r.text));for(const [label,value]of [['Kind',r.kind],['Status',r.status],['Source',r.source_ref],['Scope',r.project||'global'],['Confidence',r.confidence],['Created',r.created_at],['Expires',r.expires_at],['Conditions',r.condition],['Unless',r.unless],['Depends on',r.requires],['Resolution',r.resolution]]){if(value!==undefined&&value!==null&&value!==''){box.append(make('strong',label),make('div',typeof value==='object'?JSON.stringify(value,null,1):String(value)));}}box.append(make('p','An observation, decision or hypothesis in context. Verify changing facts.','quiet'));}
function render(){const q=byId('search').value.toLocaleLowerCase(),kind=byId('kind').value,all=byId('visibility').value==='all';const rows=data.records.filter(r=>(all||active(r))&&(!kind||r.kind===kind)&&(!q||[r.text,r.source_ref,r.key,r.value].join(' ').toLocaleLowerCase().includes(q)));byId('count').textContent=rows.length+' memories shown';const list=byId('list');list.replaceChildren();for(const r of rows){const section=make('section','','row');section.append(make('div',r.kind+' / '+r.status,'tag'));const b=make('button',r.text);b.onclick=()=>detail(r);section.append(b,make('p',r.source_ref,'meta'));list.append(section);}if(!rows.length)list.append(make('p','No memories match this view.','empty'));}
function readyLabel(r){return typeof r==='string'?r:(r.text||r.id||(r.record&&r.record.text)||JSON.stringify(r));}byId('ready').append(make('p',data.readiness.ready.length+' ready · '+data.readiness.blocked.length+' waiting','meta'));for(const r of data.readiness.ready)byId('ready').append(make('p',readyLabel(r)));for(const r of data.readiness.blocked)byId('ready').append(make('p',readyLabel(r),'quiet'));
if(data.habits.conflicts.length){byId('conflicts').append(make('h2','Preferences in tension'));for(const c of data.habits.conflicts)byId('conflicts').append(make('pre',JSON.stringify(c,null,1)));}
byId('search').oninput=render;byId('kind').onchange=render;byId('visibility').onchange=render;byId('copy').onclick=async()=>{try{await navigator.clipboard.writeText(data.handoff);byId('status').textContent='Copied.';}catch(e){byId('status').textContent='Copy is unavailable here. Select the brief below.';}};render();
</script></body></html>'''
