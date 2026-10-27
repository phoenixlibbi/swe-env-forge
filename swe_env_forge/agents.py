"""Reference agents: nop, oracle, replay and an LLM agent for OpenAI-compatible endpoints.

An agent implements ``reset(observation)`` and ``act(last_output) -> action``. Add your
own by passing ``--agent module.path:ClassName`` to ``sweforge run``; the class is called
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
from .tools import TOOL_SCHEMAS, action_from_tool_call


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
    _PATH = ".sweforge_oracle.diff"

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

    Sources: a swe-env-forge episode folder or ``trajectory.jsonl``, a JSON list, or a text
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


TOOLS_PROMPT = """You are an expert software engineer working autonomously in a sandboxed repository.
{instructions}

Work like a careful engineer: explore with search, find_file and str_replace_editor view; reproduce the
problem; make focused edits with str_replace_editor; run the relevant tests with bash; then call submit.
Call exactly one tool per step. Long outputs are truncated, so view files in ranges and narrow searches."""


class LLMAgent:
    """A tool-using agent in the style of SWE-agent, for any OpenAI-compatible model.

    * ``mode="tools"`` (default): native function calling with bash, str_replace_editor, search,
      find_file and submit (see :mod:`swe_env_forge.tools`).
    * ``mode="text"``: for models without function calling; one ```bash block per reply, or SUBMIT.

    Context management follows SWE-agent: the last ``keep_observations`` tool outputs stay in
    full and older ones are collapsed to a one-line note, so long episodes do not overflow.
    """

    def __init__(self, task: Task, client: ChatClient, mode: str = "tools", keep_observations: int = 5,
                 max_format_errors: int = 3, history_chars: int = 400_000):
        if mode not in ("tools", "text"):
            raise ValueError("mode must be 'tools' or 'text'")
        self.task, self.client, self.mode = task, client, mode
        self.name = f"llm:{client.model}"
        self.keep_observations, self.max_format_errors, self.history_chars = keep_observations, max_format_errors, history_chars
        self.messages: list[dict[str, Any]] = []
        self.queue: list[tuple[str, dict[str, Any]]] = []
        self.pending_id: str | None = None
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
        prompt = TOOLS_PROMPT if self.mode == "tools" else SYSTEM_PROMPT
        self.messages = [{"role": "system", "content": prompt.format(instructions=observation["instructions"])},
                         {"role": "user", "content": "\n\n".join(parts)}]
        self.queue, self.pending_id, self.format_errors = [], None, 0

    # ------------------------------------------------------------ context
    def _is_observation(self, m: dict[str, Any]) -> bool:
        return m["role"] == "tool" or (m["role"] == "user" and str(m.get("content", "")).startswith("OUTPUT:"))

    def _collapse(self) -> None:
        obs = [i for i, m in enumerate(self.messages) if self._is_observation(m)]
        for i in obs[:-self.keep_observations] if self.keep_observations > 0 else obs:
            content = str(self.messages[i].get("content", ""))
            if not content.startswith("[collapsed"):
                self.messages[i]["content"] = f"[collapsed: old output, {content.count(chr(10)) + 1} lines]"
        while sum(len(str(m.get("content") or "")) for m in self.messages) > self.history_chars and len(self.messages) > 4:
            del self.messages[2]
            while len(self.messages) > 2 and self.messages[2]["role"] == "tool":
                del self.messages[2]  # never leave a tool result without its call

    # -------------------------------------------------------------- acting
    def act(self, last_output: str | None) -> dict[str, Any]:
        if last_output is not None:
            if self.pending_id is not None:
                self.messages.append({"role": "tool", "tool_call_id": self.pending_id, "content": last_output})
            else:
                self.messages.append({"role": "user", "content": f"OUTPUT:\n{last_output}"})
            self.pending_id = None
        if self.queue:
            self.pending_id, action = self.queue.pop(0)
            return action
        return self._think()

    def _think(self) -> dict[str, Any]:
        spent = 0.0  # every model call is paid for, including replies that had to be retried
        for _ in range(self.max_format_errors + 1):
            self._collapse()
            if self.mode == "tools":
                message, usage = self.client.complete(self.messages, tools=TOOL_SCHEMAS)
            else:
                text, usage = self.client.chat(self.messages)
                message = {"role": "assistant", "content": text}
            spent += usage.cost if usage else 0.0
            cost = spent
            self.messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
            calls = message.get("tool_calls") or []
            if calls:
                for c in calls:
                    fn = c.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = None
                    action = (action_from_tool_call(fn.get("name", ""), args) if isinstance(args, dict)
                              else {"type": "invalid_tool_arguments", "raw": str(fn.get("arguments"))[:200]})
                    action["thought"] = (message.get("content") or "").strip()[:2000]
                    self.queue.append((c.get("id", ""), action))
                self.pending_id, action = self.queue.pop(0)
                action["cost"] = cost
                return action
            action = parse_reply(message.get("content") or "")
            action["cost"] = cost
            if action["type"] != "format_error":
                return action
            self.format_errors += 1
            if self.format_errors > self.max_format_errors:
                break
            hint = ("Call one of the tools (bash, str_replace_editor, search, find_file) or submit."
                    if self.mode == "tools" else "Reply with THOUGHT and one ```bash block, or SUBMIT.")
            self.messages.append({"role": "user", "content": hint})
        raise LLMError("too many replies without a tool call, bash block or SUBMIT")


def load_agent_class(spec: str):
    """Import ``module.path:ClassName``."""
    mod, _, cls = spec.partition(":")
    if not mod or not cls:
        raise ValueError("custom agents are given as module.path:ClassName")
    return getattr(importlib.import_module(mod), cls)
