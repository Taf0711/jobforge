"""Section-aware retrieval over the complete job description.

The deterministic scorer only needs requirement rows, but an agent needs the
whole posting: culture language, preferred qualifications, visa wording, the
actual context around a keyword, and sections that no regex parser recognized.
This module keeps every part of the description addressable and ranks chunks
with a small local BM25-style index. It has no model, network, or dependency
requirement.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .jd import _heading, promote_inline_headings

DEFAULT_CHUNK_CHARS = 900
DEFAULT_CHUNK_OVERLAP = 120
MAX_EXCERPT_CHARS = 1_400
DEFAULT_LIMIT = 5
MAX_LIMIT = 10

SECTION_PRIOR = {
    "requirements": 1.25,
    "preferred": 1.15,
    "responsibilities": 1.05,
    "other": 0.95,
    "body": 1.0,
}

# Small, explicit query expansions for the questions an agent actually asks.
# They are retrieval hints, not facts; the returned text is still the evidence.
QUERY_EXPANSIONS = {
    "degree": (
        "degree", "bachelor", "masters", "master", "phd", "doctorate",
        "college", "grad", "graduate",
    ),
    "education": (
        "degree", "bachelor", "masters", "master", "phd", "doctorate",
        "college", "grad", "graduate",
    ),
    "experience": ("experience", "years"),
    "visa": ("visa", "sponsorship", "sponsor", "authorization", "authorized"),
    "sponsorship": ("visa", "sponsorship", "sponsor", "authorization", "authorized"),
    "salary": ("salary", "compensation", "pay", "range", "base"),
    "compensation": ("salary", "compensation", "pay", "range", "base"),
    "remote": ("remote", "hybrid", "onsite", "on-site"),
    "senior": ("senior", "staff", "principal", "lead"),
    "junior": ("junior", "entry", "college", "grad"),
}

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*", re.IGNORECASE)
_STOPWORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "been", "by", "can",
    "does", "do", "for", "from", "had", "has", "have", "how", "i", "if", "in",
    "into", "is", "it", "its", "me", "my", "of", "on", "or", "our", "s",
    "should", "than", "that", "the", "their", "them", "then", "there", "they",
    "this", "to", "us", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "would", "you", "your",
}


@dataclass(frozen=True)
class JDChunk:
    section: str
    text: str
    start: int
    end: int


def _section_label(line: str) -> str | None:
    """Return a section label, including for headings the scorer does not know."""
    known = _heading(line)
    if known:
        return known
    cleaned = re.sub(r"^[#*_`\s]+|[#*_`\s]+$", "", line or "").strip()
    if not cleaned or len(cleaned) > 60:
        return None
    words = cleaned.split()
    if len(words) > 8:
        return None
    if cleaned.endswith(":") or (cleaned.isupper() and len(words) <= 6):
        return "other"
    return None


def _split_block(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", text)
        if part.strip()
    ]
    pieces: list[str] = []
    current = ""
    for part in parts:
        if len(part) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            step = max(1, max_chars - overlap)
            pieces.extend(part[index : index + max_chars] for index in range(0, len(part), step))
            continue
        if not current:
            current = part
        elif len(current) + 1 + len(part) <= max_chars:
            current += " " + part
        else:
            pieces.append(current)
            tail = current[-overlap:] if overlap else ""
            current = (tail + " " + part).strip() if tail else part
    if current:
        pieces.append(current)
    return pieces


def chunk_jd(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[JDChunk]:
    """Chunk the complete posting, preserving section labels and offsets.

    Recognized headings become section labels. Unrecognized heading-like lines
    stay in the text and start an ``other`` section, so no content is discarded.
    """
    if not text or not text.strip():
        return []
    normalized = promote_inline_headings(text)
    chunks: list[JDChunk] = []
    section = "body"
    block: list[str] = []
    block_start = 0
    offset = 0

    def flush() -> None:
        nonlocal block
        if not block:
            return
        raw = "\n".join(block).strip()
        block = []
        if not raw:
            return
        search_from = 0
        for piece in _split_block(raw, max_chars, overlap):
            index = raw.find(piece, search_from)
            if index < 0:
                index = search_from
            start = block_start + index
            chunks.append(
                JDChunk(
                    section=section,
                    text=piece,
                    start=start,
                    end=start + len(piece),
                )
            )
            search_from = index + max(1, len(piece) - overlap)

    for line in normalized.splitlines():
        line_start = offset
        offset += len(line) + 1
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        label = _section_label(stripped)
        if label:
            flush()
            section = label
            if _heading(stripped) is None:
                # Preserve an unrecognized heading as content; only the label
                # was inferred.
                block_start = line_start
                block.append(stripped)
            continue
        if not block:
            block_start = line_start
        block.append(stripped)
    flush()
    return chunks


def _terms(text: str) -> list[str]:
    return [
        token.lower()
        for token in _TOKEN.findall(text or "")
        if token.lower() not in _STOPWORDS
    ]


def _query_terms(query: str) -> list[str]:
    terms = _terms(query)
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        expanded.extend(QUERY_EXPANSIONS.get(term, ()))
    return list(dict.fromkeys(expanded))


def _section_inventory(chunks: list[JDChunk]) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.section] = counts.get(chunk.section, 0) + len(chunk.text)
    return [
        {"section": section, "chars": chars}
        for section, chars in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def search_jd(
    text: str,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    max_excerpt_chars: int = MAX_EXCERPT_CHARS,
) -> dict[str, object]:
    """Rank full-description chunks against a natural-language query.

    The result always reports the section inventory and total character count,
    so the caller can see that the search covered the whole posting rather than
    a truncated prefix.
    """
    limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    chunks = chunk_jd(text)
    inventory = _section_inventory(chunks)
    total_chars = len(text or "")
    query_terms = _query_terms(query)
    if not chunks:
        return {
            "query": query,
            "total_chars": total_chars,
            "chunk_count": 0,
            "sections": inventory,
            "chunks": [],
            "offset_basis": "normalized_description",
        }

    token_lists = [_terms(chunk.text) for chunk in chunks]
    document_frequency: Counter[str] = Counter()
    for tokens in token_lists:
        document_frequency.update(set(tokens))
    total_documents = len(chunks)
    phrase = " ".join((query or "").lower().split())

    ranked: list[tuple[float, JDChunk, list[str]]] = []
    for chunk, tokens in zip(chunks, token_lists):
        counts = Counter(tokens)
        length = max(len(tokens), 1)
        score = 0.0
        matched: list[str] = []
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            inverse = math.log(
                (total_documents + 1) / (document_frequency.get(term, 0) + 0.5)
            ) + 1.0
            score += (frequency / length) * inverse
            matched.append(term)
        if phrase and phrase in " ".join(chunk.text.lower().split()):
            score += 1.5
        score *= SECTION_PRIOR.get(chunk.section, 1.0)
        if score > 0:
            ranked.append((score, chunk, matched))

    if not ranked and not query_terms:
        ranked = [(0.0, chunk, []) for chunk in chunks[:limit]]

    ranked.sort(key=lambda item: (-item[0], item[1].start))
    selected = ranked[:limit]
    return {
        "query": query,
        "total_chars": total_chars,
        "chunk_count": len(chunks),
        "sections": inventory,
        "offset_basis": "normalized_description",
        "chunks": [
            {
                "section": chunk.section,
                "text": chunk.text[:max_excerpt_chars],
                "score": round(score, 4),
                "start": chunk.start,
                "end": chunk.end,
                "matched_terms": matched[:12],
            }
            for score, chunk, matched in selected
        ],
    }
