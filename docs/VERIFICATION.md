# Verification log

Every claim in the README is backed by a check that was actually run. Unit and integration tests run in CI (`pytest -q`, no Docker or network). The live checks below ran on an x86-64 Mac with Docker, against the benchmarks' **official** images and data. They are kept out of CI because they download several GB.

## 1. Grading parity with SWE-bench, every repository type

For each repository in SWE-bench Lite, one task was graded with the reference patch (`--gold --expect resolved`) and with an empty patch (`--nop --expect unresolved`), in the task's official image, using the test command and log parser from the dataset's own `eval_script`.

| Repository | Log parser | Gold patch | Empty patch |
|---|---|---|---|
| astropy | pytest-v2 | resolved | not resolved |
| django | django | resolved | not resolved |
| matplotlib | matplotlib | resolved | not resolved |
| seaborn | seaborn | resolved | not resolved |
| flask | pytest | resolved | not resolved |
| requests | pytest-options | see note | not resolved |
| xarray | pytest | resolved | not resolved |
| pylint | pytest-options | resolved | not resolved |
| pytest | pytest | resolved | not resolved |
| scikit-learn | pytest-v2 | resolved | not resolved |
| sphinx | pytest-v2 (via tox) | resolved | not resolved |
| sympy | sympy (`bin/test`) | resolved | not resolved |

**Bug found and fixed (sphinx).**
- **What happened:** official SWE-bench images carry one extra commit on top of the base, authored "SWE-bench", with environment tweaks. For sphinx that commit adds `pytest -rA` to `tox.ini`. Resetting the sandbox to `base_commit` threw the tweak away, so pytest printed no result lines.
- **The fix:** SWE-bench tasks now start from the image's HEAD (`env.start_ref = "image-head"`), and that HEAD is checked: it must descend from the base by at most five commits, otherwise the task is refused as a possible leak.
- **Regression tests:** `tests/test_hardening.py::test_image_head_*`.

**Note on requests.** `psf__requests-2317` has dataset F2P tests that call httpbin.org over the live network. Today they pass before the fix too, so the official list is not reproducible. `validate` reports what *is* reproducible: 1 F2P / 141 P2P.

## 2. Test lists match the official datasets

`sweforge validate` re-derives FAIL_TO_PASS / PASS_TO_PASS by running the tests before and after the fix:

| Task | Ours vs dataset |
|---|---|
| `pytest-dev__pytest-5227` | identical (3 / 34) |
| `django__django-11099` | P2P ⊇ dataset; one dataset F2P test passes before the fix in the official image |
| SWE-bench Pro `openlibrary-00bec1e7` (Python) | identical (5 / 15), using Pro's own `run_script.sh` + `parser.py` |
| SWE-bench Pro `navidrome-0130c6dc` (Go) | identical (1 / 0) |

## 3. Predictions run through the official SWE-bench harness

The predictions file written by `sweforge run` (oracle agent, django-11099 and pytest-5227) was evaluated by the official `swebench` package (v5.0.2, `python -m swebench.harness.run_evaluation`). Result: **2 submitted, 2 completed, 2 resolved, 0 errors**.

### The official harness accepts a fake fix that sweforge rejects

The same harness was given attack patches for `pytest-dev__pytest-5227`. None of them fixes the bug:

| Attack patch | Official SWE-bench harness | sweforge |
|---|---|---|
| Adds `conftest.py` files whose pytest hook marks every test as passed | **resolved** | not resolved: the conftest files are reverted and flagged |
| Overwrites the hidden test file with an empty test | unresolved | not resolved: the file is reverted and flagged |
| Adds a `-p` plugin to `tox.ini`'s pytest `addopts` | unresolved | `tampering` |

The official harness restores the hidden test files but not test-runner configuration, so a `conftest.py` hook turns a non-solution into a pass. For RL this is a live reward hack: a policy can learn it.

## 4. Attacks inside a real container

These ran as agent episodes on `pytest-dev__pytest-5227`, in its official image:
- **Network.** `urllib` to pypi.org and `git fetch` from GitHub both fail: DNS is off. Agents are offline by default.
- **History.** `git log --all` shows 10,159 commits, all ancestors of the start. `git fsck --unreachable` and `git reflog` show nothing, and the stash is empty.
- **Scratch files.** `/tmp/sweforge` holds only the agent's own `cwd` and `step.sh` files. The per-run test scripts and markers are created only at grading time, in a fresh container.

## 5. Mining and environments on real repositories

- **PR mining** (`mine --mode prs`) on `pallets/itsdangerous`, GitHub API, 150 PRs: 1 task. 93 PRs had no linked issue and 3 had no test changes. This matches the heavy filtering SWE-bench reports.
- **Commit mining** (`mine --pro`) on the same repo: 4 Pro-eligible tasks out of 91 commits, with generated Dockerfiles.
- **Demo repo in Docker.** Images are built from a bundle of the base history only: the image holds 1 commit and no bundle file. `validate --backend docker` reproduces the local results exactly, including the flaky-test detection.
- **Full pipeline on a real repository** (`pallets/itsdangerous`): `mine`, then `env --build`, `validate --runs 2`, `spec`, `validate`, and finally `grade --gold --expect resolved` and `--nop --expect unresolved`.

  | Candidate | Verdict |
  |---|---|
  | PR #378 (FIPS builds without SHA-1) | valid: F2P = the 2 `test_iter_unsigners` cases, 39 P2P; gold resolves, empty patch fails |
  | Its commit-pair twin | identical result: both mining methods find the same fix |
  | "Drop end-of-life Python versions" | `no_fail_to_pass`: not a behaviour change |
  | "Update project files" | `no_fail_to_pass` |
  | A merge commit | `environment_incomplete:freezegun`: the generated environment lacks a test dependency |

  Three weaknesses surfaced here and were fixed:
  1. **Masked F2P tests.** The new test file imports a name the fix creates (`_lazy_sha1`), so before the fix the whole file failed to import, and all 41 tests looked like fail-to-pass. An agent would have earned most of the dense reward just by defining the name. Validation now runs the tests once more at the start commit without the test patch. Tests that already passed there, and whose bodies the test patch did not change, move to P2P.
  2. **Missing dependencies were misdiagnosed** as `no_fail_to_pass`. They are now reported as `environment_incomplete:<module>`.
  3. **Private names.** The interface drafter skipped leading-underscore names even when the hidden tests import them, so the naming leak could not be fixed. Such names are now drafted.

## 6. Real agent trajectories

38 real SWE-agent 1.x trajectories (private, not committed) were imported with `sweforge import-traj`. Two bugs surfaced and were fixed:
- **Overwrites.** Several runs of the same task overwrote each other; repeated attempts are now kept as `<id>@2`, `@3`, ...
- **Ungraded runs.** They were reported as 0% resolved; they are now listed separately and never counted as failures.
