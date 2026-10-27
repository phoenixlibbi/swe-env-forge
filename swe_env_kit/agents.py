"""Reference agents: nop, oracle, replay and an LLM agent for OpenAI-compatible endpoints.

An agent implements ``reset(observation)`` and ``act(last_output) -> action``. Add your
own by passing ``--agent module.path:ClassName`` to ``swekit run``; the class is called
with the task as its only argument.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any

from .llm import ChatClient, LLMError
from .task import Task


class NopAgent:
    """Submits immediately. Its score is the floor: a valid task must give it zero."""

    name = "nop"

    def __init__(self, task: Task | None = None):
        pass

    def reset(self, observation: dict[str, Any]) -> None:
        pass

    def act(self, last_output: str | None) -> dict[str, Any]:
        return {"type": "submit"}


class OracleAgent:
    """Applies the gold patch through the normal action interface. Its score is the ceiling."""

    name = "oracle"
    _PATH = ".swekit_oracle.diff"

    def __init__(self, task: Task):
        self.task = task
        self._plan: list[dict[str, Any]] = []

    def reset(self, observation: dict[str, Any]) -> None:
        self._plan = [
            {"type": "write_file", "path": self._PATH, "content": self.task.gold_patch},
            {"type": "bash", "command": f"git apply --whitespace=nowarn {self._PATH} || "
                                        f"patch --batch --fuzz=5 -p1 -i {self._PATH}; rm -f {self._PATH}"},
            {"type": "submit"},
        ]

    def act(self, last_output: str | None) -> dict[str, Any]:
        return self._plan.pop(0) if self._plan else {"type": "submit"}


class ReplayAgent:
    """Replays a fixed list of actions (bash strings or action dicts), then submits.

    Sources: a swe-env-kit episode folder or ``trajectory.jsonl``, a JSON list, or a text
    file with one command per line.
    """

    name = "replay"

    def __init__(self, actions: list[dict[str, Any] | str]):
        self.actions = [a if isinstance(a, dict) else {"type": "bash", "command": a} for a in actions]
        self._i = 0

    @classmethod
    def from_file(cls, path: str | Path) -> "ReplayAgent":
        path = Path(path)
        if path.is_dir():
            path = path / "trajectory.jsonl"
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            return cls([json.loads(ln)["action"] for ln in text.splitlines() if ln.strip()])
        if path.suffix == ".json":
            return cls(json.loads(text))
        return cls([ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")])

    def reset(self, observation: dict[str, Any]) -> None:
        self._i = 0

    def act(self, last_output: str | None) -> dict[str, Any]:
        if self._i >= len(self.actions):
            return {"type": "submit"}
        a = self.actions[self._i]
        self._i += 1
        return a


SYSTEM_PROMPT = """You are an expert software engineer working autonomously in a sandboxed repository.
{instructions}

Reply in this format every turn:
THOUGHT: one or two sentences about what you will do next.
```bash
<exactly one shell command>
```
To edit files use shell tools (sed, python, cat with a heredoc, ...). When the task is resolved and you have
checked your change, reply with a THOUGHT and the single word SUBMIT on its own line instead of a command."""

_BASH_BLOCK = re.compile(r"```(?:bash|sh|shell)?\s*\n(.*?)```", re.S)
_SUBMIT = re.compile(r"^\s*SUBMIT\s*$", re.M)


def parse_reply(text: str) -> dict[str, Any]:
    thought = ""
    m = re.search(r"THOUGHT:\s*(.*?)(?:```|^\s*SUBMIT\s*$|\Z)", text, re.S | re.M)
    if m:
        thought = m.group(1).strip()
    blocks = _BASH_BLOCK.findall(text)
    if blocks:
        return {"type": "bash", "command": blocks[-1].strip(), "thought": thought}
    if _SUBMIT.search(text):
        return {"type": "submit", "thought": thought}
    return {"type": "format_error", "thought": thought}


class LLMAgent:
    """A minimal ReAct-style agent: one bash command per turn, ``SUBMIT`` when done."""

    def __init__(self, task: Task, client: ChatClient, max_format_errors: int = 3, history_chars: int = 400_000):
        self.task, self.client = task, client
        self.name = f"llm:{client.model}"
        self.max_format_errors, self.history_chars = max_format_errors, history_chars
        self.messages: list[dict[str, str]] = []
        self.format_errors = 0

    def reset(self, observation: dict[str, Any]) -> None:
        t = observation["task"]
        parts = [f"<problem_statement>\n{t['problem_statement']}\n</problem_statement>"]
        if t.get("requirements"):
            parts.append(f"<requirements>\n{t['requirements']}\n</requirements>")
        if t.get("interface"):
            parts.append(f"<interface>\n{t['interface']}\n</interface>")
        if t.get("hints"):
            parts.append(f"<hints>\n{t['hints']}\n</hints>")
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT.format(instructions=observation["instructions"])},
                         {"role": "user", "content": "\n\n".join(parts)}]
        self.format_errors = 0

    def _trim(self) -> None:
        # keep the system prompt and task, drop the oldest exchanges when the history grows too large
        while sum(len(m["content"]) for m in self.messages) > self.history_chars and len(self.messages) > 4:
            del self.messages[2:4]

    def act(self, last_output: str | None) -> dict[str, Any]:
        if last_output is not None:
            self.messages.append({"role": "user", "content": f"OUTPUT:\n{last_output}"})
        self._trim()
        text, usage = self.client.chat(self.messages)
        self.messages.append({"role": "assistant", "content": text})
        action = parse_reply(text)
        action["cost"] = usage.cost if usage else 0.0
        if action["type"] == "format_error":
            self.format_errors += 1
            if self.format_errors > self.max_format_errors:
                raise LLMError("too many replies without a bash block or SUBMIT")
            # a turn is spent; tell the model how to reply
            return {"type": "bash", "command": "echo 'Reply with THOUGHT and one ```bash block, or SUBMIT.'",
                    "cost": action["cost"], "thought": action.get("thought", "")}
        return action


def load_agent_class(spec: str):
    """Import ``module.path:ClassName``."""
    mod, _, cls = spec.partition(":")
    if not mod or not cls:
        raise ValueError("custom agents are given as module.path:ClassName")
    return getattr(importlib.import_module(mod), cls)
