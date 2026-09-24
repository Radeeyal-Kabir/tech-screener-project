import json

import pytest

from screener import analyze_filing as af
from screener import edgar_client

PROSE = (
    "Revenue increased 18% driven by strong demand for data center products, while gross margin "
    "declined due to higher inventory provisions and export restrictions affecting shipments. "
)


def _filing_html(form="10-K", body_paras=40, split_heading=False, stub=False):
    item, nxt = ("7", "Item 7A. Quantitative and Qualitative Disclosures About Market Risk") if form == "10-K" \
        else ("2", "Item 3. Quantitative and Qualitative Disclosures About Market Risk")
    heading = (f"<table><tr><td>Item {item}.</td><td>Management&#8217;s Discussion and Analysis</td></tr></table>"
               if split_heading else f"<p><b>Item {item}. Management&#8217;s Discussion and Analysis of Financial "
                                     f"Condition and Results of Operations</b></p>")
    body = ("<p>The information required by this item is incorporated herein by reference to the "
            "Annual Report.</p>" if stub else "".join(f"<p>{PROSE}Paragraph {i}.</p>" for i in range(body_paras)))
    return f"""<html><head><title>x</title><style>p{{}}</style></head><body>
    <div style="display: none"><ix:header>Item {item}. Management's Discussion hidden xbrl header</ix:header></div>
    <p>TABLE OF CONTENTS</p>
    <table><tr><td>Item {item}.</td><td>Management's Discussion and Analysis</td><td>25</td></tr>
    <tr><td>{nxt}</td><td>40</td></tr></table>
    {heading}
    {body}
    <table><tr><td>Revenue</td><td>$ 1,234</td><td>$ 999</td></tr></table>
    <p><b>{nxt}</b></p><p>Market risk text.</p>
    </body></html>"""


def test_html_to_text_skips_hidden_and_styles():
    text = af.html_to_text(_filing_html())
    assert "hidden xbrl header" not in text
    assert "p{}" not in text
    assert "Management’s Discussion" in text


@pytest.mark.parametrize("form", ["10-K", "10-Q"])
def test_find_section_skips_table_of_contents(form):
    section = af.find_section(af.html_to_text(_filing_html(form)), form)
    assert section.count("Paragraph") == 40
    assert "Market risk text" not in section


def test_find_section_handles_heading_split_across_table_cells():
    section = af.find_section(af.html_to_text(_filing_html(split_heading=True)), "10-K")
    assert "Paragraph 39" in section


def test_missing_heading_raises_extraction_error():
    with pytest.raises(af.ExtractionError):
        af.find_section("no headings at all here", "10-K")


def test_incorporated_by_reference_follows_ex13(monkeypatch):
    ex13 = "<p>Management Discussion</p>" + "".join(f"<p>{PROSE}Paragraph {i}.</p>" for i in range(30)) + \
           "<p>Report of Independent Registered Public Accounting Firm</p><p>audit</p>"
    docs = {"ibm-10k.htm": _filing_html(stub=True), "ibm-ex13.htm": ex13}
    monkeypatch.setattr(edgar_client, "get_text", lambda url: docs[url.rsplit("/", 1)[1]])
    monkeypatch.setattr(edgar_client, "filing_documents", lambda cik, acc: ["ibm-10k.htm", "ibm-ex13.htm", "ex21.htm"])
    section, url = af.extract_mda("0000051143", {"accession": "0000051143-26-000001", "form": "10-K",
                                                 "primary_document": "ibm-10k.htm"})
    assert url.endswith("ibm-ex13.htm")
    assert "Paragraph 29" in section and "audit" not in section


def test_select_text_respects_budget_and_keeps_overview_and_order():
    paras = [f"Opening overview paragraph number {i} describing the business in plain factual words only." * 2
             for i in range(5)]
    paras += ["Filler text about office locations and the history of the company in many words." * 2] * 30
    paras += [PROSE * 2]
    section = "\n".join(paras)
    selected = af.select_text(section, budget=3500)
    assert sum(len(p) for p in selected) <= 3500
    assert selected[0].startswith("Opening overview paragraph number 0")
    assert (PROSE * 2).strip() in selected  # the high-signal paragraph beats filler


def test_chunk_sizes():
    chunks = af.chunk(["a" * 2500] * 5, size=6000)
    assert len(chunks) == 3 and all(len(c) <= 6000 for c in chunks)


def test_quote_verification():
    text = "Gross margin declined due to higher inventory provisions and export restrictions."
    assert af.quote_in_text("gross margin declined due to higher inventory provisions", text)
    assert af.quote_in_text("Gross margin declined due to higher inventory provisions and a weak yen", text)
    assert not af.quote_in_text("revenue collapsed amid a severe customer exodus", text)
    assert not af.quote_in_text("gross margin", text)  # too short to count as evidence


def _llm(monkeypatch, *responses):
    it = iter(responses)
    monkeypatch.setattr(af, "_ollama_generate", lambda prompt: next(it))


CONTEXT = {"name": "Nvidia", "ticker": "NVDA", "form": "10-K", "period_end": "2026-01-25"}


def test_analyze_chunk_drops_hallucinated_flags(monkeypatch):
    _llm(monkeypatch, json.dumps({
        "tone": "bullish", "reason": "Strong demand.",
        "red_flags": [
            {"category": "export_controls_geopolitical", "summary": "Export limits hit shipments",
             "quote": "higher inventory provisions and export restrictions affecting shipments"},
            {"category": "liquidity_debt", "summary": "Invented", "quote": "the company may be unable to repay its notes"},
            {"category": "made_up_category", "summary": "x", "quote": "Revenue increased 18% driven by strong demand"},
        ],
    }))
    out = af.analyze_chunk(PROSE, CONTEXT)
    assert out["tone"] == "bullish" and out["dropped"] == 1
    assert [f["category"] for f in out["red_flags"]] == ["export_controls_geopolitical", "other"]


def test_analyze_chunk_retries_bad_json_then_gives_up(monkeypatch):
    _llm(monkeypatch, "not json", json.dumps({"tone": "neutral", "reason": "", "red_flags": []}))
    assert af.analyze_chunk(PROSE, CONTEXT)["tone"] == "neutral"
    _llm(monkeypatch, "nope", "{\"tone\": \"ecstatic\"}")
    assert af.analyze_chunk(PROSE, CONTEXT) is None


def test_combine_weights_tone_by_length_and_dedupes_flags():
    flag = {"category": "supply_chain", "summary": "s", "quote": "q"}
    out = af.combine([
        {"tone": "bearish", "reason": "Weak.", "red_flags": [flag], "dropped": 0, "chars": 5000},
        {"tone": "neutral", "reason": "Flat.", "red_flags": [flag], "dropped": 2, "chars": 1000},
    ])
    assert out["tone"] == "bearish" and out["tone_rationale"] == "Weak."
    assert len(out["red_flags"]) == 1 and out["unverified_flags_dropped"] == 2


def test_extraction_failure_is_recorded_not_raised(monkeypatch):
    monkeypatch.setattr(edgar_client, "get_text", lambda url: "<p>nothing useful</p>")
    result = af.analyze_filing("NVDA", "0001045810", {"accession": "a", "form": "10-Q", "filed": "2026-08-27",
                                                     "period_end": "2026-07-26", "primary_document": "d.htm"})
    assert result["status"] == "extraction_failed"


def test_record_result_replaces_same_accession_and_keeps_order():
    state = {"companies": {"NVDA": {}}}
    for acc, period in [("b", "2026-04-26"), ("a", "2026-01-25"), ("b", "2026-04-26")]:
        af.record_result(state, "NVDA", {"accession": acc, "filed": period, "period_end": period, "status": "ok"})
    assert [f["accession"] for f in state["companies"]["NVDA"]["qualitative"]["filings"]] == ["a", "b"]
