"""``swekit``: the command-line interface."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _select(tasks: list, ids: str | None = None, limit: int | None = None, language: str | None = None) -> list:
    if ids:
        wanted = {i.strip() for i in ids.split(",") if i.strip()}
        tasks = [t for t in tasks if t.instance_id in wanted]
    if language:
        tasks = [t for t in tasks if t.language == language]
    return tasks[:limit] if limit else tasks


def _load(path: str, args) -> list:
    from .formats import load_any

    tasks = _select(load_any(path, getattr(args, "format", "auto")), getattr(args, "ids", None),
                    getattr(args, "limit", None), getattr(args, "language", None))
    if getattr(args, "log_parser", None):
        for t in tasks:
            t.env.log_parser = args.log_parser
    return tasks


def _parallel(items: list, fn: Callable[[Any], Any], workers: int) -> Iterable[tuple[Any, Any]]:
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for fut in cf.as_completed(futures):
            yield futures[fut], fut.result()


def _client(args):
    from .llm import ChatClient

    key = os.environ.get(args.api_key_env) if getattr(args, "api_key_env", None) else None
    kw = {k: v for k, v in (("model", args.model), ("base_url", args.base_url), ("api_key", key)) if v}
    return ChatClient(input_price=args.input_price, output_price=args.output_price, **kw)


def _add_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", default="auto", help="input format: auto, swekit, swebench, swebench-pro, generic")
    p.add_argument("--ids", help="comma-separated instance ids to keep")
    p.add_argument("--limit", type=int, help="keep the first N tasks")
    p.add_argument("--language", help="keep tasks of one language")
    p.add_argument("--log-parser", help="override every task's log parser (see swe_env_kit.logparsers)")


def _add_llm(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", help="model name (or SWEKIT_LLM_MODEL)")
    p.add_argument("--base-url", help="OpenAI-compatible base URL (or SWEKIT_LLM_BASE_URL)")
    p.add_argument("--api-key-env", help="environment variable holding the API key (default SWEKIT_LLM_API_KEY / OPENAI_API_KEY)")
    p.add_argument("--input-price", type=float, default=0.0, help="USD per million prompt tokens (for cost budgets)")
    p.add_argument("--output-price", type=float, default=0.0, help="USD per million completion tokens")


# ------------------------------------------------------------------- commands
def cmd_demo(args) -> int:
    from .demo import build_demo_repo

    shas = build_demo_repo(args.dir)
    log(f"demo repository with {len(shas)} commits at {args.dir}")
    print(f"next: swekit mine {args.dir} -o demo-tasks.jsonl")
    return 0


def cmd_fetch(args) -> int:
    from . import hub
    from .formats import from_pro, to_task
    from .task import write_jsonl

    rows = hub.iter_hf_rows(args.dataset, split=args.split, limit=args.limit, offset=args.offset)
    ids = {i.strip() for i in args.ids.split(",")} if args.ids else None
    out = []
    for row in rows:
        if ids and row.get("instance_id") not in ids:
            continue
        if args.raw:
            out.append(row)
            continue
        if args.with_scripts and "requirements" in row:
            task = from_pro(row, hub.fetch_pro_scripts(row["instance_id"]))
        else:
            task = to_task(row)
        out.append(task.to_dict())
    n = write_jsonl(args.output, out)
    log(f"wrote {n} {'rows' if args.raw else 'tasks'} from {hub.resolve_dataset(args.dataset)} to {args.output}")
    return 0


def cmd_convert(args) -> int:
    from .formats import EXPORTERS
    from .task import write_jsonl

    tasks = _load(args.input, args)
    n = write_jsonl(args.output, (EXPORTERS[args.to](t) for t in tasks))
    log(f"converted {n} tasks to {args.to} -> {args.output}")
    return 0


def cmd_mine(args) -> int:
    from .mine import MineConfig, mine_commits, mine_prs
    from .task import save_tasks

    base = MineConfig.pro() if args.pro else MineConfig()
    for k in ("min_lines", "max_lines", "max_tasks", "since", "until", "max_commits"):
        v = getattr(args, k)
        if v is not None:
            setattr(base, k, v)
    if args.branch:
        base.branch = args.branch
    skipped: dict[str, int] = {}

    def skip(_ref: str, why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    if args.mode == "commits":
        tasks = mine_commits(args.source, args.repo, base, on_skip=skip)
    else:
        tasks = mine_prs(args.repo or args.source, base, max_prs=args.max_prs, on_skip=skip)
    save_tasks(args.output, tasks)
    log(f"mined {len(tasks)} candidate tasks -> {args.output}; skipped: {skipped or 'none'}")
    if tasks:
        log(f"next: swekit validate {args.output} -o validated.jsonl --backend docker")
    return 0


def cmd_stats(args) -> int:
    from .hub import GitHub
    from .stats import summarize, task_stats

    tasks = _load(args.tasks, args)
    licenses: dict[str, str | None] = {}
    if args.licenses:
        gh = GitHub()
        for repo in sorted({t.repo for t in tasks}):
            try:
                licenses[repo] = gh.license_spdx(repo)
            except Exception as exc:
                log(f"license lookup failed for {repo}: {exc}")
    per = [task_stats(t, args.min_lines, licenses.get(t.repo)) for t in tasks]
    summary = summarize(per)
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "tasks": [s.to_dict() for s in per]}, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


def cmd_spec(args) -> int:
    from .spec import augment, leak_check
    from .task import save_tasks, write_jsonl

    tasks = _load(args.tasks, args)
    if args.check_only:
        rows = []
        for t in tasks:
            rows.append({"instance_id": t.instance_id, **{f: leak_check(getattr(t, f), t.gold_patch, allow=t.interface).to_dict()
                                                         for f in ("problem_statement", "requirements", "hints")}})
        leaked = [r["instance_id"] for r in rows if any(r[f]["leaked"] for f in ("problem_statement", "requirements", "hints"))]
        if args.report:
            write_jsonl(args.report, rows)
        print(json.dumps({"tasks": len(rows), "leaking": leaked}, indent=2))
        return 1 if leaked else 0
    if not args.output:
        log("spec: -o/--output is required unless --check-only")
        return 2
    client = _client(args) if args.model or os.environ.get("SWEKIT_LLM_MODEL") else None
    reports = []
    for t in tasks:
        _, rep = augment(t, client, overwrite=args.overwrite)
        reports.append({"instance_id": t.instance_id, **rep})
    save_tasks(args.output, tasks)
    if args.report:
        write_jsonl(args.report, reports)
    leaks = sum(1 for t in tasks if t.metadata.get("spec", {}).get("leak"))
    log(f"drafted specs for {len(tasks)} tasks -> {args.output}; {leaks} flagged for leaks. Review before use.")
    return 0


def cmd_env(args) -> int:
    from .dockerfiles import generate_dockerfile
    from .workspace import ensure_image

    tasks = _load(args.tasks, args)
    failed = 0
    for t in tasks:
        if args.dockerfiles:
            out = Path(args.dockerfiles) / t.instance_id
            out.mkdir(parents=True, exist_ok=True)
            (out / "Dockerfile").write_text(t.env.dockerfile or generate_dockerfile(t))
        if args.build:
            try:
                log(f"{t.instance_id}: {ensure_image(t, rebuild=args.rebuild)}")
            except Exception as exc:
                failed += 1
                log(f"{t.instance_id}: build failed: {str(exc)[-800:]}")
    if args.dockerfiles:
        log(f"wrote Dockerfiles under {args.dockerfiles}")
    return 1 if failed else 0


def cmd_validate(args) -> int:
    from .task import save_tasks, write_jsonl
    from .validate import apply_validation, validate

    tasks = _load(args.tasks, args)
    kept, reports, counts = [], [], {"valid": 0, "invalid": 0, "error": 0}

    def one(t):
        return validate(t, args.backend, runs=args.runs, timeout=args.timeout, naming=args.naming)

    for t, res in _parallel(tasks, one, args.workers):
        counts[res.verdict] = counts.get(res.verdict, 0) + 1
        log(f"{t.instance_id}: {res.verdict} f2p={len(res.fail_to_pass)} p2p={len(res.pass_to_pass)} "
            f"{' '.join(res.reasons + res.warnings)}")
        reports.append(res.to_dict())
        if res.verdict == "valid":
            kept.append(apply_validation(t, res))
        elif args.keep_invalid:  # keep the task's existing test lists; only record what happened
            t.metadata["validation"] = {"verdict": res.verdict, "reasons": res.reasons, "warnings": res.warnings}
            kept.append(t)
    order = {t.instance_id: i for i, t in enumerate(tasks)}
    kept.sort(key=lambda t: order[t.instance_id])
    save_tasks(args.output, kept)
    if args.report:
        write_jsonl(args.report, sorted(reports, key=lambda r: order[r["instance_id"]]))
    log(f"{counts} -> {args.output}")
    return 0 if counts.get("valid") else 1


def cmd_grade(args) -> int:
    from .formats import load_predictions
    from .grade import grade
    from .task import write_jsonl

    tasks = _load(args.tasks, args)
    if args.gold:
        preds = {t.instance_id: t.gold_patch for t in tasks}
    elif args.nop:
        preds = {t.instance_id: "" for t in tasks}
    elif args.predictions:
        preds = load_predictions(args.predictions)
    else:
        log("grade: give a predictions file, --gold or --nop")
        return 2
    todo = [t for t in tasks if t.instance_id in preds]
    missing = len(tasks) - len(todo)
    results = []
    for t, r in _parallel(todo, lambda t: grade(t, preds[t.instance_id], args.backend, strict=not args.lenient,
                                                 timeout=args.timeout), args.workers):
        results.append(r.to_dict())
        log(f"{t.instance_id}: {'RESOLVED' if r.resolved else r.failure} dense={r.dense}"
            + (f" flags={','.join(r.flags)}" if r.flags else ""))
    write_jsonl(args.output, results)
    n = len(results)
    resolved = sum(r["resolved"] for r in results)
    print(json.dumps({"graded": n, "resolved": resolved, "resolved_pct": round(100 * resolved / n, 1) if n else 0.0,
                      "mean_dense": round(sum(r["dense"] for r in results) / n, 4) if n else 0.0,
                      "missing_predictions": missing}, indent=2))
    return 0


def _agent_factory(args) -> tuple[Callable, str]:
    from .agents import LLMAgent, NopAgent, OracleAgent, ReplayAgent, load_agent_class

    spec = args.agent
    if spec == "nop":
        return (lambda t: NopAgent(t)), "nop"
    if spec == "oracle":
        return (lambda t: OracleAgent(t)), "oracle"
    if spec.startswith("replay:"):
        root = Path(spec.split(":", 1)[1])

        def replay(t):
            for cand in (root / t.instance_id, root / f"{t.instance_id}.jsonl", root / f"{t.instance_id}.json",
                         root / f"{t.instance_id}.txt", root):
                if cand.exists():
                    return ReplayAgent.from_file(cand)
            raise FileNotFoundError(f"no replay source for {t.instance_id} under {root}")
        return replay, "replay"
    if spec == "llm":
        client_args = args

        def llm(t):
            return LLMAgent(t, _client(client_args))
        return llm, f"llm:{args.model or os.environ.get('SWEKIT_LLM_MODEL', '')}"
    cls = load_agent_class(spec)
    return (lambda t: cls(t)), spec


def cmd_run(args) -> int:
    from .gym import Budget, run_batch

    tasks = _load(args.tasks, args)
    factory, name = _agent_factory(args)
    if args.backend == "local" and args.agent not in ("nop", "oracle") and not args.agent.startswith("replay:"):
        log("warning: --backend local gives the agent your whole machine (and the task file and repo cache, "
            "which contain the answers). Use --backend docker for real agents.")
    budget = Budget(max_turns=args.max_turns, max_cost=args.max_cost, max_seconds=args.max_seconds,
                    command_timeout=args.command_timeout, max_output_chars=args.max_output_chars)
    eps = run_batch(tasks, factory, args.backend, budget=budget, out_dir=args.output, workers=args.workers,
                    model_name=name, do_grade=not args.no_grade, grade_checkpoints=args.grade_checkpoints,
                    checkpoint_every=args.checkpoint_every, network=args.network, strict=not args.lenient,
                    timeout=args.timeout)
    for ep in eps:
        g = ep.grade or {}
        log(f"{ep.instance_id}: {ep.exit_reason} turns={ep.turns} cost=${ep.cost:.4f} "
            f"{'RESOLVED' if g.get('resolved') else g.get('failure', 'ungraded')}")
    log(f"{len(eps)} episodes -> {args.output} (report: swekit report {args.output} --tasks {args.tasks})")
    return 0


def cmd_import_traj(args) -> int:
    from .grade import grade
    from .trajectory import from_swe_agent

    tasks = {t.instance_id: t for t in _load(args.tasks, args)} if args.tasks else {}
    paths: list[Path] = []
    for p in args.trajectories:
        p = Path(p)
        paths.extend(sorted(p.rglob("*.traj")) if p.is_dir() else [p])
    n = 0
    for p in paths:
        ep = from_swe_agent(p)
        if args.grade and ep.instance_id in tasks:
            ep.grade = grade(tasks[ep.instance_id], ep.patch, args.backend).to_dict()
        ep.save(args.output)
        n += 1
        log(f"{ep.instance_id}: {ep.exit_reason} turns={ep.turns} cost=${ep.cost:.2f}"
            + (f" resolved={ep.grade['resolved']}" if ep.grade else ""))
    log(f"imported {n} trajectories -> {args.output}")
    return 0


def cmd_report(args) -> int:
    from .report import build_report, to_markdown
    from .task import read_jsonl
    from .trajectory import load_episodes

    tasks = _load(args.tasks, args) if args.tasks else []
    names = args.names.split(",") if args.names else []
    reports = []
    for i, run in enumerate(args.runs):
        p = Path(run)
        name = names[i] if i < len(names) else p.stem if p.is_file() else p.name
        if p.is_file():
            reports.append(build_report(grades=list(read_jsonl(p)), tasks=tasks, name=name))
        else:
            reports.append(build_report(load_episodes(p), tasks=tasks, name=name))
    md = to_markdown(reports)
    if args.md:
        Path(args.md).write_text(md)
    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=2))
    print(md)
    return 0


def cmd_localize(args) -> int:
    from .localize import localize_task, summarize
    from .task import write_jsonl

    tasks = _load(args.tasks, args)
    budgets = [int(b) for b in args.budgets.split(",")]
    locs = []
    for t in tasks:
        try:
            loc = localize_task(t, budgets, include_tests=args.include_tests)
        except Exception as exc:
            log(f"{t.instance_id}: {exc}")
            continue
        locs.append(loc)
        log(f"{t.instance_id}: first oracle file at rank {loc.first_hit_rank} of {loc.n_files}")
    if args.output:
        write_jsonl(args.output, (l.to_dict() for l in locs))
    print(json.dumps(summarize(locs), indent=2))
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="swekit", description="From git history to RL reward for SWE-bench-style tasks.")
    ap.add_argument("--version", action="version", version=f"swe-env-kit {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="build the original demo repository")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("fetch", help="download a public dataset (SWE-bench, Lite, Verified, Pro) as tasks")
    p.add_argument("dataset", help="swebench | swebench-lite | swebench-verified | swebench-pro | <hf org/name>")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--split", default="test")
    p.add_argument("--limit", type=int)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--ids")
    p.add_argument("--raw", action="store_true", help="keep the dataset's own rows (no conversion)")
    p.add_argument("--with-scripts", action="store_true", help="Pro: also fetch each instance's run_script and parser")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("convert", help="convert tasks between formats")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--to", default="swekit", choices=["swekit", "swebench", "swebench-pro"])
    _add_selection(p)
    p.set_defaults(fn=cmd_convert)

    p = sub.add_parser("mine", help="mine candidate tasks from git history (commit pairs or merged PRs)")
    p.add_argument("source", help="local repo path or clone URL (commits), or owner/name (prs)")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--mode", choices=["commits", "prs"], default="commits")
    p.add_argument("--repo", help="owner/name label for the tasks")
    p.add_argument("--pro", action="store_true", help="SWE-bench Pro filters (>=10 changed lines, <=100 tasks)")
    p.add_argument("--min-lines", type=int)
    p.add_argument("--max-lines", type=int)
    p.add_argument("--max-tasks", type=int)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--branch")
    p.add_argument("--max-commits", type=int)
    p.add_argument("--max-prs", type=int, default=500)
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("stats", help="task sizes, triviality, Pro-eligibility, naming leaks, licenses")
    p.add_argument("tasks")
    p.add_argument("--min-lines", type=int, default=10)
    p.add_argument("--licenses", action="store_true", help="look up repo licenses on GitHub")
    p.add_argument("--json", help="also write per-task stats here")
    _add_selection(p)
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("spec", help="draft Pro-style interface and requirements; check for leaks")
    p.add_argument("tasks")
    p.add_argument("-o", "--output")
    p.add_argument("--overwrite", action="store_true", help="replace existing interface/requirements")
    p.add_argument("--check-only", action="store_true", help="only run the leak check (exit 1 on leaks)")
    p.add_argument("--report")
    _add_selection(p)
    _add_llm(p)
    p.set_defaults(fn=cmd_spec)

    p = sub.add_parser("env", help="write and/or build Docker environments")
    p.add_argument("tasks")
    p.add_argument("--dockerfiles", help="write Dockerfiles under this directory")
    p.add_argument("--build", action="store_true")
    p.add_argument("--rebuild", action="store_true")
    _add_selection(p)
    p.set_defaults(fn=cmd_env)

    p = sub.add_parser("validate", help="pre/post test runs -> FAIL_TO_PASS, PASS_TO_PASS, flaky, naming leaks")
    p.add_argument("tasks")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--backend", choices=["docker", "local"], default="docker")
    p.add_argument("--runs", type=int, default=2, help="runs per phase (more catches more flaky tests)")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--timeout", type=float)
    p.add_argument("--naming", choices=["warn", "invalid"], default="warn")
    p.add_argument("--keep-invalid", action="store_true")
    p.add_argument("--report")
    _add_selection(p)
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("grade", help="grade predictions (or the gold / empty patch)")
    p.add_argument("tasks")
    p.add_argument("predictions", nargs="?")
    p.add_argument("-o", "--output", default="grades.jsonl")
    p.add_argument("--gold", action="store_true")
    p.add_argument("--nop", action="store_true")
    p.add_argument("--backend", choices=["docker", "local"], default="docker")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--timeout", type=float)
    p.add_argument("--lenient", action="store_true",
                   help="only flag (do not fail) skip markers and test-setting edits; test files are reverted either way")
    _add_selection(p)
    p.set_defaults(fn=cmd_grade)

    p = sub.add_parser("run", help="run an agent on tasks with budgets, then grade")
    p.add_argument("tasks")
    p.add_argument("--agent", required=True, help="nop | oracle | replay:<dir> | llm | module.path:Class")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--backend", choices=["docker", "local"], default="docker")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--max-turns", type=int, default=50)
    p.add_argument("--max-cost", type=float)
    p.add_argument("--max-seconds", type=float)
    p.add_argument("--command-timeout", type=float, default=180)
    p.add_argument("--max-output-chars", type=int, default=12_000)
    p.add_argument("--checkpoint-every", type=int, default=0)
    p.add_argument("--grade-checkpoints", action="store_true")
    p.add_argument("--no-grade", action="store_true")
    p.add_argument("--network", action="store_true",
                   help="give agent containers network access (off by default: the fix is one git fetch away)")
    p.add_argument("--lenient", action="store_true", help="see grade --lenient")
    p.add_argument("--timeout", type=float, help="test timeout when grading (default: the task's env.timeout)")
    _add_selection(p)
    _add_llm(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("import-traj", help="import SWE-agent .traj files as episodes (optionally grade them)")
    p.add_argument("trajectories", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--tasks")
    p.add_argument("--grade", action="store_true")
    p.add_argument("--backend", choices=["docker", "local"], default="docker")
    _add_selection(p)
    p.set_defaults(fn=cmd_import_traj)

    p = sub.add_parser("report", help="resolve rates by slice, budget curves, failure modes (compare runs)")
    p.add_argument("runs", nargs="+", help="run directories or grades.jsonl files")
    p.add_argument("--tasks")
    p.add_argument("--names", help="comma-separated names for the runs")
    p.add_argument("--md")
    p.add_argument("--json")
    _add_selection(p)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("localize", help="BM25 vs oracle file retrieval (SWE-bench Table 3)")
    p.add_argument("tasks")
    p.add_argument("--budgets", default="13000,27000,50000")
    p.add_argument("--include-tests", action="store_true")
    p.add_argument("-o", "--output")
    _add_selection(p)
    p.set_defaults(fn=cmd_localize)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, ValueError) as exc:
        log(f"swekit {args.command}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
