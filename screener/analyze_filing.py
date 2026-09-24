"""Qualitative pipeline: a 10-Q/10-K's MD&A -> management tone + red flags.

Usage:
    python -m screener.analyze_filing NVDA                 # latest 10-Q/10-K
    python -m screener.analyze_filing NVDA 0001045810-25-000209

Steps:
  1. Fetch the filing's primary HTML document and flatten it to text.
  2. Locate MD&A (10-K Item 7 / 10-Q Item 2). Headings also appear in the
     table of contents, so the longest heading-to-next-item span wins. If
     that span is only a stub incorporating MD&A by reference (IBM does
     this), follow it to the annual report exhibit (EX-13).
  3. Select what to send to the model. A free GitHub Actions runner is
     CPU-only, so instead of the whole MD&A (often 50-100k characters) we
     keep the opening overview plus the paragraphs densest in tone/risk
     language, up to MAX_CHARS_ANALYZED, split into CHUNK_CHARS chunks.
  4. Ask a small local model (via Ollama) for tone + red flags per chunk,
     with a JSON schema constraining the answer. Red flags must use a fixed
     category list (so persistence across quarters can be matched) and cite
     a verbatim quote; flags whose quote isn't in the text are dropped.
  5. Combine chunk results into one record per filing.

Section-extraction problems are recorded on the filing (status
"extraction_failed") and surfaced as a GitHub Actions warning rather than
aborting the run. EDGAR or Ollama being unreachable is a hard failure.
"""

from __future__ import annotations

import json
import os
import re
import sys
from html.parser import HTMLParser

import requests

from screener import edgar_client, store
from screener.universe import by_ticker

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 4096}
OLLAMA_TIMEOUT = 900  # seconds per chunk; CPU inference is slow

PERIODIC_FORMS = ("10-K", "10-Q")
FILINGS_KEPT = 8

MAX_CHARS_ANALYZED = 12_000
CHUNK_CHARS = 6_000
OVERVIEW_CHARS = 2_500
MIN_SECTION_CHARS = 3_000

RED_FLAG_CATEGORIES = [
    "demand_weakness",
    "margin_pressure",
    "pricing_pressure",
    "supply_chain",
    "inventory_buildup",
    "customer_concentration",
    "competition",
    "export_controls_geopolitical",
    "regulatory_legal",
    "liquidity_debt",
    "restructuring_layoffs",
    "impairment_writedown",
    "accounting_controls",
    "guidance_cut",
    "macro_fx",
    "other",
]

TONE_VALUES = {"bearish": -1, "neutral": 0, "bullish": 1}

# Words that make a paragraph more informative about tone or risk.
SIGNAL_WORDS = re.compile(
    r"\b(declin\w*|decreas\w*|lower|weak\w*|soft\w*|headwind\w*|pressure\w*|uncertain\w*|challeng\w*|"
    r"slow\w*|delay\w*|constrain\w*|shortage\w*|excess|impair\w*|restructur\w*|litigation|investigation|"
    r"export|restriction\w*|tariff\w*|inventor\w*|concentrat\w*|record|strong\w*|robust|accelerat\w*|"
    r"grow\w*|increas\w*|demand|outlook|expect\w*|anticipat\w*|guidance|momentum|risk\w*)\b",
    re.IGNORECASE,
)

_SEP = r"[\s\.:\-–—|]*"
_MDA = r"management\W{0,3}s?\s+discussion"
SECTION_PATTERNS = {
    "10-K": (rf"item{_SEP}7{_SEP}{_MDA}", rf"item{_SEP}7a\b|item{_SEP}8{_SEP}financial\s+statements"),
    "10-Q": (rf"item{_SEP}2{_SEP}{_MDA}", rf"item{_SEP}3{_SEP}quantitative|item{_SEP}4{_SEP}controls"),
    # Annual-report exhibits (EX-13) have no Item numbers.
    "EX-13": (
        _MDA,
        r"report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm"
        r"|consolidated\s+(income\s+)?statements?\s+of\s+(earnings|income|operations)",
    ),
}


class QualitativeError(RuntimeError):
    """Environment problem (Ollama down, model missing): abort the run."""


class ExtractionError(RuntimeError):
    """This filing's MD&A couldn't be located/analyzed: record it and move on."""


# --------------------------------------------------------------------------
# HTML -> text


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "center"}
    VOID = {"br", "img", "hr", "meta", "input", "link", "col", "area", "base", "wbr"}
    SKIP = {"script", "style", "head", "title", "ix:header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []  # (tag, hidden)

    def _hidden(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in self.VOID:
            return
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        hidden = self._hidden() or tag in self.SKIP or "display:none" in style
        self.stack.append((tag, hidden))

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in self.BLOCK:
            self.parts.append("\n")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self._hidden():
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ").replace("​", "")
    lines = [re.sub(r"[ \t|]+", " ", line).strip(" |") for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --------------------------------------------------------------------------
# Locating MD&A


def find_section(text: str, kind: str) -> str:
    start_pat, end_pat = SECTION_PATTERNS[kind]
    starts = [m.start() for m in re.finditer(start_pat, text, re.IGNORECASE)]
    if not starts:
        raise ExtractionError(f"No MD&A heading found ({kind})")
    ends = [m.start() for m in re.finditer(end_pat, text, re.IGNORECASE)]
    best = ""
    for s in starts:
        e = next((e for e in ends if e > s + 50), min(len(text), s + 250_000))
        if e - s > len(best):
            best = text[s:e]
    return best.strip()


def _incorporated_by_reference(section: str) -> bool:
    return len(section) < MIN_SECTION_CHARS and re.search(r"incorporated\W+(herein\W+)?by\W+reference", section, re.I) is not None


def extract_mda(cik10: str, filing: dict) -> tuple[str, str]:
    """Return (mda_text, source_url)."""
    url = edgar_client.archive_url(cik10, filing["accession"], filing["primary_document"])
    text = html_to_text(edgar_client.get_text(url))
    kind = "10-K" if filing["form"].startswith("10-K") else "10-Q"
    section = find_section(text, kind)

    if _incorporated_by_reference(section):
        exhibits = [n for n in edgar_client.filing_documents(cik10, filing["accession"])
                    if re.search(r"ex-?13", n, re.I) and n.lower().endswith((".htm", ".html"))]
        if not exhibits:
            raise ExtractionError("MD&A incorporated by reference, but no EX-13 exhibit found")
        url = edgar_client.archive_url(cik10, filing["accession"], exhibits[0])
        section = find_section(html_to_text(edgar_client.get_text(url)), "EX-13")

    if len(section) < MIN_SECTION_CHARS:
        raise ExtractionError(f"MD&A section too short ({len(section)} chars) — likely mis-located")
    return section, url


# --------------------------------------------------------------------------
# Selecting and chunking text for the model


def _prose_paragraphs(section: str) -> list[str]:
    paras = []
    for p in re.split(r"\n+", section):
        p = p.strip()
        letters = sum(ch.isalpha() for ch in p)
        # Drop table rows, page numbers and short headings.
        if len(p) >= 80 and letters / len(p) >= 0.6:
            paras.append(p)
    return paras


def select_text(section: str, budget: int = MAX_CHARS_ANALYZED) -> list[str]:
    """Opening overview + highest-signal paragraphs, in original order."""
    paras = _prose_paragraphs(section)
    chosen: set[int] = set()
    used = 0
    for i, p in enumerate(paras):
        if used >= OVERVIEW_CHARS:
            break
        chosen.add(i)
        used += len(p)

    def density(i: int) -> float:
        return len(SIGNAL_WORDS.findall(paras[i])) / max(1, len(paras[i]) / 100)

    for i in sorted((i for i in range(len(paras)) if i not in chosen), key=density, reverse=True):
        if used + len(paras[i]) > budget:
            continue
        chosen.add(i)
        used += len(paras[i])
    return [paras[i] for i in sorted(chosen)]


def chunk(paragraphs: list[str], size: int = CHUNK_CHARS) -> list[str]:
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        p = p[:size]
        if current and len(current) + len(p) + 2 > size:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{p}" if current else p
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# The model

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "tone": {"type": "string", "enum": list(TONE_VALUES)},
        "reason": {"type": "string"},
        "red_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": RED_FLAG_CATEGORIES},
                    "summary": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["category", "summary", "quote"],
            },
        },
    },
    "required": ["tone", "reason", "red_flags"],
}

SYSTEM_PROMPT = (
    "You are a careful equity analyst reading the Management's Discussion and Analysis (MD&A) "
    "section of an SEC filing. Answer only with JSON matching the requested schema."
)

PROMPT_TEMPLATE = """Company: {name} ({ticker}). Filing: {form} for the period ending {period_end}.

Read this MD&A excerpt and return:
- "tone": management's overall tone about the business: "bullish" (confident, strong demand, improving results), "neutral" (balanced or purely factual), or "bearish" (cautious, weakening results, headwinds dominate).
- "reason": one sentence explaining the tone.
- "red_flags": specific problems management discloses about THIS company's business now. Skip generic boilerplate that any company could write. For each: "category" (one of: {categories}), "summary" (at most 20 words), and "quote": 5-25 words copied exactly, word for word, from the excerpt. Use an empty list if there are none.

Excerpt:
\"\"\"
{text}
\"\"\"
"""


def _ollama_generate(prompt: str) -> str:
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "system": SYSTEM_PROMPT,
                "prompt": prompt,
                "format": RESPONSE_SCHEMA,
                "stream": False,
                "options": OLLAMA_OPTIONS,
            },
            timeout=OLLAMA_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise QualitativeError(f"Ollama not reachable at {OLLAMA_URL}: {exc}") from exc
    if resp.status_code == 404:
        raise QualitativeError(f"Model {OLLAMA_MODEL!r} not available — run `ollama pull {OLLAMA_MODEL}`")
    if resp.status_code != 200:
        raise QualitativeError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json().get("response", "")


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip(" .,;:\"'")


def quote_in_text(quote: str, text: str) -> bool:
    q, t = _norm(quote), _norm(text)
    if len(q.split()) < 4:
        return False
    if q in t:
        return True
    # Small models often trail off or tweak the end of a quote; the opening words must still match.
    return " ".join(q.split()[:8]) in t


def analyze_chunk(text: str, context: dict) -> dict | None:
    prompt = PROMPT_TEMPLATE.format(text=text, categories=", ".join(RED_FLAG_CATEGORIES), **context)
    for _ in range(2):
        raw = _ollama_generate(prompt)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("tone") not in TONE_VALUES:
            continue
        flags, dropped = [], 0
        for rf in data.get("red_flags") or []:
            if not isinstance(rf, dict):
                continue
            if quote_in_text(str(rf.get("quote", "")), text):
                category = rf.get("category") if rf.get("category") in RED_FLAG_CATEGORIES else "other"
                flags.append({"category": category, "summary": str(rf.get("summary", ""))[:200],
                              "quote": str(rf["quote"])[:300]})
            else:
                dropped += 1
        return {"tone": data["tone"], "reason": str(data.get("reason", ""))[:300],
                "red_flags": flags, "dropped": dropped, "chars": len(text)}
    return None


def combine(chunk_results: list[dict]) -> dict:
    """Length-weighted average tone; red flags de-duplicated by category."""
    total = sum(r["chars"] for r in chunk_results)
    avg = sum(TONE_VALUES[r["tone"]] * r["chars"] for r in chunk_results) / total
    tone = "bullish" if avg > 0.33 else "bearish" if avg < -0.33 else "neutral"
    reason = next((r["reason"] for r in chunk_results if r["tone"] == tone), chunk_results[0]["reason"])
    flags: dict[str, dict] = {}
    for r in chunk_results:
        for rf in r["red_flags"]:
            flags.setdefault(rf["category"], rf)
    return {
        "tone": tone,
        "tone_score": round(avg, 3),
        "tone_rationale": reason,
        "red_flags": [flags[c] for c in sorted(flags)],
        "unverified_flags_dropped": sum(r["dropped"] for r in chunk_results),
    }


# --------------------------------------------------------------------------
# Orchestration


def analyze_filing(ticker: str, cik10: str, filing: dict) -> dict:
    company = by_ticker(ticker)
    base = {
        "accession": filing["accession"],
        "form": filing["form"],
        "filed": filing["filed"],
        "period_end": filing["period_end"],
        "analyzed_at": store.utc_now_iso(),
        "model": OLLAMA_MODEL,
    }
    try:
        section, source_url = extract_mda(cik10, filing)
    except ExtractionError as exc:
        return {**base, "status": "extraction_failed", "error": str(exc)}

    selected = select_text(section)
    context = {"name": company.name, "ticker": ticker, "form": filing["form"],
               "period_end": filing["period_end"] or "unknown"}
    results = [r for r in (analyze_chunk(c, context) for c in chunk(selected)) if r is not None]
    if not results:
        return {**base, "status": "llm_failed", "error": "Model returned no valid JSON for any chunk",
                "source_url": source_url}
    return {
        **base,
        "status": "ok",
        "source_url": source_url,
        "mda_chars": len(section),
        "chars_analyzed": sum(r["chars"] for r in results),
        **combine(results),
    }


def record_result(state: dict, ticker: str, result: dict) -> None:
    rec = state["companies"][ticker]
    filings = [f for f in rec.setdefault("qualitative", {}).get("filings", [])
               if f["accession"] != result["accession"]]
    filings.append(result)
    filings.sort(key=lambda f: (f.get("period_end") or f["filed"], f["filed"]))
    rec["qualitative"]["filings"] = filings[-FILINGS_KEPT:]
    rec["qualitative_as_of"] = store.utc_now_iso()
    if result["status"] != "ok":
        # Shows as a yellow annotation on the Actions run without failing it.
        print(f"::warning title=Qualitative {ticker}::{result['accession']} {result['status']}: "
              f"{result.get('error')}", file=sys.stderr)


def main(argv: list[str]) -> int:
    from screener import score

    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    ticker = argv[0].upper()
    state = store.load_companies()
    try:
        cik10 = edgar_client.resolve_ciks([ticker])[ticker]
        filings = edgar_client.recent_filings(edgar_client.get_submissions(cik10), PERIODIC_FORMS)
        if len(argv) > 1:
            filing = next(f for f in filings if f["accession"] == argv[1])
        else:
            filing = filings[0]
        result = analyze_filing(ticker, cik10, filing)
    except StopIteration:
        print(f"ERROR: accession {argv[1]} is not a recent 10-K/10-Q for {ticker}", file=sys.stderr)
        return 1
    except (edgar_client.EdgarError, QualitativeError) as exc:
        print(f"ERROR: {exc}\nNothing written.", file=sys.stderr)
        return 1
    record_result(state, ticker, result)
    state["companies"][ticker]["cik"] = cik10
    score.rescore_all(state)
    store.save_companies(state)
    print(json.dumps({k: v for k, v in result.items() if k != "red_flags"}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
