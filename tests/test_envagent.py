import json

from swe_env_forge.envagent import check_proposal, setup_environment, summary
from swe_env_forge.task import Task
from swe_env_forge.validate import ValidationResult

BASE = "a" * 40
DF = f"FROM python:3.11\nRUN git clone -q https://x/r /testbed; cd /testbed && git checkout -q {BASE}\n"


def task():
    return Task.from_dict({"instance_id": "o__r-1", "repo": "o/r", "base_commit": BASE, "language": "python",
                           "env": {"dockerfile": DF, "test_command": "python -m pytest -rA {test_files}",
                                   "log_parser": "pytest"}})


class Client:
    model = "fake"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def chat(self, messages, **kw):
        self.prompts.append(messages)
        return self.replies.pop(0), None


def checker(*verdicts):
    seq = list(verdicts)

    def check(t):
        v = seq.pop(0)
        return ValidationResult(t.instance_id, verdict=v, reasons=[] if v == "valid" else ["environment_incomplete:freezegun"],
                                fail_to_pass=["t::a"] if v == "valid" else [],
                                log_tails={"post0": "ModuleNotFoundError: No module named 'freezegun'"})
    return check


def test_agent_repairs_until_valid():
    fixed = DF + "RUN pip install freezegun\n"
    client = Client([json.dumps({"dockerfile": fixed, "test_command": "python -m pytest -rA {test_files}",
                                 "log_parser": "pytest", "explanation": "install the missing test dependency"})])
    t, attempts = setup_environment(task(), client, build=lambda t: "ok", check=checker("invalid", "valid"))
    assert [a.outcome for a in attempts] == ["invalid", "valid"]
    assert "freezegun" in t.env.dockerfile and t.fail_to_pass == ["t::a"]
    assert t.metadata["env_setup"]["valid"] is True
    sent = client.prompts[0][1]["content"]
    assert "No module named 'freezegun'" in sent and "Current Dockerfile" in sent
    assert summary(attempts) == "invalid(environment_incomplete:freezegun) -> valid"


def test_agent_handles_build_failures_and_bad_proposals():
    bad = json.dumps({"dockerfile": "FROM python:3.11\nRUN echo no checkout\n", "test_command": "pytest"})
    good = json.dumps({"dockerfile": DF.replace("3.11", "3.10"), "test_command": "python -m pytest -rA {test_files}"})
    calls = {"n": 0}

    def build(t):
        calls["n"] += 1
        if "3.10" not in t.env.dockerfile:
            raise RuntimeError("docker build failed: no matching distribution")
        return "ok"

    t, attempts = setup_environment(task(), Client([bad, good]), build=build, check=checker("valid"), max_attempts=4)
    outcomes = [a.outcome for a in attempts]
    # the bad reply is sent back with the reason; the corrected one is used without an extra rebuild
    assert outcomes == ["build_failed", "rejected_proposal", "valid"]
    assert attempts[1].reasons == ["dockerfile must still check out the base commit"]


def test_without_a_model_it_only_reports():
    t, attempts = setup_environment(task(), None, build=lambda t: "ok", check=checker("invalid"))
    assert [a.outcome for a in attempts] == ["invalid"] and t.metadata["env_setup"]["valid"] is False


def test_check_proposal_rules():
    t = task()
    assert check_proposal(t, {"dockerfile": DF, "test_command": "pytest {test_files}"}) == ""
    assert "FROM" in check_proposal(t, {"dockerfile": "RUN x", "test_command": "pytest"})
    assert "parser" in check_proposal(t, {"dockerfile": DF, "test_command": "x {test_files}", "log_parser": "nope"})
    assert "placeholder" in check_proposal(t, {"dockerfile": DF, "test_command": "pytest"})


def test_markdown_replies_from_smaller_models_are_accepted():
    from swe_env_forge.envagent import parse_proposal

    reply = ("Sure! The tests need freezegun.\n```dockerfile\n" + DF + "RUN pip install freezegun\n```\n"
             "TEST_COMMAND: `python -m pytest -rA {test_files}`\n")
    p = parse_proposal(reply, task())
    assert p["dockerfile"].startswith("FROM python:3.11") and "freezegun" in p["dockerfile"]
    assert p["test_command"] == "python -m pytest -rA {test_files}"
    assert parse_proposal("no code here", task()) is None
    fixed = DF + "RUN pip install freezegun\n"
    t, attempts = setup_environment(task(), Client(["I think you should install it.", "```dockerfile\n" + fixed + "```"]),
                                    build=lambda t: "ok", check=checker("invalid", "valid"))
    assert [a.outcome for a in attempts] == ["invalid", "rejected_proposal", "valid"]
