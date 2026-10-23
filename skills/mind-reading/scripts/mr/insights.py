"""Historical analogies for creative intent reasoning. No guessed user labels."""
import re
from collections import defaultdict
from .recall import load_episodes, words, kinds


def _card(row):
    return {k: row.get(k, [] if k in ('before','source_ids','signals','kinds') else '')
            for k in ('id','seed','addition','project','time','source_ids','before','signals','kinds')}


def ambiguity_twins(model_dir, query='', limit=4):
    """Show identical short prompts whose observed continuations differed."""
    buckets = defaultdict(list)
    query_words = words(query)
    for row in load_episodes(model_dir):
        key = re.sub(r'[^a-z0-9 ]+', '', row['seed'].casefold())
        key = ' '.join(key.split())
        buckets[key].append(row)
    found = []
    for key, rows in buckets.items():
        unique = []
        for row in rows:
            if any(old['addition'].casefold() == row['addition'].casefold() for old in unique):
                continue
            unique.append(row)
        if len(unique) < 2:
            continue
        match = len(query_words & words(key)) if query_words else 0
        if query_words and not match:
            continue
        selected = sorted(unique, key=lambda r: (r.get('time',''),r['id']), reverse=True)[:3]
        found.append({'seed': selected[0]['seed'], 'situations': [_card(r) for r in selected],
                      'relevance_terms': sorted(query_words & words(key)),
                      'meaning': 'Same words, different observed continuations. Resolve from the current situation, not the phrase alone.',
                      '_score': match})
    found.sort(key=lambda r: (-r['_score'],r['seed'].casefold()))
    for row in found:
        row.pop('_score')
    return found[:max(0,min(20,int(limit)))]


def bridges(model_dir, query, project=None, limit=4):
    """Find cross-project workflow analogies anchored on actual query evidence."""
    query_words, query_kinds = words(query), kinds(query)
    if not query_words:
        return []
    rows = load_episodes(model_dir)
    anchors = []
    for row in rows:
        # Search historical seed and preceding context only. The later addition
        # can explain a lesson, but cannot manufacture a retrieval hit.
        before = '\n'.join(v['text'] for v in row.get('before', []))
        hits = query_words & words(row['seed'] + '\n' + before)
        if hits and (not project or row.get('project') in ('',project)):
            anchors.append((len(hits),row,sorted(hits)))
    anchors.sort(key=lambda a: (-a[0],a[1]['id']))
    result, used = [], set()
    for _, anchor, hits in anchors[:10]:
        candidates = []
        for other in rows:
            if other['id'] == anchor['id'] or other.get('session_id') == anchor.get('session_id'):
                continue
            if not other.get('project') or other.get('project') == anchor.get('project'):
                continue
            shared_kinds = sorted(set(anchor['kinds']) & set(other['kinds']))
            shared_signals = sorted(set(anchor['signals']) & set(other['signals']))
            if not shared_kinds or not shared_signals:
                continue
            score = 2*len(shared_kinds)+len(shared_signals)+len(query_kinds & set(other['kinds']))
            candidates.append((score,other,shared_kinds,shared_signals))
        candidates.sort(key=lambda x: (-x[0],x[1]['id']))
        for _, other, shared_kinds, shared_signals in candidates[:2]:
            identity = tuple(sorted((anchor['id'],other['id'])))
            if identity in used:
                continue
            used.add(identity)
            result.append({'anchor':_card(anchor),'analogy':_card(other),'query_terms':hits,
                           'shared_workflow':shared_kinds,'shared_followup':shared_signals,
                           'use':'Consider the transferable work habit. Historical project details and targets are not current requirements or authorization.'})
            if len(result) >= max(0,min(12,int(limit))):
                return result
    return result


def discover(model_dir, query='', project=None, limit=4):
    if type(limit) is not int or not 0 <= limit <= 20:
        raise ValueError('insight limit must be an integer from0to20')
    if not isinstance(query,str) or len(query)>32000:
        raise ValueError('insight query must be bounded text')
    return {'version':1,'ambiguity_twins':ambiguity_twins(model_dir,query,limit),
            'cross_project_bridges':bridges(model_dir,query,project,limit) if limit else [],
            'notice':'Observed historical episodes for the host to reason about. No inferred wanted labels, new facts or automatic assignments.'}
