# swe-env-kit

> From git history to RL reward: a toolkit to mine, validate, specify, run and grade SWE-bench / SWE-bench Pro style software-engineering tasks in any language, with tamper-proof binary and dense rewards.

[![CI](https://github.com/phoenixlibbi/swe-env-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/phoenixlibbi/swe-env-kit/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

**Every coding benchmark task is an RL environment plus a verifier.** `swekit` (the CLI and Python package of this repo) builds both the way [SWE-bench](https://arxiv.org/abs/2310.06770) and [SWE-bench Pro](https://arxiv.org/abs/2509.16941) do:
1. It turns a repository's history into tasks.
2. It proves each task is fair by running the tests before and after the real fix.
3. It writes the task description an agent is allowed to see.
4. It runs agents with turn and cost budgets.
5. It grades their patches with a verifier that resists the obvious cheats.

```
            mine ──► env ──► validate ──► spec ──► run ──► grade ──► report
 git history │        │         │           │        │        │         │
             ▼        ▼         ▼           ▼        ▼        ▼         ▼
       candidate   Docker    F2P / P2P   problem   agent    binary +  slices,
       tasks       image     flaky,      statement episodes dense     budget
       (PR or      (or       naming      require-  with     reward,   curves,
       commit      local     leaks       ments,    budgets  tamper    failure
       pair)       sandbox)              interface          flags     modes
```

| Command | What it does | Docker / LLM needed |
|---|---|---|
| `swekit fetch` / `convert` | Download SWE-bench, Lite, Verified or Pro from HuggingFace; convert between swe-env-kit, SWE-bench, Pro and generic rows | no / no |
| `swekit mine` | Turn git history into candidate tasks: merged PRs that close an issue (SWE-bench), or consecutive commit pairs (Pro, works on private repos) | no / no |
| `swekit stats` | Fix size, "trivial 1–2 line" tasks, Pro eligibility, static naming leaks, license class | no / no |
| `swekit spec` | Write the Pro-style problem statement, requirements and interface: deterministic drafts, an LLM writer, or your own agent; leak check | no / optional |
| `swekit spec-export` / `spec-import` | Human review: one editable Markdown file per task; records who wrote and reviewed every field | no / no |
| `swekit env` | Write or build a Docker environment per task (dataset image, its Dockerfile, or a generated one for 10 languages) | yes / no |
| `swekit validate` | Run the tests before and after the reference fix N times: FAIL_TO_PASS, PASS_TO_PASS, flaky tests, tests the fix breaks, naming leaks | yes (or `--backend local`) / no |
| `swekit run` | Run an agent in a gym with SWE-agent style tools (bash, `str_replace_editor`, search, find_file), turn / cost / time budgets and checkpoint grading. Agents: nop, oracle, replay, a tool-calling LLM agent, or your class | yes / optional |
| `swekit grade` | Grade patches (SWE-bench predictions format) with a tamper guard; binary + dense reward; CI gate with `--expect` | yes / no |
| `swekit import-traj` | Import SWE-agent `.traj` files so existing runs can be graded, reported and failure-classified | optional / no |
| `swekit report` | Resolve rate by language, fix size and file count; budget curves; failure modes; compare runs side by side | no / no |
| `swekit localize` | BM25 vs oracle file retrieval at 13k / 27k / 50k token budgets (SWE-bench's Table 3) | no / no |

It has no dependencies beyond the Python standard library. Docker and git are used through their CLIs, and LLMs through any OpenAI-compatible HTTP endpoint.

## Why labs and data teams use it

Frontier labs train coding models with RL on SWE-style tasks and report SWE-bench / Pro scores. These are the problems that work runs into, and what swe-env-kit does about each:

| Problem | Why it hurts | What swe-env-kit does |
|---|---|---|
| **Fresh tasks are scarce.** Public benchmarks are saturated (Verified over 70%) and likely in pre-training data. | Scores on memorized tasks measure nothing. Labs buy new tasks from data vendors. | `mine --mode commits` turns any repository, **including private or licensed code**, into tasks with Pro's method. `stats` measures triviality and license-based contamination risk. |
| **Rewards get hacked.** Policies learn to edit tests, add a `conftest.py` that fakes passes, or read the fix from git history. | A hackable reward trains the model to cheat and wastes the RL compute. | The tamper guard reverts tests, helpers and runner config. Agents run offline. Future history is purged and the purge verified. Pro's parser runs outside the sandbox. Each attack is a regression test. |
| **Broken tasks poison training.** Flaky tests, F2P tests that don't really fail, gold patches that break other tests, "guess the name" false negatives. | A wrong reward is worse than none: a correct answer that fails teaches the wrong thing. | `validate` runs before and after the fix N times. `grade --gold/--nop --expect` is a CI gate. `spec --check-only` blocks descriptions that leak the fix. |
| **Binary rewards are sparse on long tasks.** | On 100-line, multi-file Pro tasks most rollouts score 0 and give no gradient. | Every grade returns a dense reward (F2P rate × P2P rate) next to the binary one, plus checkpoint grades along the trajectory. |
| **Annotation is the expensive step.** Pro paid experts to write every task's text. | It takes thousands of hours and needs an audit trail. | Deterministic drafts, an LLM writer with leak-check retries, or your own agent. Human review files record who wrote and who reviewed every field. |
| **Eval numbers aren't comparable.** The same model scores ~23% at a $2 cap and 43.6% uncapped. | Models must be compared at equal compute, and failures understood to decide what to train next. | Budget curves give the score at any turn or cost cap from one run. Failure modes use Pro's buckets. Every result is sliced by language and fix size. |
| **Every dataset has its own format.** | Glue code everywhere. | Reads and writes SWE-bench, Pro, generic and specs-dict rows. Predictions are compatible with the official harness. SWE-agent trajectories import directly. |

**Where it stops.** This is the task-factory and verifier layer.
- **Scale-out:** running thousands of sandboxes in parallel (Kubernetes, Modal) plugs in behind the `Sandbox` interface; it isn't included here.
- **Review:** the static checks are heuristics designed to be reviewed by a person.

## Who is this for

| You are... | Use it to... | Commands |
|---|---|---|
| **Building RL environments or benchmark tasks** | Mine tasks from any repo (including private ones), prove each is fair (fails before the fix, passes after, no flaky tests), and catch descriptions that leak the answer | `mine`, `validate`, `spec`, `grade --gold/--nop --expect` |
| **Training coding agents with RL (RLVR)** | Get a verifiable reward with a dense variant, from a gym whose sandbox hides the answer and whose grader reverts test tampering | Python API: `SWEEnv`, `grade` |
| **Running evals / leaderboards** | Grade SWE-bench and Pro predictions in the official images; report scores at any turn or cost cap from one run; classify failures | `fetch`, `grade`, `run`, `report` |
| **Annotating tasks (Pro-style)** | Give annotators drafts instead of blank pages, or let an LLM write the fields and have people review them | `spec`, `spec-export`, `spec-import` |
| **Researching retrieval or contamination** | Reproduce SWE-bench's BM25-vs-oracle study; measure triviality, license mix and naming leaks of a dataset | `localize`, `stats` |

### Typical workflows

**"Turn our private repo into an RL environment."**
```bash
swekit mine ~/code/our-service -o mined.jsonl --pro          # commit pairs, >= 10 changed lines, <= 100 tasks
swekit env mined.jsonl --build                                # per-language Docker images (edit env.dockerfile if needed)
swekit validate mined.jsonl -o valid.jsonl --runs 3           # F2P / P2P, flaky tests out, naming leaks flagged
swekit spec valid.jsonl -o specced.jsonl --mode llm --model gpt-4.1   # or: --mode draft, then human review
swekit spec-export specced.jsonl review/ && swekit spec-import specced.jsonl review/ -o final.jsonl --require-reviewed
```

**"What does our agent really score, and why does it fail?"**
```bash
swekit fetch swebench-pro -o pro.jsonl --with-scripts --limit 50
swekit run pro.jsonl --agent mypkg.agent:MyAgent -o runs/mine --max-turns 50 --max-cost 2 --checkpoint-every 5 --grade-checkpoints
swekit report runs/mine --tasks pro.jsonl --md report.md       # by language / size, budget curves, failure modes
```

**"Is this task safe to train on?"**
```bash
swekit grade tasks.jsonl --gold --expect resolved      # the reference fix must pass
swekit grade tasks.jsonl --nop  --expect unresolved    # doing nothing must fail
swekit spec tasks.jsonl --check-only                   # no field may copy code from the fix
```

### Use in CI

Guard a task repository so that no task with a broken verifier or a leaking description gets merged:

```yaml
# .github/workflows/tasks.yml
on: [pull_request]
jobs:
  task-health:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install git+https://github.com/phoenixlibbi/swe-env-kit
      - run: swekit spec tasks.jsonl --check-only
      - run: swekit grade tasks.jsonl --gold --expect resolved
      - run: swekit grade tasks.jsonl --nop --expect unresolved
```

Exit codes:
- `0`: ok.
- `1`: a gate failed or the input was bad. A gate means a `--expect` mismatch, a leak found by `spec --check-only`, no valid tasks in `validate`, or a failed `env --build`.
- `2`: usage error.

---

## Background: two papers, one pipeline

**SWE-bench** (Jimenez et al., ICLR 2024) made coding evaluation real:
- **The tasks:** 2,294 GitHub issues from 12 Python repositories, each paired with the pull request that fixed it.
- **The setup:** the model gets the issue and the repository at the commit before the fix, and must write a patch.
- **The grade:** the repository's own tests.
  - **FAIL_TO_PASS (F2P):** the tests the fix's PR added, which must flip from failing to passing.
  - **PASS_TO_PASS (P2P):** the tests that passed before, which must keep passing.
- **The filter:** tasks were kept only if this could be checked by execution. The authors ran the tests before the fix (`log_pre`) and after it (`log_post`), and dropped tasks whose tests failed before the fix with `ImportError` / `AttributeError`. Those errors mean the test calls a name invented in the fix, which no solver could guess.
- **The baselines** gave the model files chosen by BM25 retrieval, or the "oracle" files the fix edited. The best 2023 model solved about 2%.

**SWE-bench Pro** (Scale AI, 2025) keeps the recipe but fixes what made SWE-bench saturate (over 70% on the 500-task Verified subset):
- **Contamination:** it uses copyleft (GPL) repositories and code bought from startups, and keeps a held-out set private.
- **Triviality:** every fix is at least 10 lines; on average 107 lines across 4 files.
- **Narrowness:** it covers Python, Go, JavaScript and TypeScript, with 50–100 tasks per repository.
- **New task source:** tasks are mined from **consecutive commit pairs** instead of issue-linked PRs.
- **Human augmentation:** experts write a problem statement, **requirements** and an **interface** for every task. Strict unit tests then stop rejecting valid solutions over a different function name, a "false negative".
- **Evaluation:** agents run in SWE-agent with a 50-turn limit. Top models scored about 23% under a $2 cost cap and up to 43.6% uncapped, and the paper classifies failures with an LLM judge.

| | SWE-bench | SWE-bench Pro | swe-env-kit |
|---|---|---|---|
| Task source | merged PR + linked issue | consecutive commit pair | both (`mine --mode prs / commits`) |
| Languages | Python | Python, Go, JS, TS | any; 20+ test-log formats |
| Task text | the raw issue | human-written statement + requirements + interface | drafts, LLM writer, custom agent or human review |
| Validity check | log_pre vs log_post, drop ImportError tasks | + repeated gold runs, human test review | log_pre / log_post × N runs, flaky and naming-leak detection |
| Context for the model | BM25 or oracle retrieval | agent explores the repo (SWE-agent, 50 turns) | `localize` for the former, `run` for the latter |
| Reward | binary (all F2P and P2P pass) | binary | binary **and** dense (F2P rate × P2P rate) |
| Anti-leak | n/a | private and copyleft data | history purge, hidden paths, offline agents, leak check |

## Key ideas, explained

| Term | Meaning | Where in the code |
|---|---|---|
| **Gold patch** | The human fix from the real commit or PR: the answer key. Never shown to the agent. | `Task.gold_patch` |
| **Test patch** | The tests added or changed with the fix. Applied only at grading time. | `Task.test_patch` |
| **FAIL_TO_PASS / PASS_TO_PASS** | Tests that must flip to passing / must stay passing | `validate.classify`, `grade.score` |
| **log_pre / log_post** | Test results before and after the gold patch. Only the pair proves a test detects the bug *and* the fix cures it. | `validate.validate_in` |
| **Naming leak** | A test calls a name invented in the fix, so a correct solution with another name fails (a false negative). SWE-bench drops these tasks; Pro states the names in the interface. | `languages.missing_symbols`, `stats.task_stats` |
| **Interface** | The *outside* of the new code that tests call: name, file, signature. It says *what* to build, never *how*. | `spec.draft_interface` |
| **Requirements** | Behaviours the hidden tests check, including exact strings, written as behaviour rather than code | `spec.draft_requirements` |
| **Flaky test** | A test whose result changes between identical runs. It's excluded from rewards. | `validate.combine` |
| **Contamination** | The model saw the task's answer during pre-training, so the score measures memory | `stats.license_class` |
| **Copyleft (GPL)** | A license requiring derived works to stay open. Labs tend to avoid training on it, which gives contamination *resistance*, not a guarantee. | `stats.license_class` |
| **Saturation** | Top models crowd near the ceiling, so the benchmark can't separate them | budget curves in `report` |
| **Commit pair** | (parent, commit) in git history: the base is the parent, and the diff splits into test and gold patches | `mine.mine_commits` |
| **BM25 vs oracle** | Keyword retrieval of files vs handing the model exactly the files the fix edits. The gap measures how much search costs. | `localize` |
| **Dense reward** | Partial credit, F2P pass rate × P2P pass rate. It gives a learning signal where binary gives 0, and the P2P factor punishes regressions. | `grade.score` |
| **Tamper guard** | The grader reverts test files, test helpers and runner config the agent touched, and fails skip markers and test-setting edits | `grade.tamper_flags` |
| **History purge** | No commit after the base is left in the agent's git object store, so `git log --all` can't reveal the fix | `workspace.strip_future_history` |
| **Budget curve** | The resolve rate if the run had stopped at turn *t* or cost *c*. It explains capped vs uncapped scores (Pro: ~23% vs 43.6%). | `report.budget_curves` |

## Results

### 1. The tool reproduces the papers' own measurements

From `swekit stats` on the public datasets as published on HuggingFace (Verified: 500 tasks; Pro public split: 642 tasks):

| | swe-env-kit | Pro paper |
|---|---|---|
| Verified tasks needing only 1–2 edited lines | **161 / 500** | "161 out of 500" |
| Pro reference fix, mean edited lines | **107.8** | 107.4 |
| Pro reference fix, code files touched (mean) | 3.8 | 4.1 |
| Pro tasks under 10 changed lines | 0 | excluded by design |
| Largest single-repo share: Verified / Pro | 46.2% (django) / 12.8% | "imbalanced" vs "50–100 per repo" |

### 2. Pro's human augmentation removes most "guess the name" false negatives

The static check counts tasks where the hidden tests call a function or class that the reference fix *creates*, but the agent-visible text never names:

| Pro, the agent sees… | Tasks with unspecified new names |
|---|---|
| problem statement only | **44.7%** (287 / 642) |
| + Pro's human requirements and interface | **3.7%** |
| + swe-env-kit's automatic `spec` drafts | **2.2%** |
| *(SWE-bench Verified, statement only)* | *1.4%*: SWE-bench filtered these tasks out |

This is the mechanism behind the paper's ablation, where GPT-5 drops from 25.9% to 8.4% without the augmentations.

Caveat: the 2.2% is partly by construction, because the drafter and the checker share the same "new names the tests call" logic. Compared with what Pro's annotators actually wrote, the automatic drafts find 56.5% of the human-listed names (humans also list public functions the tests never call) at 70.7% precision. Drafts are a starting point for a reviewer or an LLM writer, not a replacement for review.

### 3. Retrieval is the bottleneck SWE-bench reported

`swekit localize` on all 300 SWE-bench Lite tasks: the share of tasks where BM25 (file paths prepended, code-aware tokens) puts the files the fix edits into the prompt at each token budget.

| Token budget | swe-env-kit, Lite (300) | SWE-bench paper, full test set ("all" recall) |
|---|---|---|
| 13k | 25.7% | 26.1% |
| 27k | 47.0% | 39.8% |
| 50k | 58.7% | 45.9% |

Rank metrics: recall@1 36.0%, recall@5 65.7%, recall@10 74.7%, MRR 0.50.

Even at 50k tokens, **41% of tasks never show the model the file it must change**, and a bigger budget adds noise. That is why SWE-bench's one-shot baselines solved about 2%, and why Pro (and `swekit run`) let the agent search for itself.

How to read the comparison: Lite tasks edit a single file, so "all", "any" and "average" recall coincide. Our token counts approximate the paper's tokenizer at about 4 characters per token, or use `tiktoken` when it is installed.

### 4. Verified against the official harnesses (live Docker runs)

All runs use the benchmarks' **own** images:

| Task | Benchmark | Our F2P / P2P | Matches the dataset? | Gold patch | Empty patch |
|---|---|---|---|---|---|
| `pytest-dev__pytest-5227` | SWE-bench Lite | 3 / 34 | identical | resolved | not resolved |
| `django__django-11099` | SWE-bench Lite | 2 / 20 | P2P ⊇ dataset; one dataset F2P test passes *before* the fix in the official image | resolved | not resolved |
| `psf__requests-2317` | SWE-bench Lite | 1 / 141 | 7 dataset F2P tests call httpbin.org over the live network and pass before the fix today, so they aren't reproducible | n/a | n/a |
| `openlibrary-00bec1e7` | SWE-bench Pro | 5 / 15 | identical | resolved | not resolved |
| `navidrome-0130c6dc` (Go) | SWE-bench Pro | 1 / 0 | identical | resolved | not resolved |

- **Pro scripts:** the Pro tasks ran Pro's own per-instance `run_script.sh`, with its `parser.py` executed outside the sandbox.
- **Oracle agent:** driven through the gym, it resolved both SWE-bench tasks, with checkpoint grades on every turn.
- **History probe:** an agent in the same sandboxes saw **0 commits after the base** (`git rev-list --all --not HEAD`) out of django's 26,747. The official images already ship history only up to the base. Repos cloned for mined tasks and many third-party datasets don't, which is why the purge and its check always run.

### 5. Hardened against an adversarial review

An independent review attacked the grader and the sandbox. Every finding is fixed and replayed in [`tests/test_hardening.py`](tests/test_hardening.py):

| Attack | Before | Now |
|---|---|---|
| `conftest.py` hook that marks every test passed | resolved | reverted, not resolved |
| `pyproject.toml` `addopts = "-p plugin"` + plugin module | resolved even in strict mode | `tampering` |
| Files named `safe.directory=1/2` that break an unquoted git glob | harness error, episode never graded | graded normally |
| Renaming a hidden test file / a file where tests need a directory | harness error (rollout dropped) | reverted; blocking the tests counts as tampering |
| Printing a fake end-of-tests marker | results hidden | random per-run markers |
| Code under test rewriting Pro's `parser.py` | resolved | parser runs outside the sandbox |
| `.keep` packs / `ORIG_HEAD` keeping the future fix reachable | fix readable | purged, verified on the whole object store |
| Mined image built from a bundle of `--all` refs | fix inside the image | bundle of the base history only |

## Install

```bash
pip install git+https://github.com/phoenixlibbi/swe-env-kit
swekit --version
```

You need Python 3.10+ and git. Docker is needed for the `docker` backend, which is the default and the real sandbox. On Apple Silicon, the x86 benchmark images run under emulation; `env.platform` is set for you.

## Quickstart (no Docker, two minutes)

The package ships an original demo repository with a realistic history:
- a bug fix,
- a feature whose tests import a brand-new function,
- a docs-only commit,
- a multi-file change,
- a commit with a flaky test.

```bash
swekit demo minishop                                   # build the demo repo
swekit mine minishop -o mined.jsonl                    # 4 candidate tasks (the docs-only commit is skipped)
swekit spec mined.jsonl -o spec.jsonl                  # draft interface + requirements
swekit validate spec.jsonl -o tasks.jsonl --backend local --runs 2
swekit run tasks.jsonl --agent oracle -o runs/oracle --backend local --checkpoint-every 1 --grade-checkpoints
swekit run tasks.jsonl --agent nop    -o runs/nop    --backend local
swekit report runs/oracle runs/nop --tasks tasks.jsonl
```

What `validate` finds:

```
demo__minishop-298f07aeb9b0: valid f2p=1 p2p=2                     # the rounding bug fix
demo__minishop-fc32df4e382f: valid f2p=1 p2p=2 f2p_masked_by_import_error:2   # feature; its tests import a new name
demo__minishop-d17fd482950e: valid f2p=1 p2p=2                     # multi-file bulk discounts (the only Pro-eligible one)
demo__minishop-0952843e2bf7: valid f2p=1 p2p=0 flaky_tests:1       # the alternating test is excluded
```

`--backend local` runs commands on your machine in a temporary checkout. Use it for trusted code only. The CLI warns if you point a real agent at it.

## Usage

### Get tasks

```bash
swekit fetch swebench-verified -o verified.jsonl                 # official image, eval script and parser per task
swekit fetch swebench-pro -o pro.jsonl --with-scripts --limit 20 # + Pro's run_script.sh / parser.py per task
swekit mine owner/name --mode prs -o prs.jsonl --max-prs 300     # SWE-bench rules (needs a GitHub token or `gh auth`)
swekit mine ./repo -o commits.jsonl --pro --since 2024-01-01     # Pro rules, offline, private repos fine
swekit convert other.jsonl -o tasks.jsonl                        # generic rows, including SWE-bench style `specs` dicts
swekit convert tasks.jsonl --to swebench -o sb.jsonl             # export for the official SWE-bench harness
```

### Validate

```bash
swekit validate tasks.jsonl -o valid.jsonl --runs 3 --workers 4 --report validation.jsonl
swekit validate tasks.jsonl -o valid.jsonl --naming invalid       # SWE-bench's rule: reject unspecified new names
```

Each report row holds:
- the `verdict` (`valid` / `invalid` / `error`), with reasons and warnings,
- the F2P, P2P, P2F, F2F and flaky test lists,
- the naming errors, each marked `specified` or not,
- the log tails.

### Write the task description (Pro's human augmentation)

| Mode | Who writes | Command |
|---|---|---|
| Draft | Deterministic: interface from the code, requirements from the tests | `swekit spec tasks.jsonl -o s.jsonl` |
| LLM | A model writes the statement, requirements and descriptions; retried if the leak check fires | `swekit spec tasks.jsonl -o s.jsonl --mode llm --model gpt-4.1` |
| Custom agent | Any class with `write(task, drafts) -> dict` | `swekit spec tasks.jsonl -o s.jsonl --writer mypkg.writers:Writer` |
| Human review | People edit one Markdown file per task | `swekit spec-export s.jsonl review/` then `swekit spec-import s.jsonl review/ -o final.jsonl --require-reviewed` |

Every mode records `metadata.spec.fields_by` (`draft`, `llm:<model>`, `<writer>` or `human:<name>`) and whether a person reviewed the task. LLMs use any OpenAI-compatible endpoint:

```bash
export SWEKIT_LLM_API_KEY=...                     # or OPENAI_API_KEY
swekit spec tasks.jsonl -o s.jsonl --mode llm --model llama3.1 --base-url http://localhost:11434/v1   # e.g. Ollama
```

### Run agents

The agent works the way SWE-bench Pro evaluated models (in SWE-agent): it **explores the repository itself** and keeps its context across up to 50 turns. Each step is one tool call:

| Tool | What it does |
|---|---|
| `bash` | any shell command; the working directory persists |
| `str_replace_editor` | `view` (line numbers, ranges, directory trees), `create`, `str_replace` (exact, unique match), `insert`, `undo_edit` |
| `search` / `find_file` | grep the repository / find files by name or glob |
| `submit` | finish; the diff against the base commit is the answer |

The tools run through the harness's sandbox API, so they work in any image and language with nothing installed in the container.
- **Calling the tools:** the built-in LLM agent uses **native function calling** (`--agent-mode tools`). There's a text fallback for models without it (`--agent-mode text`).
- **Context management:** like SWE-agent, it keeps the last `--keep-observations` tool outputs in full and collapses older ones, which targets Pro's top failure mode for some models, context overflow.
- **Retrieval is separate:** retrieval (`localize`) is the 2023 one-shot baseline, kept to measure localization, not to drive the agent.

```bash
swekit run tasks.jsonl --agent llm --model gpt-4.1 --input-price 2 --output-price 8 \
       --max-turns 50 --max-cost 2 --checkpoint-every 5 --grade-checkpoints -o runs/gpt41
swekit run tasks.jsonl --agent replay:recorded/ -o runs/replay      # one commands file / trajectory per task
swekit run tasks.jsonl --agent mypkg.agents:MyAgent -o runs/mine    # reset(observation) / act(last_output)
swekit import-traj sweagent_runs/ --tasks tasks.jsonl --grade -o runs/sweagent
```

`run` writes the following, so the official SWE-bench harness can re-grade the same patches:
- one folder per task (`trajectory.jsonl`, `episode.json`, `patch.diff`)
- `results.jsonl`
- `predictions.jsonl` in SWE-bench's format

Interrupted batches resume where they stopped.

### Grade and report

```bash
swekit grade tasks.jsonl predictions.jsonl -o grades.jsonl --workers 4
swekit report runs/gpt41 runs/mine --tasks tasks.jsonl --md report.md --json report.json
swekit localize verified.jsonl --budgets 13000,27000,50000 -o loc.jsonl
```

The report contains:
- resolve rate and mean dense reward overall, by language, by fix size and by files touched,
- budget curves (the score at each turn and cost cap),
- failure modes in Pro's buckets: wrong solution, edge case, regression, syntax error, incorrect file, instruction following, long context, stuck in loop, endless reading, tool use,
- grader flags.

## Use it as an RL environment

```python
from swe_env_kit.formats import load_any
from swe_env_kit.grade import grade
from swe_env_kit.gym import Budget, SWEEnv

task = load_any("tasks.jsonl")[0]
with SWEEnv(task, backend="docker", budget=Budget(max_turns=50, max_cost=2.0)) as env:   # offline by default
    obs = env.reset()                    # problem statement, requirements, interface; no answers
    out, done, info = env.step({"type": "bash", "command": "grep -rn 'def apply_discount' ."})
    out, done, info = env.step({"type": "submit"})
    patch = env.final_patch

result = grade(task, patch, backend="docker")   # fresh container; tamper guard; hidden tests applied last
reward = result.dense                           # or float(result.resolved) for the papers' binary metric
```

**Choosing a reward.**
- **`resolved`** is the benchmark metric.
- **`dense`** (F2P rate × P2P rate) gives signal on partial fixes.
- **`f2p_rate`** alone ignores regressions, which you usually don't want.

Every grade also carries `flags`, `failure` and `reverted_test_files`, so reward-hacking attempts show up in your training logs.

## How it maps to the papers

| Paper section | Module |
|---|---|
| SWE-bench §2.1 Stages I–II: repos, merged PRs, linked issues, test changes | `mine.mine_prs` |
| SWE-bench §2.1 Stage III / App. A: log_pre, log_post, ImportError filter | `validate`, `languages.missing_symbols` |
| SWE-bench §4.1: BM25 (paths prepended) and oracle retrieval, Table 3 recall | `localize` |
| SWE-bench App. D: one-shot patch prompt | `localize.build_prompt`, `extract_patch` |
| SWE-bench eval: F2P + P2P all pass | `grade.score` |
| Pro §3.1: copyleft repos, contamination resistance | `stats.license_class` |
| Pro §3.1: ≥ 10 lines, multi-file, 50–100 tasks per repo | `mine.MineConfig.pro`, `stats` |
| Pro §3.2 / App. B: problem statement, requirements, interface | `spec` (draft, LLM, custom, human review) |
| Pro §4.1: consecutive commit pairs, test / gold split | `mine.mine_commits`, `patches.split_patch` |
| Pro §4.3: environments, repeated gold runs, flaky tests, human test review | `env`, `validate` (runs × N) |
| Pro §5: SWE-agent scaffold, 50 turns, cost caps | `tools` (SWE-agent's editor + search), `agents.LLMAgent`, `gym.SWEEnv`, `Budget`, `report.budget_curves` |
| Pro §6.2: augmentation ablation | `stats` naming-leak measurement (Results §2) |
| Pro §6.3: LLM-as-a-judge failure buckets | `failure.classify` (deterministic), `failure.llm_judge` |

## Design and security model

- **One schema.** Everything converts into `Task` / `EnvSpec`, and an environment is just data:
  - an image or Dockerfile,
  - a workdir and setup commands,
  - a test command or a per-task run script,
  - a log parser.

  See [docs/DESIGN.md](docs/DESIGN.md).
- **The agent can't see the answer.**
  - Only `problem_statement`, `requirements` and `interface` are visible.
  - Agents have no network by default.
  - The git history is purged after the base commit, and `env.hidden_paths` are deleted.
- **Graders can't easily be gamed.** Grading always runs in a fresh sandbox:
  - The candidate patch is applied with three fallbacks: `git apply`, `--reject`, `patch --fuzz`.
  - Test files, test-dir helpers (unless the reference fix edits them) and runner config are reverted.
  - Paths the hidden tests need are cleared.
  - Hidden tests are applied last.
  - Each run uses random markers, and Pro parsers run outside the sandbox.
  - Real environment problems are `harness_error`, never agent failures.
- **Test names match the official harnesses.**
  - The Python parsers reproduce SWE-bench's naming rules (pytest, pytest-options, pytest-v2, django, sympy, matplotlib, seaborn).
  - Pro tasks use Pro's own scripts.
  - Other parsers cover Go, Jest/Vitest, Mocha, Cargo, JUnit, TRX, .NET, TAP, RSpec, Minitest and GoogleTest. `auto` picks the best.
- **Parallel safe.** File-locked repo cache with atomic clones. Batches resume after interruption.

## Limitations

- **Grading trusts the test process.** Like SWE-bench, code under test that monkeypatches the test framework itself from a source module (for example `_pytest.reports`) is not detected.
- **Generated Dockerfiles are best-effort.** Real repositories often need a hand-written `env.dockerfile`. The dataset presets don't have this problem.
- **Static checks are heuristics.** The naming-leak, interface and definition checks are per-language regexes. Treat their output as drafts and warnings.
- **Rust in-file tests can't be split out.** Unit tests inside Rust source files (`#[cfg(test)]`) stay in the gold patch when a commit is split.
- **The LLM agent is a minimal reference agent.** Use your own agent for leaderboard numbers.
- **Some upstream lists aren't reproducible.** A few upstream FAIL_TO_PASS lists depend on live network services (see Results §4). `validate` reports what is reproducible today.

## Development

```bash
git clone https://github.com/phoenixlibbi/swe-env-kit && cd swe-env-kit
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                  # ~230 tests, no Docker or network needed (~2 min)
.venv/bin/pytest -q -m network      # live HuggingFace / GitHub checks
```

```
swe_env_kit/
  task.py        Task / EnvSpec schema, JSONL io          mine.py       commit-pair and PR miners
  patches.py     diff parsing, split, stats, definitions  spec.py       interface, requirements, writers, review files
  languages.py   test paths, definitions, error patterns  gym.py        SWEEnv, budgets, episodes, batches
  logparsers.py  20+ test-log parsers                     agents.py     nop, oracle, replay, LLM agents
  formats.py     SWE-bench / Pro / generic converters     grade.py      tamper guard, binary + dense reward
  presets.py     official images, eval scripts, parsers   validate.py   pre/post runs, flaky, naming leaks
  hub.py         HuggingFace, Pro scripts, GitHub API     report.py     slices, budget curves
  sandbox.py     Docker and local sandboxes               failure.py    Pro failure buckets (+ LLM judge)
  tools.py       SWE-agent style editor, search, schemas
  workspace.py   checkout, history purge, patch, tests    localize.py   BM25 vs oracle retrieval
  dockerfiles.py default per-language images              trajectory.py episodes, SWE-agent import
  llm.py         OpenAI-compatible client                 demo.py       the original demo repository
```

## References

- Jimenez, Yang, Wettig, Yao, Pei, Press, Narasimhan. *SWE-bench: Can Language Models Resolve Real-World GitHub Issues?* ICLR 2024. [arXiv:2310.06770](https://arxiv.org/abs/2310.06770)
- Deng, Da, et al. (Scale AI). *SWE-Bench Pro: Can AI Agents Solve Long-Horizon Software Engineering Tasks?* 2025. [arXiv:2509.16941](https://arxiv.org/abs/2509.16941) · [data](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) · [harness](https://github.com/scaleapi/SWE-bench_Pro-os)
- Yang et al. *SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering.* NeurIPS 2024.
- Robertson & Zaragoza. *The Probabilistic Relevance Framework: BM25 and Beyond.* 2009.
- Companion project: [terminal-bench-reward-lens](https://github.com/phoenixlibbi/terminal-bench-reward-lens), binary vs dense rewards for Terminal-Bench and Long-Horizon-Terminal-Bench.

## License

MIT
