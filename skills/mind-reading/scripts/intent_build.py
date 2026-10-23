#!/usr/bin/env python3
"""Explicit private study builder. No live activation, installs or hosted inference."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--home", required=True, help="new private study directory, never a live alias")
    sub = p.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--source", action="append", required=True, help="claude=PATH, codex=PATH, kimi=PATH, grok=PATH or memory=PATH")
    ingest.add_argument("--machine", default="local")
    bundle = sub.add_parser("import-bundle")
    bundle.add_argument("--file", required=True)
    bundle.add_argument("--parser-sha256", required=True)
    git = sub.add_parser("git")
    git.add_argument("--root", action="append", required=True)
    sub.add_parser("prepare")
    encode = sub.add_parser("encode")
    encode.add_argument("--encoder-path", required=True)
    encode.add_argument("--model", required=True)
    encode.add_argument("--revision", required=True)
    encode.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
    encode.add_argument("--max-length", type=int, default=256)
    train = sub.add_parser("train")
    train.add_argument("--no-adapters", action="store_true")
    train.add_argument("--candidate-count", type=int, default=8)
    sub.add_parser("seal")
    sub.add_parser("evaluate")
    refit = sub.add_parser("refit", help="after evaluation, refit the fixed recipe on all eligible observations")
    refit.add_argument("--output", required=True, help="new candidate directory, never the evaluated candidate")
    refit.add_argument("--tape", help="optional nondefault private tape path")
    return p


def _log(value):
    print(json.dumps(value) if isinstance(value, dict) else str(value), file=sys.stderr, flush=True)


def main(argv=None):
    args = parser().parse_args(argv)
    home = Path(args.home).expanduser()
    if home.is_symlink():
        raise ValueError("a study home must be its own directory, not a live symlink")
    if args.command == "ingest":
        from mr.trace_build import ingest_sources
        specs = []
        for value in args.source:
            source, sep, path = value.partition("=")
            if not sep or source not in ("claude", "codex", "kimi", "grok", "memory") or not path:
                raise ValueError("source must be kind=PATH")
            specs.append({"source": source, "path": str(Path(path).expanduser()), "machine": args.machine})
        result = ingest_sources(home, specs, log=_log)
        roots = [spec["path"] for spec in specs if spec["source"] == "claude"]
        if roots:
            from mr.trace_legacy_agents import ingest
            result["agent_evidence"] = ingest(home, roots, args.machine, log=_log)
        return result
    if args.command == "import-bundle":
        from mr.trace_store import connect, import_bundle
        db = connect(home/"tape.sqlite")
        try:
            return import_bundle(db, args.file, args.parser_sha256)
        finally:
            db.close()
    if args.command == "git":
        from mr.trace_git import discover_repositories, inspect_repository
        out = home/"git"
        out.mkdir(parents=True, exist_ok=True)
        repos = []
        for base in args.root:
            for root in discover_repositories(Path(base).expanduser()):
                repo = inspect_repository(root)
                name = repo["project"]+"-"+(repo["head"] or "empty")[:12]+".json"
                (out/name).write_text(json.dumps(repo, ensure_ascii=False), encoding="utf-8")
                repos.append({"project": repo["project"], "counts": repo["counts"]})
        return {"repositories": repos}
    if args.command == "prepare":
        from mr.trace_build import freeze_research
        out = freeze_research(home)
        protocol = {"version": "trace-protocol-1", "task": "next observed human turn after a 2..60 word human seed",
                    "fences": "family-end clock quantiles; straddlers excluded", "candidate_count": 8,
                    "selection": "validation family macro top1, then family macro MRR", "candidate_pool": "partition only, seed-matched other families",
                    "endorsement": "not measured", "test": "one sealed look, no refitting"}
        (home/"research"/"protocol.json").write_text(json.dumps(protocol, indent=2)+"\n")
        return out
    if args.command == "encode":
        from mr.trace_build import encode_partitions
        device = args.device
        if device == "auto":
            import torch
            device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
        return encode_partitions(home, {"path": str(Path(args.encoder_path).expanduser().resolve()), "model": args.model,
                                       "revision": args.revision, "max_length": args.max_length}, device=device, log=_log)
    if args.command == "refit":
        from mr.trace_refit import prepare_refit_data, refit_candidate
        examples, vectors, _ = prepare_refit_data(home, tape=args.tape, log=_log)
        return refit_candidate(home, examples, vectors, args.output, log=_log)
    from mr.trace_experiment import train_experiment, seal_experiment, evaluate_experiment
    if args.command == "train":
        return train_experiment(home, adapters=not args.no_adapters, candidate_count=args.candidate_count, log=_log)
    if args.command == "seal":
        return seal_experiment(home)
    return evaluate_experiment(home, log=_log)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (ValueError, OSError, ImportError, RuntimeError) as exc:
        print("intent-build: "+str(exc), file=sys.stderr)
        sys.exit(2)
