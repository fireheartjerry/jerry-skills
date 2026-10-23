"""Small CLI surface for situational memory; never changes the personal model."""
import json
import sys
from datetime import datetime,timezone


def _input():
    from .context_tools import unique_json
    return unique_json(sys.stdin.read(2000001))


def command(args, paths, cfg, log):
    from . import working_memory as memory
    from . import habits
    from . import context_tools as tools
    try:
        if args.cmd == 'packet':
            result = tools.packet(paths, cfg, _input())
            if not args.json:
                print(result['slice'], end='')
                return 0
        elif args.cmd == 'inspect':
            result = tools.capture_checkout(args.path)
        elif args.cmd == 'insights':
            from .insights import discover
            result = discover(paths.model, args.seed, args.project, args.limit)
        elif args.cmd == 'workbench':
            from .workbench import build
            result = build(paths, args.out, args.project, force=args.force)
        elif args.cmd == 'handoff':
            if args.action == 'export':
                kinds = args.kinds.split(',') if args.kinds else None
                result = tools.write_capsule(paths.home, args.out, args.project, kinds) if args.out else tools.capsule(paths.home,args.project,kinds)
            elif args.action == 'import':
                result = tools.import_capsule(paths.home, _input())
            else:
                result = tools.handoff_text(paths.home,args.project)
        elif args.cmd == 'memory':
            action = args.action
            if action == 'add':
                data = _input()
                if not isinstance(data,dict):
                    raise ValueError('memory add requires one record object')
                if args.project is not None:
                    if data.get('project',args.project) != args.project:
                        raise ValueError('record and --project disagree')
                    data['project']=args.project
                result = memory.remember(paths.home,data)
            elif action == 'list':
                result = {'records':memory.query(paths.home,args.project,include_inactive=args.all),
                          'revision':memory.load(paths.home)['revision']}
            elif action in ('close','forget','restore'):
                if not args.id:
                    raise ValueError('this action requires --id')
                result = memory.change(paths.home,args.id,{'close':'closed','forget':'forgotten','restore':'active'}[action])
            elif action == 'resolve':
                data = _input()
                if not args.id or not isinstance(data,dict) or set(data) != {'text','source_ref'}:
                    raise ValueError('resolve needs --id and JSON text/source_ref')
                result = memory.resolve(paths.home,args.id,data['text'],data['source_ref'])
            elif action == 'ready':
                result = memory.ready(paths.home,args.project)
            elif action == 'frame':
                result = memory.frame(paths.home,args.project)
            elif action == 'history':
                result = {'history':memory.history(paths.home,since=args.since)}
            elif action == 'diff':
                result = tools.changes(paths.home,since=args.since)
            elif action == 'explain':
                if not args.id:
                    raise ValueError('explain requires --id')
                records=memory.query(paths.home,args.project,include_inactive=True)
                row=next((r for r in records if r['id']==args.id),None)
                if row and row['kind'] not in ('preference','alias','counterexample'):
                    explanatory=dict(row)
                    if 'value' in explanatory and not isinstance(explanatory['value'],str):
                        explanatory['value']=json.dumps(explanatory['value'],ensure_ascii=False)
                    result=habits.explain([explanatory],args.id,seed=args.seed,project=args.project,
                                          phase=args.phase,now=datetime.now(timezone.utc))
                    if result['outcome'] not in ('forgotten','missing'):
                        result['record']=row
                    result['reasons'].append('Situational context; changing facts require verification.')
                else:
                    result=habits.explain(records,args.id,seed=args.seed,project=args.project,phase=args.phase,now=datetime.now(timezone.utc))
            elif action == 'habits':
                result=habits.select(memory.query(paths.home,args.project),args.seed,project=args.project,phase=args.phase,now=datetime.now(timezone.utc))
            else:
                raise ValueError('unknown notebook action')
        else:
            raise ValueError('unknown working-context command')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log('%s: %s' % (args.cmd,exc))
        if args.json:
            print(json.dumps({'error':str(exc)},ensure_ascii=False))
        return 4
    print(json.dumps(result,ensure_ascii=False,default=str))
    return 0


def register(sub):
    p=sub.add_parser('memory',help='remember goals, decisions, open loops and scoped situational facts')
    p.add_argument('action',choices=['add','list','close','forget','restore','resolve','ready','frame','history','diff','explain','habits'])
    p.add_argument('--project')
    p.add_argument('--id')
    p.add_argument('--all',action='store_true',help='include inactive records within the selected scope')
    p.add_argument('--since',type=int,default=0)
    p.add_argument('--seed',default='')
    p.add_argument('--phase')
    p.set_defaults(func=command)
    p=sub.add_parser('packet',help='read one JSON seed/context packet from stdin; reuse scoped notebook automatically')
    p.set_defaults(func=command)
    p=sub.add_parser('handoff',help='portable, selective working-context capsule or readable resume brief')
    p.add_argument('action',choices=['export','import','text'])
    p.add_argument('--project')
    p.add_argument('--kinds',help='comma-separated record kinds to export')
    p.add_argument('--out')
    p.set_defaults(func=command)
    p=sub.add_parser('inspect',help='capture read-only checkout facts and a quoted README for current context')
    p.add_argument('--path',default='.')
    p.set_defaults(func=command)
    p=sub.add_parser('insights',help='find same-prompt ambiguity and cross-project workflow analogies in training history')
    p.add_argument('--seed',default='')
    p.add_argument('--project')
    p.add_argument('--limit',type=int,default=4)
    p.set_defaults(func=command)
    p=sub.add_parser('workbench',help='export a private offline explorer of working context and its evidence')
    p.add_argument('--project')
    p.add_argument('--out',required=True)
    p.add_argument('--force',action='store_true')
    p.set_defaults(func=command)
