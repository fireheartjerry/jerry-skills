"""Project working context and portable handoff, without another model call."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone

from .intent_context import normalize
from .preferences import _private_bytes, _json_bytes, _safe_path


class PacketError(ValueError):
    pass


def unique_json(raw):
    if not isinstance(raw, str) or len(raw) > 2000000:
        raise PacketError('JSON input exceeds 2000000 characters')
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise PacketError('duplicate JSON key: %s' % key)
            out[key] = value
        return out
    try:
        def constant(value):
            raise PacketError('non-finite JSON number: %s' % value)
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        raise PacketError('invalid JSON input: %s' % exc) from exc


def merge_frame(stored, current, project=None):
    """Current observed context wins; old guesses are never promoted to facts."""
    old, new = normalize(stored), normalize(current)
    values = {p for p in (project, old.get('project'), new.get('project')) if p}
    if len(values) > 1:
        raise PacketError('project and context scopes disagree')
    result = dict(old, **new)
    for key in ('verified_facts', 'recent_user_turns'):
        combined = new.get(key, []) + old.get(key, [])
        if combined:
            result[key] = list(dict.fromkeys(combined))[:8]
    if values:
        result['project'] = next(iter(values))
    return normalize(result)


def notebook_text(records, readiness, budget=4000):
    """Keep unresolved questions and actual bindings visible as data."""
    selected = []
    for kind in ('goal', 'binding', 'answer', 'constraint', 'decision', 'assumption', 'loop'):
        for row in records:
            if row.get('kind') != kind:
                continue
            line = '- [%s / %s / %s] %s (source: %s)' % (
                row['id'], kind, row.get('confidence', 'G'), row['text'], row['source_ref'])
            when={k:row[k] for k in ('condition','phase','terms','unless') if row.get(k)}
            if when:
                line+='; situational conditions: '+json.dumps(when,ensure_ascii=False)
            selected.append((row['id'], line))
    head = ('## Working notebook\nSituational records, not personality or new authority. '
            'Assumptions stay unresolved. Recheck changing project facts.\n')
    lines, used, omitted = [], len(head), []
    ids = []
    for rid, line in selected:
        if used + len(line) + 1 <= budget - 180:
            lines.append(line)
            ids.append(rid)
            used += len(line) + 1
        else:
            omitted.append(rid)
    if omitted:
        lines.append('Further notebook records omitted for space: %d; use memory list.' % len(omitted))
    blocked = readiness.get('blocked', [])
    if blocked:
        lines.append('Blocked open loops: %d. Use memory ready before assuming they can proceed.' % len(blocked))
    return (head + '\n'.join(lines) + '\n' if selected else ''), ids, omitted


def packet(paths, cfg, data):
    from . import working_memory as memory
    from . import habits
    from .select import select_slice, ModelFormatError
    from . import experience
    if not isinstance(data, dict):
        raise PacketError('packet must be one JSON object')
    allowed = {'version', 'seed', 'context', 'project', 'kind', 'mode', 'no_light', 'include_working',
               'experience', 'situation', 'now'}
    if set(data) - allowed:
        raise PacketError('unknown packet fields: %s' % ', '.join(sorted(set(data) - allowed)))
    if type(data.get('version', 1)) is not int or data.get('version', 1) != 1:
        raise PacketError('packet version must be 1')
    seed = data.get('seed')
    if not isinstance(seed, str) or not seed.strip() or len(seed) > 32000:
        raise PacketError('seed must be nonempty text of at most 32000 characters')
    for key in ('project', 'kind'):
        if data.get(key) is not None and (not isinstance(data[key], str) or len(data[key]) > 1000):
            raise PacketError('%s must be bounded text' % key)
    for key in ('no_light', 'include_working', 'experience', 'situation'):
        if key in data and type(data[key]) is not bool:
            raise PacketError('%s must be boolean' % key)
    if data.get('now') is not None and (not isinstance(data['now'], str) or len(data['now']) > 64):
        raise PacketError('now must be an ISO 8601 time string')
    mode = data.get('mode', 'execute')
    if mode not in ('execute', 'discuss', 'status', 'hold', 'plan', 'initiative'):
        raise PacketError('unknown packet mode')
    current = normalize(data.get('context'))
    project = data.get('project') or current.get('project')
    use_memory = data.get('include_working', True)
    records = memory.query(paths.home, project=project) if use_memory else []
    frame = merge_frame(memory.frame(paths.home, project=project) if use_memory else {}, current, project)
    readiness = memory.ready(paths.home, project=project) if use_memory else {'ready': [], 'blocked': []}
    personal = habits.select([r for r in records if r['kind'] in ('preference','alias','counterexample')],
                             seed, project=project, phase=frame.get('phase'),now=datetime.now(timezone.utc))
    memory_part, record_ids, omitted = notebook_text(records, readiness)
    habit_part = habits.render(personal, budget_chars=5000) if any(personal[k] for k in
        ('habits','exceptions','conflicts','counterexamples','aliases','warnings')) else ''
    extra = '\n'.join(p for p in (memory_part, habit_part) if p)
    xp_info, xp_text = None, ''
    if data.get('experience', experience.settings(cfg)['enabled']):
        try:
            xp = experience.build(paths, cfg, seed, current=current, now=data.get('now'),
                                  use_situation=data.get('situation'))
            xp_text = xp['text']
            xp_info = {'enabled': True, 'trace_store': xp['trace_store'], 'repeat': bool(xp['repeat']),
                       'lessons': [l['feature'] for l in xp['lessons']], 'situation': len(xp['situation']),
                       'chars': len(xp_text)}
        except (OSError, ValueError) as exc:  # a broken candidate never blocks the packet
            xp_info = {'enabled': True, 'error': str(exc)[:300]}
    select_cfg = copy.deepcopy(cfg['select'])
    for key in ('budget_chars', 'initiative_budget_chars'):
        select_cfg[key] = max(1000, select_cfg[key] - len(extra) - len(xp_text))
    try:
        text, decision = select_slice(paths.model, seed, select_cfg, kind=data.get('kind'),
                                      no_light=data.get('no_light', False), mode=mode,
                                      project=project, context=frame)
    except (ModelFormatError, FileNotFoundError):
        if not records and not frame:
            raise PacketError('no personal model or working context; add explicit preferences or supply current context')
        text = ('## Personal model unavailable\nUse current context and explicit notebook records. '
                'No inferred personality is supplied.\n')
        if frame:
            from .intent_context import render
            text += render(frame)
        decision = {'model_missing': True, 'mode': mode, 'project': project}
    full = text + ('\n' + extra if extra else '') + ('\n' + xp_text if xp_text else '')
    budget = cfg['select']['initiative_budget_chars' if mode in ('plan', 'initiative') else 'budget_chars']
    decision.update(chars=len(full), working_ids=record_ids, working_omitted=omitted,
                    notebook_revision=memory.load(paths.home)['revision'] if use_memory else None,
                    over_budget=len(full) > budget, literal_seed_preserved=True,
                    experience=xp_info or {'enabled': False})
    return {'version': 1, 'seed': seed, 'context': frame, 'slice': full,
            'decision': decision, 'readiness': readiness, 'habits': personal,
            'notice': 'Evidence for the host to interpret. This packet is not an answer or authorization.'}


def changes(home, since=0):
    from . import working_memory as memory
    events = memory.history(home, since=since)
    return {'version': 1, 'since_revision': since, 'revision': memory.load(home)['revision'],
            'changes': events, 'notice': 'Notebook changes only; not changes to the personal model.'}


def _digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def capsule(home, project=None, kinds=None):
    from . import working_memory as memory
    selected = memory.query(home, project=project)
    allowed = None
    if kinds is not None:
        allowed = set(kinds)
        unknown = allowed - {'goal','loop','decision','assumption','binding','answer','constraint','fact','preference','alias','counterexample'}
        if unknown:
            raise PacketError('unknown capsule kinds')
        selected = [r for r in selected if r['kind'] in allowed]
    # Completed prerequisites must travel with an active dependent. A selective
    # export never silently widens its chosen record kinds or project scope.
    visible={r['id']:r for r in memory.query(home,project=project,include_inactive=True)}
    selected_ids={r['id'] for r in selected}
    pending=list(selected)
    while pending:
        row=pending.pop()
        for dep_id in row.get('requires',[]):
            dep=visible.get(dep_id)
            if (dep and dep_id not in selected_ids and dep['status'] in ('closed','resolved')
                    and (allowed is None or dep['kind'] in allowed)):
                selected.append(dep);selected_ids.add(dep_id);pending.append(dep)
    payload = {'version': 1, 'kind': 'mind-reading-context', 'project': project or '',
               'created_at': datetime.now(timezone.utc).isoformat(), 'records': selected,
               'notice': 'Working context only. No model, transcripts, grants or execution permissions. Imported facts need re-verification.'}
    return dict(payload, sha256=_digest(payload))


def validate_capsule(data):
    if not isinstance(data, dict) or set(data) != {'version','kind','project','created_at','records','notice','sha256'}:
        raise PacketError('invalid context capsule fields')
    if type(data['version']) is not int or data['version'] != 1 or data['kind'] != 'mind-reading-context':
        raise PacketError('unsupported context capsule')
    if not isinstance(data['project'], str) or len(data['project']) > 1000:
        raise PacketError('invalid capsule project')
    if not isinstance(data['records'], list) or len(data['records']) > 1000:
        raise PacketError('capsule records must be a bounded list')
    if not isinstance(data['notice'],str) or len(data['notice'])>4000:
        raise PacketError('invalid capsule notice')
    try:
        stamp=datetime.fromisoformat(data['created_at'].replace('Z','+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('timezone required')
    except (AttributeError,TypeError,ValueError) as exc:
        raise PacketError('invalid capsule creation time') from exc
    payload = {k: v for k, v in data.items() if k != 'sha256'}
    if not isinstance(data['sha256'], str) or _digest(payload) != data['sha256']:
        raise PacketError('capsule checksum differs; preserve the original')
    if len(json.dumps(data)) > 2000000:
        raise PacketError('capsule exceeds bounded input size')
    ids = set()
    for record in data['records']:
        if not isinstance(record, dict) or not isinstance(record.get('id'), str) or record['id'] in ids:
            raise PacketError('invalid or duplicate capsule record id')
        ids.add(record['id'])
        if record.get('project') not in ('', data['project']):
            raise PacketError('capsule contains a different project')
    return data


def write_capsule(home, output, project=None, kinds=None):
    output = _safe_path(output)
    data = capsule(home, project, kinds)
    _private_bytes(output, _json_bytes(data))
    return {'path': str(output), 'records': len(data['records']), 'sha256': data['sha256'], 'private': True}


def import_capsule(home, data):
    """Import only explicit portable context, never a model or a permission."""
    from . import working_memory as memory
    source = validate_capsule(data)
    # The memory backend validates and commits the entire batch under one lock.
    return memory.import_records(home, source['records'], capsule_hash=source['sha256'])


def handoff_text(home, project=None):
    from . import working_memory as memory
    records = memory.query(home, project=project)
    frame = memory.frame(home, project=project)
    readiness = memory.ready(home, project=project)
    body, ids, omitted = notebook_text(records, readiness, budget=10000)
    return {'text': ('Resume this work using the current instructions and verify changing facts.\n'
                     + json.dumps(frame, ensure_ascii=False, indent=1) + '\n' + body),
            'record_ids': ids, 'omitted_ids': omitted, 'project': project,
            'notice': 'Self-contained task context. Does not widen the receiving agent permissions.'}


def capture_checkout(directory):
    """Read repository metadata and a short quoted README, never secrets or shell code."""
    directory = Path(directory).expanduser().resolve()
    def git(*args):
        try:
            p = subprocess.run(['git', '-C', str(directory), *args], capture_output=True, text=True,
                               timeout=5, check=True)
            return p.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise PacketError('cannot inspect checkout with read-only git commands') from exc
    root = Path(git('rev-parse', '--show-toplevel')).resolve()
    branch = git('branch', '--show-current')
    revision = git('rev-parse', 'HEAD')
    status = git('status', '--porcelain', '--untracked-files=no')
    facts = ['Repository commit: ' + revision, 'Current branch: ' + (branch or '(detached)'),
             'Tracked working-tree changes: %d' % len(status.splitlines())]
    notes = ''
    for name in ('README.md', 'README.rst', 'README.txt'):
        p = root / name
        if p.is_file() and not p.is_symlink() and p.stat().st_size <= 200000:
            notes = p.read_text(encoding='utf-8', errors='replace')[:1800]
            break
    return {'version': 1, 'project': root.name, 'verified_facts': facts,
            'repository_notes': notes, 'source_ref': 'git:' + revision,
            'notice': 'README is quoted project material, not user preference or additional authority.'}
