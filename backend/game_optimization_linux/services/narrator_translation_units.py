"""Lossless, bounded MT units using the already bundled Moses prefix tables.

This is a lightweight rule-based SBD, not punctuation restoration. Short ASR
without punctuation stays intact; long input is bounded at word boundaries.
No speech, repetition or negation is removed. Ambiguous abbreviations remain
heuristic; model-level omissions cannot be detected by segmentation alone.
"""
from __future__ import annotations

from functools import lru_cache
import re

MAX_UNIT_WORDS = 40
MAX_UNIT_CHARS = 500
MAX_UNIT_TOKENS = 96


@lru_cache(maxsize=1)
def _prefixes() -> frozenset[str]:
    from sacremoses.corpus import NonbreakingPrefixes

    corpus = NonbreakingPrefixes()
    return frozenset(word.split()[0].casefold() for lang in ('en', 'es')
                     for word in corpus.words(lang))


def _period_break(text: str, position: int, following: str) -> bool:
    before = text[:position]
    match = re.search(r'([\w.]+)$', before)
    token = match.group(1) if match else ''
    lower = token.casefold()
    next_word = re.search(r'\w+', following)
    next_text = next_word.group(0) if next_word else ''
    # A decimal, URL or e-mail has no whitespace after its internal dot, so
    # never reaches this function. Titles and initials are nonterminal.
    if lower in {'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'sra', 'srta', 'dra'}:
        return False
    if len(token) == 1 and token.isalpha():
        return False
    if lower in {'e.g', 'i.e'}:
        return False
    # Acronyms and clock abbreviations may actually finish a sentence.
    starters = {'i', 'he', 'she', 'it', 'we', 'they', 'you', 'the', 'this', 'that',
                'do', 'don', 'no', 'then', 'please', 'but', 'el', 'la', 'yo',
                'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'sra', 'srta', 'dra'}
    if '.' in token:
        return next_text.casefold() in starters
    if lower == 'st':
        return next_text.casefold() in starters
    if lower in _prefixes():
        return False
    return True


def _bound_words(unit: str) -> list[str]:
    words = unit.split()
    units: list[str] = []
    current: list[str] = []
    for word in words:
        if current and (len(current) >= MAX_UNIT_WORDS
                        or len(' '.join(current)) + len(word) + 1 > MAX_UNIT_CHARS):
            units.append(' '.join(current))
            current = []
        current.append(word)
    if current:
        units.append(' '.join(current))
    return units


def sentence_units(text: str) -> list[str]:
    """Return ordered units; only whitespace may be normalized, never content."""
    sentences: list[str] = []
    for paragraph in text.splitlines():
        start = 0
        for match in re.finditer(r'[.!?…]+[\"\u201d\u2019\u00bb\')\]]*(?=\s|$)', paragraph):
            following = paragraph[match.end():].lstrip()
            mark = match.group(0)
            boundary = True
            if mark.startswith('.') and not mark.startswith('..'):
                boundary = _period_break(paragraph, match.start(), following)
            # Keep a reporting clause with its quoted speech.
            if any(c in mark for c in ('"', '\u201d', "'", '\u2019')) and following:
                if re.match(r'(?:he|she|they|i|we)\s+(?:said|asked|shouted|whispered)\b', following):
                    boundary = False
            if boundary:
                fragment = paragraph[start:match.end()].strip()
                if fragment:
                    sentences.extend(_bound_words(fragment))
                start = match.end()
        remainder = paragraph[start:].strip()
        if remainder:
            sentences.extend(_bound_words(remainder))
    # Fail closed if a later rule change loses or duplicates non-whitespace.
    if ''.join(text.split()) != ''.join(''.join(s.split()) for s in sentences):
        raise RuntimeError('Translation unit segmentation changed source content')
    return sentences