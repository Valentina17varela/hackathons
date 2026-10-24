"""Thin wrappers that record how the pipeline used the LLM and the intent fallback.

``MeteredClient`` attributes every answer (live or cached) to a purpose label
(``request_intent``, ``image_extraction``) so the usage report can break the
totals down. ``CountingClassifier`` counts how many request texts were decided
by the model and how many by the keyword fallback.
"""

from __future__ import annotations

import copy
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from evaluator import SOURCE_RULES, IntentClassifier, IntentDecision
from llm import LLMClient, LLMRequest, LLMResponse

T = TypeVar("T")


@dataclass(slots=True)
class PurposeUsage:
    """Answers attributed to one purpose label."""

    answers: int = 0
    live_answers: int = 0
    cached_answers: int = 0
    input_tokens: int = 0  # live and cached answers, at their original cost
    output_tokens: int = 0

    def add(self, response: LLMResponse) -> None:
        self.answers += 1
        if response.cached:
            self.cached_answers += 1
        else:
            self.live_answers += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens


class MeteredClient(LLMClient):
    """``LLMClient`` that also keeps per-purpose usage."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._purpose_lock = threading.Lock()
        self._by_purpose: dict[str, PurposeUsage] = {}

    def complete(
        self,
        request: LLMRequest,
        accept: Callable[[str], T] | None = None,
    ) -> tuple[LLMResponse, T | None]:
        response, parsed = super().complete(request, accept)
        label = request.purpose.split(":", 1)[0] or "unlabeled"
        with self._purpose_lock:
            self._by_purpose.setdefault(label, PurposeUsage()).add(response)
        return response, parsed

    def purpose_snapshot(self) -> dict[str, PurposeUsage]:
        with self._purpose_lock:
            return copy.deepcopy(self._by_purpose)


class CountingClassifier(IntentClassifier):
    """``IntentClassifier`` that tallies the decision source of every text."""

    def __init__(self, client: LLMClient | None = None) -> None:
        super().__init__(client)
        self.sources: Counter[str] = Counter()
        self.rule_texts: set[str] = set()

    def classify(self, request_text: str) -> IntentDecision:
        decision = super().classify(request_text)
        self.sources[decision.source] += 1
        if decision.source == SOURCE_RULES:
            self.rule_texts.add(request_text)
        return decision
