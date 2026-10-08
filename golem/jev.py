"""Jev, TypeSafe's typed decision model, reached through OpenRouter's Decisions API
(POST /api/alpha/decisions, the request `openrouter.alpha.decisions.create` sends).
The pinned `openrouter` SDK predates that method, so this is plain HTTPS.

Jev is advisory and can only make Golem more conservative. It may defer a build
once per tool name when an installed tool probably covers the need, or when the
task probably does not need a new tool. It never approves an install, never picks
which tool to call, and its absence or failure changes nothing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
COVERED_ABOVE = 0.85
NEEDED_BELOW = 0.15


@dataclass
class Advice:
    defer: bool = False
    reason: str = ""
    probabilities: dict = field(default_factory=dict)
    model: str = ""
    cost: float = 0.0
    error: str = ""
    request_id: str = ""

    def line(self) -> str:
        if self.error:
            return f"Jev: no advice ({self.error})"
        probs = ", ".join(
            f"{key}={value:.2f}" for key, value in self.probabilities.items()
        )
        verdict = f"defer: {self.reason}" if self.defer else "no objection"
        return f"Jev {self.model}: {probs} -> {verdict}"


def advise_gap(
    api_key: str,
    model: str,
    task: str,
    proposal: dict,
    installed: list[dict],
    timeout: float = 20.0,
) -> Advice:
    state = {
        "task": task[:6000],
        "proposed_tool": {
            key: proposal.get(key) for key in ("name", "description", "access")
        },
        "gap": proposal.get("gap", {}),
        "installed_tools": [
            {"name": row["name"], "description": row["description"]}
            for row in installed
        ],
    }
    questions = {
        "needed": {
            "type": "noul",
            "instructions": "Doing `task` needs a capability like `proposed_tool.description` that reading files and the tools in `installed_tools` do not already give.",
        },
    }
    if installed:
        questions["covered"] = {
            "type": "noul",
            "instructions": "One of `installed_tools` already does what `proposed_tool.description` describes, so `task` could use it instead of a new tool.",
        }
    try:
        response = _post(
            api_key, {"model": model, "state": state, "questions": questions}, timeout
        )
    except Exception as exc:  # Jev is advisory: any failure means no advice
        return Advice(error=f"{type(exc).__name__}: {str(exc)[:160]}")
    answers = response.get("answers") or {}
    probs = {}
    for key in questions:
        value = (answers.get(key) or {}).get("noul")
        if isinstance(value, (int, float)) and 0.0 <= value <= 1.0:
            probs[key] = float(value)
    usage = response.get("usage") or {}
    advice = Advice(
        probabilities=probs,
        model=str(response.get("model") or model),
        cost=float(usage.get("cost") or 0.0),
        request_id=str(response.get("id") or ""),
    )
    if len(probs) != len(questions):
        advice.error = f"Jev answered {len(probs)} of {len(questions)} questions"
        return advice
    if probs.get("covered", 0.0) >= COVERED_ABOVE:
        advice.defer, advice.reason = (
            True,
            "an installed tool probably covers this; try it, or say in gap.why_existing_insufficient why not",
        )
    elif "needed" in probs and probs["needed"] <= NEEDED_BELOW:
        advice.defer, advice.reason = (
            True,
            "the task probably does not need a new tool; say in gap.why_needed what is missing",
        )
    return advice


def _post(api_key: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
        ) from None
