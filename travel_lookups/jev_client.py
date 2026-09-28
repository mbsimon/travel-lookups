# Vendored copy. Source of truth: ~/.config/michaelsimon/jev_client.py. Fix it there first, then copy it here verbatim.
"""Jev, TypeSafe's typed decision model, reached through OpenRouter.

Jev answers yes/no ("noul"), pick-one ("choice") and 0-to-N ("score")
questions about a piece of text in about 0.3 seconds for about $0.00002 a
call. It cannot write text, do arithmetic or compare dates.

Calls:
    ask(state, questions)            -> {question_id: answer}
    ask_each(states, questions)      -> [answers or None, ...]  (parallel)
    commission_leak(text)            -> LeakVerdict

Every state passes through mask() first, which replaces long digit runs
(card, passport, account and confirmation numbers) before anything leaves.
Client names and ordinary correspondence are sent as they are (Michael,
2026-09-27). Every call is logged: questions asked, milliseconds, cost.

Standard library only, so any repo can vendor it.
Source of truth: ~/.config/michaelsimon/jev_client.py. Fix it there first,
then copy it into each repo verbatim.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("jev")

ENDPOINT = "https://openrouter.ai/api/v1/systemone"
MODEL = "typesafe/jev-1.13"  # pinned; the ~typesafe/jev-latest alias moves
TIMEOUT_S = 10
WORKERS = 8  # OpenRouter allows 1,200 requests a minute
MAX_STATE_CHARS = 60_000  # Jev's window is ~32k tokens with the questions
SECRETS = Path.home() / ".config/michaelsimon/secrets.env"


class JevError(Exception):
    """Jev could not answer: no key, HTTP error, timeout or a bad reply."""


# Card numbers (13-19 digits, spaces or dashes allowed), unbroken runs of 9+
# digits (accounts, IDs), and passport-style runs such as X12345678. Dates,
# phone numbers and prices are left alone.
_CARD = re.compile(r"(?<![\w])(?:\d[ -]?){12,18}\d(?![\w])")
_DIGIT_RUN = re.compile(r"(?<![\w])\d{9,}(?![\w])")
_PASSPORT = re.compile(r"\b[A-Z]{1,2}\d{6,9}\b")


def mask(text: str) -> str:
    """Replace card, passport, account and other long numbers with [number]."""
    for pattern in (_CARD, _DIGIT_RUN, _PASSPORT):
        text = pattern.sub("[number]", text)
    return text


def _mask_state(state):
    if isinstance(state, str):
        return mask(state)[:MAX_STATE_CHARS]
    if isinstance(state, dict):
        return {k: _mask_state(v) for k, v in state.items()}
    if isinstance(state, list):
        return [_mask_state(v) for v in state]
    return state


def _api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        for line in SECRETS.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return None


def available() -> bool:
    return bool(_api_key())


def ask(state, questions: dict, timeout: float = TIMEOUT_S) -> dict:
    """One call. Returns {question_id: answer}; raises JevError."""
    key = _api_key()
    if not key:
        raise JevError("OPENROUTER_API_KEY not set")
    body = json.dumps({"model": MODEL, "state": _mask_state(state),
                       "questions": questions}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        answers = data["answers"]
    except Exception as e:
        log.warning("jev call failed (%s): %s", ",".join(questions), e)
        raise JevError(f"{type(e).__name__}: {e}") from e
    log.info("jev %s: %d ms, $%s", ",".join(questions),
             (time.monotonic() - started) * 1000, (data.get("usage") or {}).get("cost"))
    return answers


def ask_each(states: list, questions: dict, max_fail_share: float = 0.1) -> list:
    """Ask the same questions about each state in parallel.

    Returns one answers dict per state, in order, with None where a call
    failed. Raises JevError when more than max_fail_share of calls fail, so a
    caller can fall back to its old method instead of trusting a partial run.
    """
    if not available():
        raise JevError("OPENROUTER_API_KEY not set")

    def one(state):
        try:
            return ask(state, questions)
        except JevError:
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        answers = list(pool.map(one, states))
    failed = sum(a is None for a in answers)
    if states and failed / len(states) > max_fail_share:
        raise JevError(f"{failed}/{len(states)} Jev calls failed")
    return answers


# ---------------------------------------------------------------------------
# Commission and trade-language leak check for client-facing text
# ---------------------------------------------------------------------------

LEAK_WARN_P = 0.5  # at or above this, show a warning on the draft

LEAK_QUESTIONS = {
    "leak": {
        "type": "noul",
        "instructions": (
            "This is text a travel advisor is about to send to a client. Does it "
            "reveal back-of-house trade information the client should never see: "
            "the advisor's commission or earnings, net, trade, agent-only or "
            "industry rates, the agency's IATA number, markups, or how much the "
            "advisor makes from a supplier?"
        ),
        "criteria": {
            "true": "Mentions or clearly hints at commission, earnings, net or "
                    "trade rates, markups, or the IATA number",
            "false": "Only client-facing information: prices the client pays, "
                     "perks and amenities, dates, rooms, logistics",
        },
    },
    "kind": {
        "type": "choice",
        "instructions": "Which kind of trade information, if any, does the text reveal?",
        "criteria": {
            "commission": "The advisor's commission, earnings or what they make",
            "trade_rate": "Net, trade, agent-only, industry or wholesale rates, or markups",
            "iata": "The agency's IATA or agency ID number",
            "none": "No trade information",
        },
    },
}


@dataclass
class LeakVerdict:
    p: float          # probability the text reveals trade information
    kind: str         # commission | trade_rate | iata | none
    warn: bool        # p >= LEAK_WARN_P

    def message(self) -> str:
        label = {"commission": "commission or earnings", "trade_rate": "trade or net rates",
                 "iata": "the IATA number"}.get(self.kind, "trade information")
        return f"Possible {label} in client-facing text (Jev {self.p:.2f}). Check before sending."


def commission_leak(text: str) -> LeakVerdict:
    """Judge client-facing text for trade information. Raises JevError."""
    a = ask(text, LEAK_QUESTIONS)
    p = float(a["leak"]["noul"])
    return LeakVerdict(p=p, kind=a["kind"]["choice"], warn=p >= LEAK_WARN_P)
