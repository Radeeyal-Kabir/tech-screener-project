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


def test_find_section_ignores_cross_reference_to_mda_in_risk_factors():
    # QCOM's 10-Qs: Part II Risk Factors quotes "Part I, Item 2. Management's Discussion..." mid-sentence,
    # with no Item 3/4 heading after it. That span (to the end of the document) used to win as the longest.
    html = _filing_html("10-Q").replace("</body>", (
        "<p>PART II. OTHER INFORMATION</p><p>Item 1A. Risk Factors</p>"
        "<p>You should read the risks below together with &#8220;Part I, Item 2. Management&#8217;s Discussion and "
        "Analysis of Financial Condition and Results of Operations.&#8221;</p>"
        + "<p>A decline in global economic conditions could harm our business and results of operations.</p>" * 200
        + "<p>SIGNATURES</p></body>"))
    section = af.find_section(af.html_to_text(html), "10-Q")
    assert section.count("Paragraph") == 40
    assert "could harm our business" not in section and "SIGNATURES" not in section


def test_find_section_not_cut_short_by_cross_reference_to_next_item():
    # MU/HPE/ACN 10-Ks: "see Item 8. Financial Statements..." mid-MD&A used to end the section there.
    html = _filing_html("10-K").replace(
        "Paragraph 20.</p>",
        "Paragraph 20. For details, see Item 8. Financial Statements and Supplementary Data, Note 3.</p>")
    section = af.find_section(af.html_to_text(html), "10-K")
    assert "Paragraph 39" in section and "Market risk text" not in section


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


def test_incorporated_by_reference_finds_ex13_by_document_type(monkeypatch):
    # IBM's real 10-Ks (0000051143-25-000015, 0000051143-26-000010): the EX-13 annual report is
    # "ibm-20251231_d2.htm" -- nothing in the filename says EX-13; only the filing index's Type column does.
    ex13 = ("<p>Management Discussion</p>" + "".join(f"<p>{PROSE}Paragraph {i}.</p>" for i in range(30))
            + "<p>Report of Management</p><p>Management is responsible for the financial statements.</p>"
            + "<p>Report of Independent Registered Public Accounting Firm</p><p>audit</p>")
    docs = {"ibm-20251231.htm": _filing_html(stub=True), "ibm-20251231_d2.htm": ex13}
    monkeypatch.setattr(edgar_client, "get_text", lambda url: docs[url.rsplit("/", 1)[1]])
    monkeypatch.setattr(edgar_client, "filing_documents", lambda cik, acc: [
        "ibm-20251231.htm", "ibm-20251231x10kex41.htm", "ibm-20251231_d2.htm", "ibm-20251231x10kex21.htm", "R1.htm"])
    monkeypatch.setattr(edgar_client, "filing_document_types", lambda cik, acc: {
        "ibm-20251231.htm": "10-K", "ibm-20251231x10kex41.htm": "EX-4.1", "ibm-20251231_d2.htm": "EX-13",
        "ibm-20251231x10kex21.htm": "EX-21"})
    section, url = af.extract_mda("0000051143", {"accession": "0000051143-26-000010", "form": "10-K",
                                                 "primary_document": "ibm-20251231.htm"})
    assert url.endswith("/000005114326000010/ibm-20251231_d2.htm")
    assert "Paragraph 29" in section and "Management is responsible" not in section


def test_incorporated_by_reference_without_ex13_is_extraction_failure(monkeypatch):
    monkeypatch.setattr(edgar_client, "get_text", lambda url: _filing_html(stub=True))
    monkeypatch.setattr(edgar_client, "filing_documents", lambda cik, acc: ["x-10k.htm", "x-ex21.htm"])
    monkeypatch.setattr(edgar_client, "filing_document_types", lambda cik, acc: {"x-10k.htm": "10-K", "x-ex21.htm": "EX-21"})
    with pytest.raises(af.ExtractionError, match="no EX-13"):
        af.extract_mda("0000000001", {"accession": "0000000001-26-000001", "form": "10-K", "primary_document": "x-10k.htm"})


def test_filing_document_types_parses_filing_index(monkeypatch):
    # Trimmed from https://www.sec.gov/Archives/edgar/data/51143/000005114326000010/0000051143-26-000010-index.html
    index = """<table class="tableFile" summary="Document Format Files">
      <tr><th scope="col">Seq</th><th scope="col">Description</th><th scope="col">Document</th><th scope="col">Type</th><th scope="col">Size</th></tr>
      <tr><td scope="row">1</td><td scope="row">10-K</td><td scope="row"><a href="/ix?doc=/Archives/edgar/data/51143/000005114326000010/ibm-20251231.htm">ibm-20251231.htm</a> &nbsp;&nbsp;<span class="label">iXBRL</span></td><td scope="row">10-K</td><td scope="row">1077083</td></tr>
      <tr class="blueRow"><td scope="row">5</td><td scope="row">EX-13</td><td scope="row"><a href="/Archives/edgar/data/51143/000005114326000010/ibm-20251231_d2.htm">ibm-20251231_d2.htm</a></td><td scope="row">EX-13</td><td scope="row">4525399</td></tr>
      <tr><td scope="row">&nbsp;</td><td scope="row">Complete submission text file</td><td scope="row"><a href="/Archives/edgar/data/51143/000005114326000010/0000051143-26-000010.txt">0000051143-26-000010.txt</a></td><td scope="row">&nbsp;</td><td scope="row">32665236</td></tr>
    </table>"""
    monkeypatch.setattr(edgar_client, "get_text", lambda url: index)
    types = edgar_client.filing_document_types("0000051143", "0000051143-26-000010")
    assert types == {"ibm-20251231.htm": "10-K", "ibm-20251231_d2.htm": "EX-13", "0000051143-26-000010.txt": ""}


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


def test_select_text_never_sends_safe_harbor_boilerplate():
    safe_harbor = ("This report contains forward-looking statements that involve risks and uncertainties; we undertake "
                   "no obligation to publicly update or revise any forward-looking statements, except as required by law.")
    section = "\n".join([PROSE * 2, safe_harbor, PROSE * 2 + " Second."])
    selected = af.select_text(section)
    assert len(selected) == 2 and not any("forward-looking" in p for p in selected)


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


def test_analyze_chunk_drops_hypothetical_boilerplate_and_self_negating_flags(monkeypatch):
    text = (PROSE + "We undertake no obligation to publicly update or revise any forward-looking statements. "
            "For more information, see the section titled Risk Factors in Part II, Item 1A of this report. "
            "A decline in global economic conditions could have adverse effects on our business. ")
    flag = lambda kind, category, summary, quote: {"kind": kind, "category": category, "summary": summary, "quote": quote}
    _llm(monkeypatch, json.dumps({"tone": "neutral", "reason": "Mixed.", "red_flags": [
        flag("reported_problem", "margin_pressure", "Gross margin fell on inventory provisions",
             "gross margin declined due to higher inventory provisions"),
        # The model's own labels: a possibility, and legal boilerplate.
        flag("possible_risk", "macro_fx", "Economy could weaken", "A decline in global economic conditions could have adverse effects"),
        flag("disclaimer", "guidance_cut", "No obligation to update", "We undertake no obligation to publicly update"),
        # Mislabelled, but the quote is boilerplate / the summary negates the flag (INTU 2026-01-31, TXN
        # 2026-06-30, AMD 2026-06-27 in the committed data all had flags like these).
        flag("reported_problem", "guidance_cut", "Guidance withdrawn", "We undertake no obligation to publicly update or revise"),
        flag("reported_problem", "regulatory_legal", "Regulatory risk", "see the section titled Risk Factors in Part II"),
        flag("reported_problem", "guidance_cut", "No guidance cut mentioned", "Revenue increased 18% driven by strong demand"),
        flag("reported_problem", "other", "No specific red flags mentioned in this excerpt.", "Revenue increased 18% driven by strong demand"),
        flag("lowered_outlook", "macro_fx", "Restrictions are hurting shipments, but no details on impact",
             "higher inventory provisions and export restrictions affecting shipments"),
    ]}))
    out = af.analyze_chunk(text, CONTEXT)
    assert [f["category"] for f in out["red_flags"]] == ["margin_pressure"]
    assert out["not_flags"] == 7 and out["dropped"] == 0


@pytest.mark.parametrize("summary", [
    "No guidance cut mentioned", "No liquidity concerns mentioned", "None",
    "No macroeconomic or foreign exchange impact mentioned", "No specific red flags mentioned in this excerpt.",
    "Trade regulations are a potential concern, but no specific details mentioned.",
    "Restructuring charges are mentioned, but no details on impact",
])
def test_negated_summaries_are_not_red_flags(summary):
    assert not af.is_red_flag("reported_problem", summary, "Revenue declined 12% on weaker handset demand")


@pytest.mark.parametrize("summary", [
    "No assurance of investment and partnership agreement with OpenAI",  # NVDA 2026-01-25: a real uncertainty
    "Revenue declined with no recovery expected this year", "Gross margin fell 3 points on inventory charges",
])
def test_real_flags_survive_negation_check(summary):
    assert af.is_red_flag("reported_problem", summary, "Revenue declined 12% on weaker handset demand")


def test_model_output_is_bounded_against_repetition_loops():
    # IBM 0000051143-26-000010: llama3.2:3b repeated one flag until the 900s timeout aborted the run.
    assert af.RESPONSE_SCHEMA["properties"]["red_flags"]["maxItems"] == af.MAX_FLAGS_PER_CHUNK
    worst_case_secs = af.OLLAMA_OPTIONS["num_predict"] / 3  # tokens/s, below the ~4.6 measured on a laptop CPU
    assert worst_case_secs < af.OLLAMA_TIMEOUT


def test_analyze_chunk_retries_bad_json_then_gives_up(monkeypatch):
    _llm(monkeypatch, "not json", json.dumps({"tone": "neutral", "reason": "", "red_flags": []}))
    assert af.analyze_chunk(PROSE, CONTEXT)["tone"] == "neutral"
    _llm(monkeypatch, "nope", "{\"tone\": \"ecstatic\"}")
    assert af.analyze_chunk(PROSE, CONTEXT) is None


def test_combine_weights_tone_by_length_and_dedupes_flags():
    flag = {"category": "supply_chain", "summary": "s", "quote": "q"}
    out = af.combine([
        {"tone": "bearish", "reason": "Weak.", "red_flags": [flag], "dropped": 0, "not_flags": 3, "chars": 5000},
        {"tone": "neutral", "reason": "Flat.", "red_flags": [flag], "dropped": 2, "not_flags": 1, "chars": 1000},
    ])
    assert out["tone"] == "bearish" and out["tone_rationale"] == "Weak."
    assert len(out["red_flags"]) == 1 and out["unverified_flags_dropped"] == 2 and out["non_flags_dropped"] == 4


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
