"""Read-only Git observations. Retention and reverts are proxies, not endorsements."""
import datetime as dt
import hashlib
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


class GitTraceError(ValueError):
    pass


def _git(root, *args, required=True, timeout=60):
    try:
        run = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitTraceError('read-only Git inspection failed') from exc
    if required and run.returncode:
        raise GitTraceError('Git inspection refused: %s' % args[0])
    return run.stdout.decode('utf-8', errors='replace').strip('\r\n') if run.returncode == 0 else ''


def canonical_remote(value):
    """Remove credentials and query parameters before using a remote as identity."""
    value = (value or '').strip()
    if '://' in value:
        parsed = urlsplit(value)
        if not parsed.hostname:
            return None
        host, path = parsed.hostname.casefold(), parsed.path.strip('/')
    else:
        match = re.fullmatch(r'(?:[^@\s]+@)?([^:/\s]+):([^\s]+)', value)
        if not match:
            return None
        host, path = match[1].casefold(), match[2].strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    if not host or not path:
        return None
    return host + '/' + path


def discover_repositories(directory, depth=3):
    """Find checkouts without walking their dependencies, then deduplicate worktrees."""
    directory = Path(directory).expanduser().resolve()
    if type(depth) is not int or not 0 <= depth <= 6:
        raise GitTraceError('repository search depth must be0to6')
    found, queue = {}, [(directory, 0)]
    while queue:
        path, level = queue.pop()
        if (path / '.git').exists():
            common = _git(path, 'rev-parse', '--git-common-dir', required=False)
            if common:
                key = str((path / common).resolve()) if not Path(common).is_absolute() else str(Path(common).resolve())
                found.setdefault(key, path)
            continue
        if level >= depth:
            continue
        try:
            children = list(path.iterdir())
        except OSError:
            continue
        queue.extend((p, level + 1) for p in children if p.is_dir() and not p.is_symlink()
                     and not p.name.startswith('.') and p.name not in ('node_modules','work','venv','dist','build'))
    return sorted(found.values(), key=str)


def _utc(epoch):
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def inspect_repository(root, before=None, include_stats=True):
    """Capture immutable commit metadata and reachability at this snapshot."""
    root = Path(root).expanduser().resolve()
    top = Path(_git(root, 'rev-parse', '--show-toplevel')).resolve()
    raw_remote = _git(top, 'config', '--get', 'remote.origin.url', required=False)
    remote = canonical_remote(raw_remote)
    # Never persist the original remote: it may contain credentials.
    identity = hashlib.sha256((remote or str(top)).encode()).hexdigest()[:20]
    head = _git(top, 'rev-parse', 'HEAD', required=False)
    branch = _git(top, 'branch', '--show-current', required=False)
    default_ref = _git(top, 'symbolic-ref', 'refs/remotes/origin/HEAD', required=False)
    chosen_ref = default_ref or (head if head else '')
    reachable = set(_git(top, 'rev-list', chosen_ref, required=False).splitlines()) if chosen_ref else set()
    tags = _git(top, 'for-each-ref', '--format=%(objectname) %(refname)', 'refs/tags', required=False).splitlines()
    fmt = '%H%x1f%P%x1f%at%x1f%ct%x1f%an%x1f%ae%x1f%s%x1f%b%x1e'
    args = ['log','--all','--format='+fmt]
    if before:
        args.append('--before='+before)
    raw = _git(top, *args, required=False, timeout=180)
    commits = []
    from .scrub import scrub
    for block in raw.split('\x1e'):
        block = block.strip('\r\n')
        if not block:
            continue
        fields = block.split('\x1f', 7)
        if len(fields) != 8 or not re.fullmatch(r'[a-f0-9]{40,64}', fields[0]):
            raise GitTraceError('unexpected Git metadata record')
        sha, parents, authored, committed, author, email, subject, body = fields
        text = subject + '\n' + body
        reverts = re.findall(r'This reverts commit ([a-f0-9]{7,64})', text, re.I)
        row = {'id':sha,'parents':parents.split(),'authored_at':_utc(authored),
               'committed_at':_utc(committed),'author_hash':hashlib.sha256((author+'\0'+email).encode()).hexdigest()[:16],
               'subject':scrub(subject)[0][:1000], 'body':scrub(body)[0][:4000],
               'project':identity,'source_ref':'git:'+identity+':'+sha,
               'reachable_at_snapshot':sha in reachable,'reachability_ref':chosen_ref,
               'explicit_revert_targets':reverts,
               'notice':'Observed Git outcome; neither authorship nor reachability proves human endorsement or deployment.'}
        commits.append(row)
    if include_stats and commits:
        # One walk of history, not one process per commit.
        stats_args = ['log','--all','--format=COMMIT:%H','--numstat','--no-renames']
        if before:
            stats_args.append('--before='+before)
        stats = _git(top,*stats_args,required=False,timeout=180)
        by_id = {c['id']:c for c in commits}
        current = None
        for line in stats.splitlines():
            if line.startswith('COMMIT:'):
                current = by_id.get(line[7:])
                if current is not None:
                    current['changes']={'files':0,'added':0,'deleted':0,'binary':0,'extensions':{}}
                continue
            if current is None or not line.strip():
                continue
            parts = line.split('\t', 2)
            if len(parts) != 3:
                continue
            added, deleted, filename = parts
            changes = current['changes'];changes['files'] += 1
            if added == '-' or deleted == '-':
                changes['binary'] += 1
            else:
                changes['added'] += int(added);changes['deleted'] += int(deleted)
            ext = Path(filename).suffix.casefold() or '(none)'
            changes['extensions'][ext] = changes['extensions'].get(ext,0)+1
    targets = {target for c in commits for target in c['explicit_revert_targets']}
    for commit in commits:
        commit['explicitly_reverted_by_observed_history'] = any(commit['id'].startswith(t) for t in targets)
    return {'version':1,'project':identity,'checkout':str(top),'head':head,'branch':branch,
            'remote_identity':remote,'reachability_ref':chosen_ref,'tag_refs':tags,
            'commits':commits,'counts':{'commits':len(commits),'explicit_reverts':sum(bool(c['explicit_revert_targets']) for c in commits)},
            'scope':'Read-only current snapshot; use committed_at as an availability bound, not future knowledge in a prefix.'}


def observations_before(repository, timestamp):
    """Causal metadata view. Future retention/revert outcomes are never prefix features."""
    def aware(value):
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (AttributeError, ValueError):
            raise GitTraceError('timestamp must be aware ISO text') from None
        if parsed.tzinfo is None:
            raise GitTraceError('timestamp needs a timezone')
        return parsed
    bound = aware(timestamp)
    fields=('id','project','source_ref','committed_at','subject','changes')
    return [{k:row[k] for k in fields if k in row} for row in repository['commits']
            if aware(row['committed_at']) <= bound]
