from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping


_NEGATORS = frozenset(
    {
        "avoid",
        "excluding",
        "exclude",
        "except",
        "no",
        "not",
        "omit",
        "skip",
        "without",
    }
)

_CONTRACTIONS = {
    "can't": "can not",
    "cannot": "can not",
    "don't": "do not",
    "doesn't": "does not",
    "isn't": "is not",
    "mustn't": "must not",
    "shouldn't": "should not",
    "won't": "will not",
    "wouldn't": "would not",
}


@dataclass(frozen=True)
class IntentSignalMatch:
    signal: str
    phrase: str
    polarity: str
    token_index: int
    negator: str = ""


@dataclass(frozen=True)
class IntentPolarity:
    positive: frozenset[str]
    negative: frozenset[str]
    contradictory: frozenset[str]
    matches: tuple[IntentSignalMatch, ...]

    def evidence(self) -> dict:
        return {
            "positive": sorted(self.positive),
            "negative": sorted(self.negative),
            "contradictory": sorted(self.contradictory),
            "matches": [
                {
                    "signal": match.signal,
                    "phrase": match.phrase,
                    "polarity": match.polarity,
                    "token_index": match.token_index,
                    "negator": match.negator,
                }
                for match in self.matches
            ],
        }


def analyze_intent_polarity(
    text: str,
    *,
    signals: Mapping[str, Iterable[str]],
    negation_window: int = 4,
) -> IntentPolarity:
    tokens = _intent_tokens(text)
    matches: list[IntentSignalMatch] = []

    for signal, phrases in signals.items():
        for phrase in phrases:
            phrase_tokens = _intent_tokens(phrase)
            if not phrase_tokens:
                continue
            width = len(phrase_tokens)
            for index in range(0, len(tokens) - width + 1):
                if tokens[index : index + width] != phrase_tokens:
                    continue
                negator = _negator_before(tokens, index=index, window=negation_window)
                matches.append(
                    IntentSignalMatch(
                        signal=str(signal),
                        phrase=" ".join(phrase_tokens),
                        polarity="negative" if negator else "positive",
                        token_index=index,
                        negator=negator,
                    )
                )

    positive = frozenset(match.signal for match in matches if match.polarity == "positive")
    negative = frozenset(match.signal for match in matches if match.polarity == "negative")
    return IntentPolarity(
        positive=positive,
        negative=negative,
        contradictory=positive & negative,
        matches=tuple(matches),
    )


def _intent_tokens(text: str) -> list[str]:
    normalized = str(text or "").casefold()
    for contraction, expansion in _CONTRACTIONS.items():
        normalized = normalized.replace(contraction, expansion)
    return re.findall(r"[\w]+", normalized, flags=re.UNICODE)


def _negator_before(tokens: list[str], *, index: int, window: int) -> str:
    start = max(0, index - max(1, int(window)))
    preceding = tokens[start:index]
    for token in reversed(preceding):
        if token in _NEGATORS:
            return token
    return ""
