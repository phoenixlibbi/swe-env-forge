# swe-env-forge design

`swe-env-forge` covers the full life of a SWE-bench-style task, from git history to an RL reward:

```
            mine ──► env ──► validate ──► spec ──► run ──► grade ──► report
 git history │        │         │           │        │        │         │
             ▼        ▼         ▼           ▼        ▼        ▼         ▼
       candidate   Docker    F2P / P2P   problem   agent   binary +   breakdowns,
       tasks       image     flaky,      statement trajec- dense      budgets,
       (PR or      (or       naming      require-  tories  reward,    failure
       commit      local     leaks       ments,            tamper     modes
       pair)       sandbox)              interface         flags
```

Two side tools sit next to the pipeline:
- `localize`: BM25 vs oracle file retrieval.
- `stats`: task size, Pro-eligibility and contamination risk.

It reads and writes SWE-bench and SWE-bench Pro data, so it works with datasets that already exist.

## Design principles

1. **One task schema** (`swe_env_forge.task.Task`). Everything converts *into* it and can be exported *out* of it.
2. **The environment is data.** A task carries everything needed to run its tests in its `env` section:
   - the image or Dockerfile,
   - the workdir and setup commands,
   - the test command or a per-task run script,
   - the log parser.

   The harness never hard-codes a repo.
3. **Pluggable sandboxes.** The same code runs inside Docker (the real thing) or in a local temp checkout (fast, used by CI and the demo).
4. **The agent never sees the answer.** Agents only get `problem_statement`, `requirements` and `interface`, plus a repo at `base_commit` whose future git history has been stripped. Tests are applied only at grading time, in a fresh sandbox.
5. **Graders are hardened against tampering.** Before the hidden tests are applied, the grader reverts agent changes to test files, test helpers and test-runner config. Statuses must match validation, conflicting statuses fail, a must-fail canary detects framework tampering, and suspicious edits (skip markers, test settings, test-framework access) fail strict grading. Known residual risks are listed in the README.
6. **Binary and dense rewards from one run.**
   - `resolved` is the papers' metric: every F2P and P2P test passes.
   - `dense` is LHTB-style partial credit: the fraction of F2P tests passing, multiplied by the fraction of P2P tests still passing.
7. **No required dependencies.** Python standard library only. Docker and git are used through their CLIs. An LLM, if used, is any OpenAI-compatible HTTP endpoint.

## Task schema

```jsonc
{
  "instance_id": "owner__repo-123",
  "repo": "owner/repo",
  "base_commit": "<sha>",            // the agent starts here
  "problem_statement": "...",        // shown to the agent
  "requirements": "",                // shown (Pro-style), may be empty
  "interface": "",                   // shown (Pro-style), may be empty
  "hints": "",                       // optional, not shown by default
  "gold_patch": "diff --git ...",    // hidden: reference fix
  "test_patch": "diff --git ...",    // hidden: tests added/changed with the fix
  "fail_to_pass": ["..."],           // hidden: must flip fail -> pass
  "pass_to_pass": ["..."],           // hidden: must stay passing
  "language": "python",
  "source": "swebench | swebench-pro | mined-pr | mined-commits | custom",
  "created_at": "...",
  "env": {
    "image": "registry/image:tag",   // prebuilt image, or:
    "dockerfile": "FROM ...",        // build from this
    "platform": "linux/amd64",
    "workdir": "/testbed",
    "setup": ["source ... && conda activate testbed"],  // prefixed to every command
    "test_command": "pytest -rA {test_files}",          // {test_files} is filled in
    "test_files": ["tests/test_x.py"],                  // default: test files in test_patch
    "test_file_format": "path | dotted-module | comma | ...",
    "log_parser": "pytest | django | sympy | go | jest | mocha | cargo | junit | tap | json | auto",
    "run_script": null, "parser_script": null,          // SWE-bench Pro style per-task scripts
    "env_vars": {},
    "timeout": 1800,
    "hidden_paths": [],                // deleted before an agent starts (e.g. a baked-in test patch)
    "start_ref": "base | image-head",  // image-head: keep an image's environment commit on top of the base
    "services": [{"name": "db", "image": "postgres:16", "env": {}, "ports": [5432]}]
  },
  "metadata": {}
}
```

## Modules

| Module | Responsibility |
|---|---|
| `task` | `Task` / `EnvSpec` dataclasses, JSON/JSONL load and save |
| `patches` | Parse unified diffs. Split a diff into test and source parts. Line and file stats. List added definitions. |
| `languages` | Per-language rules: file extensions, test-path patterns, definition regexes, "missing symbol" error patterns, skip markers |
| `logparsers` | Test log → `{test_name: PASSED / FAILED / SKIPPED / ERROR}` for pytest, django, sympy, go (json and verbose), jest/vitest (json), mocha (json), cargo, JUnit/TRX XML, TAP, Pro-style JSON. `auto` tries all of them. |
| `formats` | Convert from and to SWE-bench and SWE-bench Pro rows. Auto-detects the format. |
| `presets` | SWE-bench per-repo test commands and parsers. SWE-bench image names. Pro image and script conventions. |
| `hub` | Fetch public datasets through the HuggingFace datasets-server API, Pro run scripts from GitHub, and data from the GitHub REST API. Everything is cached. |
| `sandbox` | `DockerSandbox` and `LocalSandbox` behind one `exec / write_file / read_file / close` interface |
| `workspace` | Reset the repo to `base_commit`, strip future history, apply patches (three fallbacks), collect the agent diff, run the tests |
| `validate` | Run `log_pre` (tests only) and `log_post` (tests + gold) N times each. Derive F2P, P2P, flaky and broken tests. Find naming leaks. Give a verdict. |
| `grade` | Grade a candidate patch: tamper guard, apply the hidden tests, run, compute binary and dense reward |
| `mine` | Mine candidate tasks from a repo's history: `commits` (Pro: consecutive commit pairs, works on private repos; `window` merges code and tests pushed in separate commits) or `pr` (SWE-bench: merged PRs that link an issue). Pro filters. |
| `spec` | Draft the Pro-style `interface` (new symbols that tests call) and a requirements skeleton. Check that text doesn't leak the gold patch. Optional LLM polish. |
| `gym` | `SWEEnv` with `reset()` / `step(action)`. Budgets (turns, cost, wall time), per-turn checkpoints, trajectory logging, batch runner. |
| `agents` | `nop`, `oracle`, `replay` (our trajectories or SWE-agent `.traj`) and `llm` (OpenAI-compatible) |
| `report` | Resolve rate and dense reward by language, size and files. Budget curves (score at turn and cost caps). Failure modes. |
| `failure` | Heuristic failure-mode classifier, using the categories from the Pro paper |
| `localize` | BM25 (paths prepended, code-aware tokenizer) vs oracle file recall at token budgets. SWE-bench style prompt builder. |
| `stats` | Per-task size stats, the trivial (1–2 line) flag, Pro-eligibility, static naming-leak check, license class (permissive / copyleft) |
| `dockerfiles` | Fallback per-language environments for mined tasks |
| `envdetect` | Environment plans from the repo's own CI workflows (matrix-resolved setup-* versions, services, env), version files, dependency manifests, tox, compose. Every decision recorded as evidence. |
| `envagent` | Setup agent: build, validate, and let a model repair the Dockerfile or test command until the task validates. Validation is the reward. Proposals are checked before use. |
| `yamlite` | A standard-library YAML subset reader for CI and compose files (PyYAML is used when installed) |
| `tools` | SWE-agent style tools for the gym: `str_replace_editor`, `search`, `find_file`, and function-calling schemas |
| `llm` | A minimal OpenAI-compatible chat client (urllib) with token and cost accounting |

## Grading semantics

Given a task `T` and a candidate patch `P`:

1. Fresh sandbox, then `git checkout -f <start> && git clean -fd` in the workdir. `<start>` is `base_commit`, or, with `start_ref = "image-head"`, the image's own HEAD, checked to descend from the base by at most 5 commits.
2. Apply `P`. Try `git apply`, then `git apply --reject`, then `patch --fuzz`.
   - If all three fail: `resolved = false`, `failure = patch_apply_error`.
3. **Tamper guard.** Strict by default.
   - Any file touched by `test_patch` that `P` also changed is restored to its base version (or deleted if `P` created it).
   - Changed test files, deleted tests and added skip markers are recorded in `flags`.
4. Apply `test_patch`. If it fails, that's a harness error, not an agent failure.
5. Run the tests and parse the log into statuses. A test missing from the log counts as failed.
6. `resolved = all(F2P passed) and all(P2P passed)`.
   - `f2p_rate = passed F2P / |F2P|`
   - `p2p_rate = passed P2P / |P2P|` (1.0 if P2P is empty)
   - `dense = f2p_rate * p2p_rate`

## Validation semantics

These are SWE-bench Stage III, plus Pro's repeated gold runs, plus Terminal-Bench's no-op check.

- **`pre`** = base + test_patch, run `N` times. **`post`** = base + test_patch + gold_patch, run `N` times. Each run gets a fresh sandbox, so ignored build outputs and caches that survive `git clean` can't carry over from one run to the next.
- A test is `PASS` (or `FAIL`) in a set of runs only if it has that status in **every** run. Otherwise it's `FLAKY`.
- **F2P** = FAIL in pre and PASS in post. **P2P** = PASS in pre and PASS in post. **P2F** = broken by the gold patch. Flaky tests are excluded and reported.
- **Base run (masked F2P):** if F2P is non-empty, the tests run once more at the start commit *without* the test patch. A would-be F2P test moves to P2P when it passed there and its body is unchanged by the test patch. Such a test only failed because the new test file imports a name the fix creates. Changed tests are found by mapping each test-patch hunk to the test that encloses it by indentation: `def` in Python, `it(...)` / `test(...)` / `describe(...)` in JavaScript and TypeScript (mocha, jest, vitest, ospec), `func` and `t.Run` in Go, `fn` in Rust, methods for the JVM / .NET / Swift / PHP family, gtest `TEST` macros, and RSpec / minitest. A change directly inside a `describe` block counts for every test in it. For a language without rules, nothing is unmasked.
- **Environment diagnosis:** modules still missing after the fix give `environment_incomplete:<module>`, not `no_f2p`.
- **Naming leak:** "missing symbol" errors in `pre` (ImportError, Go `undefined:`, Rust E0425, C# CS0103, Java "cannot find symbol", …) whose symbol isn't mentioned in the problem statement or interface.
- **Verdict:**
  - `valid`: at least one F2P test, gold applies, tests parsed.
  - Otherwise `invalid` with a reason (`no_f2p`, `gold_apply_error`, `test_patch_apply_error`, `no_tests_parsed`).
  - Warnings: `flaky_tests`, `p2f_tests`, `naming_leak`.

## Leakage controls

- **History stripping.** After checkout, every ref that isn't an ancestor of `base_commit` is deleted, along with remotes, reflogs and stashes, then `git gc --prune=now`. Verified with `git rev-list --all --not base_commit` being empty.
- **Hidden material stays out.** Hidden tests and the gold patch never enter the agent's sandbox.
- **No network.** `--network none` is available for agent containers.
