"""Retrieval over the regulatory document corpus.

Why this exists. The advisory could already name the authority behind a number - CPCB
for the band, WHO for the guideline value, CAQM for the GRAP stage. What it could not do
was quote what those documents actually *say*. A citizen asking "why does 180 mean my
child should stay in?" got our sentence, not the regulator's.

This module indexes `advisory/corpus/*.md`, splits each document into passages at its
headings, and retrieves the passages that answer a question. The generation step then
composes only from retrieved text, so every clause in an answer traces to a passage that
traces to a publisher, a year and a URL.

Why BM25 and not embeddings. Vector retrieval would need a sentence-transformer, which
means torch, which means roughly half a gigabyte in a free-tier container that currently
starts in seconds. BM25 is the standard lexical first stage in production retrieval
stacks, it is exact and inspectable, and on a corpus of this size and vocabulary - a
closed regulatory domain where the query words *are* the document words - it is the
right tool rather than a compromise. Pure standard library: no new dependency.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

CORPUS_DIR = Path(__file__).parent / "corpus"

# BM25 free parameters. k1 controls how fast term frequency saturates, b how strongly
# passage length is normalised. These are the conventional defaults and we have no
# labelled relevance data to justify tuning them, so we do not pretend to.
K1 = 1.5
B = 0.75

_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "of", "to",
    "in", "on", "at", "for", "with", "by", "from", "as", "and", "or", "but", "if",
    "than", "that", "this", "these", "those", "it", "its", "i", "my", "me", "we",
    "our", "you", "your", "he", "she", "they", "them", "do", "does", "did", "can",
    "could", "should", "would", "will", "shall", "may", "might", "not", "no", "so",
    "what", "which", "who", "how", "when", "where", "why", "there", "here",
}

_WORD = re.compile(r"[a-z0-9][a-z0-9._-]*")

# Query-side expansion only. The corpus writes "Stage III"; a citizen types "stage 3".
# Expanding the query rather than the index keeps the documents verbatim, which matters
# because we quote them back as evidence.
_EXPAND = {
    "1": ["i"], "2": ["ii"], "3": ["iii"], "4": ["iv"],
    "i": ["1"], "ii": ["2"], "iii": ["3"], "iv": ["4"],
    "pm2.5": ["pm25"], "pm25": ["pm2.5"],
    "no2": ["nitrogen", "dioxide"], "so2": ["sulphur", "dioxide"],
    "kid": ["child", "children"], "kids": ["child", "children"],
    "child": ["children", "sensitive"], "children": ["child", "sensitive"],
    "asthma": ["respiratory", "sensitive"], "elderly": ["older", "sensitive"],
    "pregnant": ["sensitive"], "outside": ["outdoor"], "outdoors": ["outdoor"],
    "average": ["averaging", "hour"], "dust": ["coarse", "mechanical"],
}


def _tokenise(text: str) -> list[str]:
    """Lowercase word tokens, stopwords removed.

    Numbers and dotted forms survive intact because "pm2.5", "2.5" and "450" carry most
    of the meaning in this corpus - dropping them would gut the index.
    """
    return [t for t in _WORD.findall(text.lower()) if t not in _STOP]


@dataclass
class Passage:
    doc_id: str
    title: str
    publisher: str
    year: int
    url: str
    heading: str
    text: str
    tokens: list[str] = field(default_factory=list)

    def cite(self) -> str:
        return f"{self.publisher} ({self.year})"


def _parse(path: Path) -> list[Passage]:
    """One markdown file -> one passage per `##` heading.

    Splitting at headings rather than at a fixed token count keeps each passage a
    complete thought: a band table stays whole, a GRAP stage keeps all its measures.
    Fixed-width chunking would cut the 24-hour averaging rule in half, which is exactly
    the clause we most need to retrieve intact.
    """
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            for line in raw[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = raw[end + 4:]

    out: list[Passage] = []
    for block in re.split(r"\n(?=## )", body):
        block = block.strip()
        if not block:
            continue
        first, _, rest = block.partition("\n")
        heading = first.lstrip("# ").strip() if first.startswith("##") else ""
        text = (rest if heading else block).strip()
        if not text:
            continue
        # The heading is part of the searchable text: "Averaging period" is often the
        # only place the query word appears.
        tokens = _tokenise(heading + " " + text)
        out.append(Passage(
            doc_id=meta.get("id", path.stem),
            title=meta.get("title", path.stem),
            publisher=meta.get("publisher", "unknown"),
            year=int(meta.get("year", 0) or 0),
            url=meta.get("url", ""),
            heading=heading or meta.get("title", ""),
            text=text,
            tokens=tokens,
        ))
    return out


class _Index:
    def __init__(self, passages: list[Passage]):
        self.passages = passages
        self.n = len(passages)
        self.avg_len = (sum(len(p.tokens) for p in passages) / self.n) if self.n else 0.0
        df: dict[str, int] = {}
        for p in passages:
            for term in set(p.tokens):
                df[term] = df.get(term, 0) + 1
        # Standard BM25 idf with the +1 inside the log, which keeps it non-negative for
        # terms that appear in every passage instead of letting them score below zero.
        self.idf = {
            t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }
        self.tf = [
            {t: p.tokens.count(t) for t in set(p.tokens)} for p in passages
        ]

    def search(self, query: str, k: int = 4) -> list[tuple[Passage, float]]:
        q = _tokenise(query)
        # Expansions score at a discount so they can break a tie but never outweigh a
        # term the reader actually typed.
        weights = {t: 1.0 for t in q}
        for t in list(q):
            for alt in _EXPAND.get(t, ()):
                weights.setdefault(alt, 0.45)
        q = list(weights)
        if not q or not self.n:
            return []
        scored: list[tuple[Passage, float]] = []
        for i, p in enumerate(self.passages):
            dl = len(p.tokens) or 1
            s = 0.0
            for term in q:
                idf = self.idf.get(term)
                if idf is None:
                    continue
                f = self.tf[i].get(term, 0)
                if not f:
                    continue
                s += weights.get(term, 1.0) * idf * (f * (K1 + 1)) / (
                    f + K1 * (1 - B + B * dl / self.avg_len))
            if s > 0:
                scored.append((p, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]


_lock = threading.Lock()
_index: _Index | None = None


def _build() -> _Index:
    passages: list[Passage] = []
    if CORPUS_DIR.is_dir():
        for path in sorted(CORPUS_DIR.glob("*.md")):
            try:
                passages.extend(_parse(path))
            except Exception:
                continue
    return _Index(passages)


def index() -> _Index:
    """Built once on first use and held. The corpus is static and small."""
    global _index
    with _lock:
        if _index is None:
            _index = _build()
        return _index


def available() -> bool:
    return index().n > 0


def stats() -> dict:
    ix = index()
    files = len(list(CORPUS_DIR.glob("*.md"))) if CORPUS_DIR.is_dir() else 0
    return {
        "available": ix.n > 0,
        "documents": files,
        "authorities": len({p.doc_id for p in ix.passages}),
        "passages": ix.n,
        "vocabulary": len(ix.idf),
        "method": "BM25 lexical retrieval (k1=1.5, b=0.75) over heading-split passages",
    }


def search(query: str, k: int = 4) -> list[dict]:
    """Top passages for a question, each carrying its own attribution."""
    return [
        {
            "doc_id": p.doc_id,
            "title": p.title,
            "heading": p.heading,
            "text": p.text,
            "publisher": p.publisher,
            "year": p.year,
            "url": p.url,
            "citation": p.cite(),
            "score": round(score, 3),
        }
        for p, score in index().search(query, k)
    ]


def context_block(query: str, k: int = 3, max_chars: int = 2200) -> tuple[str, list[dict]]:
    """Retrieved passages formatted for a prompt, plus the hits they came from.

    Truncated by character budget rather than passage count: three short passages are
    worth more context than one long table, and the generation step has a finite window.
    """
    hits = search(query, k)
    parts: list[str] = []
    used: list[dict] = []
    budget = max_chars
    for h in hits:
        chunk = f"[{h['citation']}] {h['heading']}\n{h['text']}"
        if len(chunk) > budget and used:
            break
        parts.append(chunk[:budget])
        used.append(h)
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n\n".join(parts), used
