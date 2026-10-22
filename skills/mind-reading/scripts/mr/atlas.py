"""Private, offline intent atlas built only from source-linked model sidecars."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .config import read_json

MARKER = '<!-- mind-reading-atlas:v1 -->'


class AtlasError(ValueError):
    pass


def _load(path):
    try:
        data = read_json(path, {})
    except (OSError, ValueError) as exc:
        raise AtlasError('cannot read model sidecar: %s' % Path(path).name) from exc
    if not isinstance(data, dict):
        raise AtlasError('model sidecar must be an object: %s' % Path(path).name)
    return data


def _text(value):
    return value if isinstance(value, str) else ''


def _strings(value):
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _payload(paths):
    evidence = _load(paths.model / 'evidence.json')
    store = _load(paths.model / 'episodes.json')
    episodes = []
    for e in store.get('episodes', []) if isinstance(store.get('episodes', []), list) else []:
        if (not isinstance(e, dict) or not _text(e.get('seed')).strip() or not _text(e.get('addition')).strip()
                or len(_strings(e.get('source_ids'))) < 2):
            continue
        episodes.append({k: _text(e.get(k)) for k in ('id', 'seed', 'addition', 'time', 'session_id', 'project')})
        episodes[-1].update({k: _strings(e.get(k)) for k in ('signals', 'kinds', 'source_ids')})
    claims = []
    for item in evidence.get('items', []) if isinstance(evidence.get('items', []), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            continue
        quotes = []
        for quote in item.get('evidence', []) if isinstance(item.get('evidence', []), list) else []:
            if (isinstance(quote, dict) and quote.get('origin', 'typed') == 'typed'
                    and _text(quote.get('source_id')).strip() and _text(quote.get('quote')).strip()):
                quotes.append({k: _text(quote.get(k)) for k in ('quote', 'source_id', 'time', 'source', 'session', 'project')})
        claims.append({'id': _text(item.get('id')), 'kind': _text(item.get('kind')), 'text': item['text'],
                       'confidence': _text(item.get('confidence')) or 'unknown',
                       'evidence_class': _text(item.get('evidence_class')) or 'unknown',
                       'session_count': _count(item.get('session_count')), 'project_count': _count(item.get('project_count')),
                       'source_ids': _strings(item.get('source_ids')), 'quotes': quotes})
    training = evidence.get('training') if isinstance(evidence.get('training'), dict) else {}
    sessions = {e['session_id'] for e in episodes if e['session_id']}
    typed_ids = {quote['source_id'] for item in claims for quote in item['quotes']}
    return {'version': 1, 'episodes': episodes, 'claims': claims,
            'split_id': _text(store.get('split_id')) or _text(training.get('split_id')),
            'episode_split_id': _text(store.get('split_id')), 'evidence_split_id': _text(training.get('split_id')),
            'counts': {'episodes': len(episodes), 'episode_sessions': len(sessions), 'claims': len(claims),
                       'typed_quotes': len(typed_ids), 'strong_claims': sum(c['confidence'] == 'strong' for c in claims)}}


def render_atlas(payload):
    """JSON is inert data; every transcript string reaches the DOM via textContent."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).replace('<', '\\u003c').replace('>', '\\u003e')
    encoded = encoded.replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    return _PAGE.replace('__PAYLOAD__', encoded)


def build_atlas(paths, out_path, force=False):
    """Export only. A changed atlas needs force; unrelated files are never replaced."""
    output = Path(out_path).expanduser()
    if output.suffix.lower() != '.html':
        raise AtlasError('atlas output must be an .html file')
    if output.is_symlink():
        raise AtlasError('atlas output cannot be a symlink')
    if output.exists() and not output.is_file():
        raise AtlasError('atlas output is not a regular file')
    payload = _payload(paths)
    document = render_atlas(payload)
    if output.exists():
        try:
            existing = output.read_text(encoding='utf-8')
        except (OSError, ValueError) as exc:
            raise AtlasError('existing atlas output is unreadable; choose another file') from exc
        if existing == document:
            return dict(payload['counts'], path=str(output.resolve()), rebuilt=False, engine_calls=0,
                        private=True, evidence_hash=hashlib.sha256(document.encode()).hexdigest()[:16])
        if not force or MARKER not in existing[:300]:
            raise AtlasError('output exists; choose a new file or use --force for a previously generated atlas')
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                         prefix='.intent-atlas-', suffix='.tmp', delete=False) as fh:
            temp_name = fh.name
            os.chmod(temp_name, 0o600)
            fh.write(document)
        os.replace(temp_name, output)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return dict(payload['counts'], path=str(output.resolve()), rebuilt=True, engine_calls=0,
                private=True, evidence_hash=hashlib.sha256(document.encode()).hexdigest()[:16])


_PAGE = r'''<!doctype html>
<!-- mind-reading-atlas:v1 -->
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src data:">
<title>What you left unsaid · Intent atlas</title>
<style>
:root{--paper:#f5f1e8;--ink:#20231f;--muted:#5e625a;--line:#c9c9bb;--accent:#a32d1c;--soft:#ebe6d9;--serif:'Iowan Old Style','Baskerville',Georgia,serif;--sans:'Avenir Next','Trebuchet MS',sans-serif;--mono:'SFMono-Regular',Menlo,Consolas,monospace}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.55 var(--sans)}button,input,select{font:inherit;color:inherit}button,a,input,select,summary{touch-action:manipulation}button,select,summary{cursor:pointer}button{border:0;background:none}a{color:inherit;text-underline-offset:4px}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:4px}::selection{background:#e7b99e;color:#151815}.skip{position:absolute;top:-60px;left:16px;background:var(--ink);color:var(--paper);padding:10px 18px;z-index:20}.skip:focus{top:10px}.wrap{max-width:1440px;margin:auto;padding:0 5vw}.mast{display:flex;justify-content:space-between;gap:20px;align-items:center;padding:23px 0;border-bottom:1px solid var(--ink);font:11px/1.5 var(--mono);letter-spacing:.12em;text-transform:uppercase}.private{color:var(--accent)}.hero{display:grid;grid-template-columns:1.55fr 1fr;gap:8vw;padding:64px 0 44px;align-items:end}.eyebrow,.label{font:11px/1.5 var(--mono);letter-spacing:.12em;text-transform:uppercase}.eyebrow{color:var(--accent);margin:0 0 22px}.hero h1{font:400 clamp(54px,7.5vw,114px)/.95 var(--serif);letter-spacing:-.055em;margin:0;max-width:780px}.hero h1 em{font-style:italic;color:var(--accent)}.intro{margin:0 0 6px;max-width:390px;font-size:19px}.intro small{display:block;font-size:13px;color:var(--muted);margin-top:20px}.stats{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);margin:0;padding:23px 0 25px;gap:24px}.stats div{display:flex;flex-direction:column-reverse;gap:9px}.stats dt{font:11px/1.5 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}.stats dd{font:400 42px/1 var(--serif);margin:0}.toolbar{padding:32px 0 26px;display:grid;grid-template-columns:1.55fr 1fr;gap:8vw}.search label{display:block;font-size:14px;font-weight:600;margin-bottom:8px}.search input{display:block;width:100%;background:none;border:0;border-bottom:1px solid var(--ink);padding:12px 0;font-size:22px;border-radius:0}.search input::placeholder{color:#75786f}.hint{font-size:12px;color:var(--muted);margin:10px 0 0}.filters{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-content:end}.filters label{display:block;font:11px/1.5 var(--mono);text-transform:uppercase;letter-spacing:.05em}.filters select{display:block;width:100%;border:0;border-bottom:1px solid var(--ink);border-radius:0;padding:13px 20px 13px 0;background:none;font-size:15px;margin-top:4px}.nav{display:flex;gap:30px;align-items:center;border-bottom:1px solid var(--ink);position:sticky;top:0;background:var(--paper);z-index:3;min-height:58px}.tab{font-size:14px;padding:17px 0 14px;border-bottom:3px solid transparent;min-height:54px}.tab[aria-pressed=true]{border-color:var(--accent);color:var(--accent);font-weight:700}.count{margin-left:auto;text-align:right;font:11px/1.5 var(--mono);color:var(--muted)}.heading{display:grid;grid-template-columns:48px 1fr 34px 1.35fr;gap:22px;padding:24px 0 12px;border-bottom:1px solid var(--line);color:var(--muted)}.heading .label:first-child{grid-column:2}.heading .label:last-child{grid-column:4}.episode{padding:26px 0 21px;border-bottom:1px solid var(--line)}.pair{display:grid;grid-template-columns:48px 1fr 34px 1.35fr;gap:22px;align-items:start}.number{font:11px/1.5 var(--mono);color:var(--muted);padding-top:5px}.seed{font:400 28px/1.2 var(--serif);letter-spacing:-.02em;margin:0;overflow-wrap:anywhere;white-space:pre-wrap}.arrow{font:25px/1.2 var(--serif);color:var(--accent);padding-top:2px}.addition{font-size:16px;line-height:1.65;margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.meta{margin:18px 0 0 70px;display:flex;align-items:center;flex-wrap:wrap;gap:9px 22px;font:10px/1.6 var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.025em}.signal{color:var(--accent)}details{margin:14px 0 0 70px}summary{font:12px/1.7 var(--mono);color:var(--muted);padding:4px 0;min-height:30px}.provenance{font:11px/1.7 var(--mono);overflow-wrap:anywhere;white-space:pre-wrap;margin:10px 0;color:var(--muted)}.empty{padding:80px 0;max-width:590px}.empty h2{font:400 40px/1.1 var(--serif);letter-spacing:-.025em;margin:0 0 18px}.empty p{color:var(--muted)}.claim{display:grid;grid-template-columns:190px 1fr;gap:42px;padding:28px 0;border-bottom:1px solid var(--line)}.confidence{font:12px/1.6 var(--mono);text-transform:uppercase}.confidence strong{display:block;color:var(--accent);font-weight:500}.confidence small{display:block;color:var(--muted);font-size:11px;text-transform:none;margin-top:8px}.claim h3{font:400 25px/1.35 var(--serif);margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.claim details{margin-left:0}.quote{border-left:2px solid var(--accent);padding-left:20px;margin:22px 0}.quote blockquote{font:18px/1.55 var(--serif);margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.quote cite{display:block;font:10px/1.7 var(--mono);font-style:normal;overflow-wrap:anywhere;margin-top:8px;color:var(--muted)}.ledger-head{display:flex;justify-content:space-between;gap:28px;align-items:baseline;padding:28px 0 8px}.ledger-head p{font-size:13px;color:var(--muted);max-width:680px;margin:0}.ledger-head label{font:11px/1.6 var(--mono)}.ledger-head select{margin-left:12px;padding:9px;border:1px solid var(--line);border-radius:0;background:none;font-size:13px;min-height:42px}footer{display:grid;grid-template-columns:1.55fr 1fr;gap:8vw;padding:35px 0 45px;border-top:1px solid var(--ink);margin-top:54px;font-size:12px;color:var(--muted)}footer p{margin:0}footer strong{color:var(--ink)}.split{font:10px/1.7 var(--mono);margin-top:14px;overflow-wrap:anywhere}[hidden]{display:none!important}
@media(min-width:1600px){.wrap{padding:0 76px}}@media(max-width:800px){.wrap{padding:0 24px}.hero{grid-template-columns:1fr;gap:30px;padding:42px 0 30px}.hero h1{font-size:72px}.intro{font-size:17px;max-width:550px}.intro small{margin-top:12px}.stats{gap:15px}.stats dd{font-size:34px}.stats dt{font-size:9px}.toolbar{grid-template-columns:1fr;gap:22px}.pair,.heading{grid-template-columns:26px 1fr 24px 1.1fr;gap:14px}.seed{font-size:25px}.addition{font-size:15px}.meta,details{margin-left:40px}.claim{grid-template-columns:130px 1fr;gap:20px}footer{grid-template-columns:1fr;gap:18px}}
@media(max-width:540px){.mast{font-size:9px;letter-spacing:.08em;gap:10px}.hero h1{font-size:60px}.stats{grid-template-columns:1fr 1fr;row-gap:25px}.stats dd{font-size:38px}.search input{font-size:20px}.nav{gap:0 20px;flex-wrap:wrap}.tab{font-size:13px}.count{font-size:9px;max-width:none;flex-basis:100%;text-align:left;padding-bottom:10px}.heading{display:none}.pair{grid-template-columns:28px 1fr;gap:14px}.number{grid-column:1}.seed{grid-column:2;font-size:29px}.arrow{grid-column:2;font-size:23px;line-height:.7;transform:rotate(90deg);transform-origin:12px 12px;justify-self:start}.addition{grid-column:2;font-size:15px}.meta,details{margin-left:42px}.episode{padding:28px 0}.claim{grid-template-columns:1fr;gap:13px}.confidence{display:flex;flex-wrap:wrap;gap:6px 16px}.confidence small{margin:0;align-self:center}.claim h3{font-size:24px}.ledger-head{display:block}.ledger-head label{display:block;margin-top:15px}.filters{gap:20px}.filters select{font-size:14px}.quote{margin-left:0}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{transition:none!important}}
</style></head><body><a class="skip" href="#explorer">Skip to evidence</a><div class="wrap">
<header class="mast"><span>Mind reading / Field notes</span><span class="private">Private · Offline · Yours</span></header>
<section class="hero" aria-labelledby="title"><div><p class="eyebrow">An atlas of the missing brief</p><h1 id="title">What you<br>left <em>unsaid.</em></h1></div><p class="intro">The short request.<br>The words that came after.<small>Explore what you actually had to add, with the original evidence close at hand. Historical words are precedents, not instructions for today.</small></p></section>
<dl class="stats"><div><dt>Intent echoes</dt><dd id="stat-episodes">0</dd></div><div><dt>Independent echo sessions</dt><dd id="stat-sessions">0</dd></div><div><dt>Model claims</dt><dd id="stat-claims">0</dd></div><div><dt>Verified typed sources</dt><dd id="stat-quotes">0</dd></div></dl>
<main id="explorer"><div class="toolbar"><div class="search"><label for="query">Find a familiar request</label><input id="query" type="search" placeholder="A word, a task, a thing you keep repeating…" autocomplete="off" aria-describedby="search-note"><p class="hint" id="search-note">Local word matching, not a prediction. Clear the search to see everything.</p></div><div class="filters"><label for="kind">Request kind<select id="kind"><option value="">Every kind</option></select></label><label for="signal">What came after<select id="signal"><option value="">Every signal</option></select></label></div></div>
<nav class="nav" aria-label="Evidence view"><button class="tab" id="echo-tab" aria-pressed="true" aria-controls="echoes">Intent echoes</button><button class="tab" id="ledger-tab" aria-pressed="false" aria-controls="ledger">Evidence ledger</button><span class="count" id="result-count" aria-live="polite"></span></nav>
<section id="echoes" aria-labelledby="echo-tab"><div class="heading" aria-hidden="true"><span class="label">You asked</span><span class="label">Later you added</span></div><div id="episode-list"></div></section>
<section id="ledger" aria-labelledby="ledger-tab" hidden><div class="ledger-head"><p>Confidence comes from the model's verified evidence sidecar. A strong label is an evidence category, not a measured probability of a correct read.</p><label for="confidence">Confidence<select id="confidence"><option value="">Every level</option><option>strong</option><option>moderate</option><option>weak</option><option>unknown</option></select></label></div><div id="claim-list"></div></section></main>
<footer><div><p><strong>Exact words. Visible limits.</strong> Echoes come from prepared training history. A later addition can reflect a changed task; adjacency alone does not prove an omitted requirement. The ledger preserves confidence and source references so you can judge it yourself.</p><p class="split" id="split-id"></p></div><p>This file contains private model evidence. It has no external resources, network calls or analytics. Nothing here activates a model, changes permissions or supplies authorization. Your latest words and current project context decide what happens next.</p></footer>
</div><script type="application/json" id="atlas-data">__PAYLOAD__</script><script>
'use strict';
const data=JSON.parse(document.getElementById('atlas-data').textContent),el=id=>document.getElementById(id);
const stop=new Set('a an the and or to for of in on with i me my you your it this that is are be do can could please just all then have want need like make get use'.split(' '));
const words=text=>new Set((String(text||'').toLowerCase().match(/[a-z0-9][a-z0-9'+-]*/g)||[]).filter(w=>!stop.has(w)));
const text=(tag,value,cls)=>{const node=document.createElement(tag);if(cls)node.className=cls;node.textContent=value;return node;};
const addOptions=(id,values)=>{for(const value of [...new Set(values)].sort()){const option=document.createElement('option');option.value=value;option.textContent=value.replaceAll('_',' ');el(id).append(option);}};
addOptions('kind',data.episodes.flatMap(e=>e.kinds));addOptions('signal',data.episodes.flatMap(e=>e.signals));
el('stat-episodes').textContent=data.counts.episodes.toLocaleString();el('stat-sessions').textContent=data.counts.episode_sessions.toLocaleString();el('stat-claims').textContent=data.counts.claims.toLocaleString();el('stat-quotes').textContent=data.counts.typed_quotes.toLocaleString();el('split-id').textContent=data.episode_split_id&&data.evidence_split_id&&data.episode_split_id!==data.evidence_split_id?'Source splits differ · echoes '+data.episode_split_id+' · claims '+data.evidence_split_id+' · review before comparing':data.split_id?'Training split · '+data.split_id:'Training split · not recorded';
let view='echoes';
function matching(value,query){if(!query.trim())return 0;const queryWords=words(query),tokens=words(value);let score=0;for(const word of queryWords)if(tokens.has(word))score++;if(queryWords.size&&String(value).toLowerCase()===query.toLowerCase().trim())score+=3;return score;}
function empty(container,title,body){const box=text('div','','empty');box.append(text('h2',title),text('p',body));container.append(box);}
function renderEpisodes(){const query=el('query').value.trim(),kind=el('kind').value,signal=el('signal').value,container=el('episode-list');container.replaceChildren();let rows=data.episodes.map((episode,index)=>({episode,index,score:matching(episode.seed+' '+episode.addition,query)})).filter(({episode,score})=>(!kind||episode.kinds.includes(kind))&&(!signal||episode.signals.includes(signal))&&(!query||score>0));if(query)rows.sort((a,b)=>b.score-a.score||a.index-b.index);
if(!rows.length)empty(container,data.episodes.length?'No echoes match this search.':'No intent echoes yet.',data.episodes.length?'Clear the search or change a filter. Your history is not being rewritten to fill the gap.':'Run recall build after preparing a training split. This view stays empty until source-linked episodes exist.');
for(const {episode:e,index} of rows){const article=text('article','','episode'),pair=text('div','','pair');pair.append(text('span',String(index+1).padStart(3,'0'),'number'),text('h2',e.seed,'seed'));const arrow=text('span','→','arrow');arrow.setAttribute('aria-hidden','true');pair.append(arrow,text('p',e.addition,'addition'));article.append(pair);const meta=text('div','','meta');meta.append(text('span',e.signals.join(' / ')||'No classified signal','signal'),text('span',e.kinds.join(' / ')||'Unclassified request'),text('span','One historical episode'),text('span',e.time?e.time.slice(0,10):'Date not recorded'));article.append(meta);const details=document.createElement('details');details.append(text('summary','Source references · '+e.source_ids.length));details.append(text('p',['Echo '+e.id,e.session_id?'Session '+e.session_id:'',e.project?'Historical project '+e.project:'',...e.source_ids].filter(Boolean).join('\n'),'provenance'));article.append(details);container.append(article);}return rows.length;}
function renderClaims(){const query=el('query').value.trim(),confidence=el('confidence').value,container=el('claim-list');container.replaceChildren();let rows=data.claims.map((claim,index)=>({claim,index,score:matching(claim.text+' '+claim.quotes.map(q=>q.quote).join(' '),query)})).filter(({claim,score})=>(!confidence||claim.confidence===confidence)&&(!query||score>0));if(query)rows.sort((a,b)=>b.score-a.score||a.index-b.index);
if(!rows.length)empty(container,data.claims.length?'No claims match this search.':'The evidence ledger is empty.',data.claims.length?'Clear the search or change the confidence filter.':'Build a candidate with verified evidence. This atlas does not infer missing claims.');
for(const {claim:c} of rows){const article=text('article','','claim'),meta=text('div','','confidence');meta.append(text('strong',c.confidence),text('span',c.evidence_class.replaceAll('_',' ')),text('small',c.session_count+' independent session'+(c.session_count===1?'':'s')+' · '+c.project_count+' project'+(c.project_count===1?'':'s')));const body=document.createElement('div');body.append(text('h3',c.text));const details=document.createElement('details');details.append(text('summary','Inspect original words · '+c.quotes.length+' typed quote'+(c.quotes.length===1?'':'s')));for(const quote of c.quotes){const block=text('div','','quote');block.append(text('blockquote',quote.quote),text('cite',[quote.source_id,quote.time?quote.time.slice(0,10):'',quote.project?'Historical project '+quote.project:''].filter(Boolean).join(' · ')));details.append(block);}if(!c.quotes.length)details.append(text('p','No verified typed quote in this sidecar item.','provenance'));details.append(text('p',['Claim '+c.id,'Kind '+c.kind,...c.source_ids].join('\n'),'provenance'));body.append(details);article.append(meta,body);container.append(article);}return rows.length;}
function render(){const count=view==='echoes'?renderEpisodes():renderClaims(),total=view==='echoes'?data.episodes.length:data.claims.length;el('result-count').textContent=count+' / '+total+' '+(view==='echoes'?'echoes':'claims');}
function switchView(next){view=next;el('echoes').hidden=view!=='echoes';el('ledger').hidden=view!=='ledger';el('echo-tab').setAttribute('aria-pressed',view==='echoes');el('ledger-tab').setAttribute('aria-pressed',view==='ledger');el('kind').disabled=view!=='echoes';el('signal').disabled=view!=='echoes';render();}
el('echo-tab').onclick=()=>switchView('echoes');el('ledger-tab').onclick=()=>switchView('ledger');el('query').oninput=render;el('kind').onchange=render;el('signal').onchange=render;el('confidence').onchange=render;render();
</script></body></html>'''
