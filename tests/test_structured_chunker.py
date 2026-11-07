"""Black-box structure-preserving chunking checks; no DB, model or network calls."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from litreview.chunker import split_markdown


DOC_ID = "structured-fixture"


def structured(md, chunk_size=300, max_chars=5000):
    return split_markdown(
        DOC_ID, md, chunk_size=chunk_size, overlap=50,
        strategy="structured", max_chars=max_chars,
    )


def table_chunks(chunks):
    return [c for c in chunks if "| Catalyst |" in c.content]


def test_table_is_separate_from_surrounding_prose():
    table = "| Catalyst | Yield (%) |\n|---|---|\n| SAMPLE_A | 87.3 |"
    chunks = structured(
        "# Results\n\nINTRO_MARKER: the screening experiment is described here.\n\n"
        "Table 1. Catalyst screening\n\n" + table +
        "\n\nOUTRO_MARKER: the implications are discussed in the following paragraph."
    )
    tables = table_chunks(chunks)
    assert len(tables) == 1
    assert table in tables[0].content
    assert "Table 1. Catalyst screening" in tables[0].content
    assert "INTRO_MARKER" not in tables[0].content
    assert "OUTRO_MARKER" not in tables[0].content
    assert "INTRO_MARKER" in "\n".join(c.content for c in chunks)
    assert "OUTRO_MARKER" in "\n".join(c.content for c in chunks)


def test_long_markdown_table_repeats_context_and_preserves_every_row():
    header = "| Catalyst | Temperature (K) | Yield (%) |"
    separator = "|---|---|---|"
    caption = "Table 2. Screening at fixed pressure"
    rows = [f"| SAMPLE_{i:02d} | {300 + i} | {70 + i}.25 |" for i in range(20)]
    chunks = structured(
        "# Results\n\n" + caption + "\n\n" + header + "\n" +
        separator + "\n" + "\n".join(rows), chunk_size=240,
    )
    tables = table_chunks(chunks)
    assert len(tables) > 1
    for chunk in tables:
        assert caption in chunk.content
        assert header in chunk.content
        assert separator in chunk.content
    joined = "\n".join(c.content for c in tables)
    for row in rows:
        assert joined.count(row) == 1


def test_html_table_groups_complete_rows_and_repeats_th_header():
    header = "<tr><th>Catalyst</th><th>Yield (%)</th></tr>"
    rows = [f"<tr><td>HTML_SAMPLE_{i:02d}</td><td>{80 + i}.5</td></tr>" for i in range(12)]
    chunks = structured(
        "# Results\n\nTable 3. HTML screening\n\n<table>" +
        header + "".join(rows) + "</table>", chunk_size=280,
    )
    tables = [c for c in chunks if "<table" in c.content]
    assert len(tables) > 1
    for chunk in tables:
        assert header in chunk.content
        assert chunk.content.count("<table") == chunk.content.count("</table>") == 1
        assert chunk.content.count("<tr>") == chunk.content.count("</tr>")
        assert chunk.content.count("<td>") == chunk.content.count("</td>")
    joined = "\n".join(c.content for c in tables)
    for row in rows:
        assert joined.count(row) == 1


def test_html_first_row_is_repeated_when_no_th_header_exists():
    header = "<tr><td>Catalyst</td><td>Yield (%)</td></tr>"
    rows = [f"<tr><td>FIRST_ROW_SAMPLE_{i:02d}</td><td>{60 + i}</td></tr>" for i in range(10)]
    chunks = structured("<table>" + header + "".join(rows) + "</table>", chunk_size=240)
    tables = [c for c in chunks if "<table" in c.content]
    assert len(tables) > 1
    assert all(header in c.content for c in tables)
    assert all(sum(c.content.count(row) for c in tables) == 1 for row in rows)


def test_html_rowspan_connected_rows_are_kept_together():
    header = "<tr><th>Catalyst</th><th>Observation</th></tr>"
    first = '<tr><td rowspan="2">CONNECTED_A</td><td>' + "x" * 180 + "</td></tr>"
    second = "<tr><td>CONNECTED_B " + "y" * 180 + "</td></tr>"
    third = "<tr><td>SEPARATE_C</td><td>independent measurement</td></tr>"
    chunks = structured(
        "<table>" + header + first + second + third + "</table>",
        chunk_size=280,
    )
    connected = [c for c in chunks if "CONNECTED_A" in c.content or "CONNECTED_B" in c.content]
    assert len(connected) == 1
    assert first in connected[0].content and second in connected[0].content
    assert sum(c.content.count(third) for c in chunks) == 1


def test_html_rowspan_group_over_hard_budget_raises():
    first = '<tr><td rowspan="2">GROUP_A</td><td>' + "a" * 2600 + "</td></tr>"
    second = "<tr><td>GROUP_B " + "b" * 2600 + "</td></tr>"
    with pytest.raises(ValueError):
        structured("<table><tr><th>Group</th><th>Value</th></tr>" + first + second + "</table>")


def test_display_math_delimiters_and_environment_are_never_split():
    expression = (
        r"\begin{aligned}" + "\n" +
        "\n".join(r"k_{%d} &= A_{%d}\exp(-E_{%d}/RT) \\" % (i, i, i) for i in range(10)) +
        "\n" + r"\end{aligned}"
    )
    for formula in ("$$\n" + expression + "\n$$", "\\[\n" + expression + "\n\\]"):
        chunks = structured("# Kinetics\n\n" + formula, chunk_size=200)
        containing = [c for c in chunks if r"\begin{aligned}" in c.content or r"\end{aligned}" in c.content]
        assert len(containing) == 1
        assert formula in containing[0].content


def test_formula_keeps_adjacent_variable_definitions():
    formula = r"$$ k = A\exp(-E_a/(RT)) $$"
    for definition in (
        "where A is the pre-exponential factor and E_a is the activation energy.",
        "式中，A 为指前因子，E_a 为活化能，R 为气体常数，T 为绝对温度。",
    ):
        chunks = structured("# Kinetics\n\n" + formula + "\n\n" + definition, chunk_size=70)
        matches = [c for c in chunks if formula in c.content]
        assert len(matches) == 1
        assert definition in matches[0].content


def test_nested_section_context_keeps_ancestors_and_drops_previous_sibling():
    chunks = structured(
        "# Catalysis\n\n## Methods\n\n### Temperature\n\n"
        "TEMPERATURE_MARKER: experiments used a precisely controlled thermal bath.\n\n"
        "## Results\n\nRESULTS_MARKER: conversion increased with catalyst loading."
    )
    method = next(c for c in chunks if "TEMPERATURE_MARKER" in c.content)
    for title in ("Catalysis", "Methods", "Temperature"):
        assert title in method.section_title
        assert title in method.content
    result = next(c for c in chunks if "RESULTS_MARKER" in c.content)
    assert "Catalysis" in result.section_title and "Results" in result.section_title
    assert "Methods" not in result.section_title and "Temperature" not in result.section_title
    assert all(c.doc_id == DOC_ID for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_oversized_indivisible_formula_or_line_raises():
    for md in ("$$\n" + "z" * 5001 + "\n$$", "\\[\n" + "z" * 5001 + "\n\\]", "z" * 5001):
        with pytest.raises(ValueError):
            structured(md)


def test_hard_budget_includes_hierarchy_caption_and_repeated_header():
    parent = "Parent " + "p" * 45
    child = "Child " + "c" * 45
    caption = ("Table 4. " + "caption " * 5).rstrip()
    header = "| Catalyst | Yield (%) |"
    rows = [f"| BUDGET_SAMPLE_{i:02d}_" + "x" * 90 + " | 88.5 |" for i in range(6)]
    chunks = structured(
        "# " + parent + "\n\n## " + child + "\n\n" + caption + "\n\n" +
        header + "\n|---|---|\n" + "\n".join(rows), chunk_size=1000, max_chars=400,
    )
    tables = table_chunks(chunks)
    assert len(tables) > 1
    assert all(len(c.content) <= 400 for c in chunks)
    for chunk in tables:
        for context in (parent, child, caption, header):
            assert context in chunk.content
    assert all(sum(c.content.count(row) for c in tables) == 1 for row in rows)


def test_default_and_explicit_legacy_keep_existing_table_behavior():
    header = "| Catalyst | Temperature (K) | Yield (%) |"
    rows = [f"| catalyst_{i:02d} | 323 | {70 + i % 20} |" for i in range(70)]
    md = "# Results\n\n" + header + "\n|---|---|---|\n" + "\n".join(rows)
    default = split_markdown(DOC_ID, md, chunk_size=1000, overlap=150)
    legacy = split_markdown(DOC_ID, md, chunk_size=1000, overlap=150, strategy="legacy")
    assert default == legacy
    assert len(default) == 2
    assert header in default[0].content
    assert header not in default[1].content
    assert rows[-1] in default[1].content


def test_prose_preserves_decimals_abbreviations_and_inline_math_as_source_slices():
    from litreview.chunker import clean_markdown

    md = "# Measurements\n\n" + (
        "A value of 3.52 mg/kg was measured, e.g. in Fig. 2. "
        "The preparation uses $c=0.75$ and 0.1 g of catalyst. "
    ) * 12
    source = clean_markdown(md)
    chunks = split_markdown(DOC_ID, md, 170, 0, strategy="structured")
    assert not any("3.\n52" in c.content or "0.\n1" in c.content for c in chunks)
    assert sum(c.content.count("$c=0.75$") for c in chunks) == 12
    for chunk in chunks:
        assert len(chunk.source_spans) == 1
        a, b = chunk.source_spans[0]
        assert source[a:b].strip() in chunk.content


def test_colspan_headers_and_source_rows_are_preserved_with_notes():
    md = (
        "# Results\n\nTable 6. Screening\n\n<table>"
        '<tr><td colspan="2">Conversion (%)</td></tr>'
        "<tr><td>Catalyst</td><td>Yield</td></tr>" +
        "".join(f"<tr><td>DATA_{i}</td><td>{i}.52</td></tr>" for i in range(12)) +
        "</table>\n\nNote: All measurements at 0.1 MPa."
    )
    chunks = structured(md, chunk_size=250)
    assert len(chunks) > 1
    for chunk in chunks:
        assert 'colspan="2"' in chunk.content
        assert "<td>Catalyst</td><td>Yield</td>" in chunk.content
        assert "Note: All measurements at 0.1 MPa." in chunk.content
        assert chunk.header_inferred
    for i in range(12):
        assert sum(c.content.count(f"<td>DATA_{i}</td>") for c in chunks) == 1


def test_missing_caption_does_not_invent_table_title():
    table = "<table><tr><td>Item</td><td>Value</td></tr>" + "".join(
        f"<tr><td>ROW_{i}</td><td>1.75</td></tr>" for i in range(12)
    ) + "</table>"
    chunks = structured("# Results\n\n" + table, chunk_size=180)
    assert all("Table " not in c.content for c in chunks)
    assert all("Results" in c.section_title for c in chunks)


def test_definitions_do_not_swallow_the_next_formula_or_heading():
    md = "$$a=0.1$$\n\nwhere a is the measured activity.\n## Next\n$$b=3.52$$"
    chunks = structured(md)
    assert sum(c.content.count("$$a=0.1$$") for c in chunks) == 1
    assert sum(c.content.count("$$b=3.52$$") for c in chunks) == 1
    b = next(c for c in chunks if "$$b=3.52$$" in c.content)
    assert b.section_title == "Next"


def test_code_fence_is_not_parsed_as_table_math_or_section():
    code = "```text\n# Not a section\n<table>\n$$ broken source example\n```"
    chunks = structured("# Actual section\n\n" + code)
    assert len(chunks) == 1
    assert chunks[0].kind == "code"
    assert code in chunks[0].content
    assert chunks[0].section_title == "Actual section"


@pytest.mark.parametrize("md", ["$$x=1", r"\begin{align} x=1 \end{equation}",
                                    "<table><tr><td>x</td></tr>"])
def test_malformed_source_is_explicit_error(md):
    with pytest.raises(ValueError):
        structured(md)


def test_offline_preview_reports_unsafe_source_and_versions_the_configuration():
    from litreview.chunk_preview import preview

    md = "# Sample\n\nA paragraph with a measured yield of 3.52 percent. " * 5
    first = preview(md, chunk_size=100)
    second = preview(md, chunk_size=200)
    assert first["status"] == "ok"
    assert first["source_sha256"] == second["source_sha256"]
    assert first["preview_fingerprint"] != second["preview_fingerprint"]
    bad = preview("$$" + "x" * 5001 + "$$")
    assert bad["status"] == "needs_source_review"
    assert bad["chunks"] == []
    assert bad["error"]


def test_formula_and_short_prose_pack_together_without_losing_context():
    md = (
        "# Kinetics\n\nThe reaction rate is expressed by:\n\n"
        "$$ k = A\\exp(-E_a/(RT)) $$\n\n"
        "where A is the pre-exponential factor.\n\n"
        "This expression applies to the measured temperature range."
    )
    chunks = structured(md, chunk_size=1000)
    assert len(chunks) == 1
    assert "The reaction rate" in chunks[0].content
    assert "$$ k = A\\exp(-E_a/(RT)) $$" in chunks[0].content
    assert "measured temperature range" in chunks[0].content
    assert chunks[0].kind == "text_formula"


def test_chinese_prose_slices_at_sentence_boundaries_without_rejoining():
    from litreview.chunker import clean_markdown

    md = "# 实验结果\n\n" + "添加0.1克催化剂后，反应转化率达到83.52%，数据支持上述推断。" * 30
    chunks = split_markdown(DOC_ID, md, 180, 0, strategy="structured")
    source = clean_markdown(md)
    assert len(chunks) > 1
    assert all(len(c.content) <= 180 for c in chunks)
    for chunk in chunks:
        a, b = chunk.source_spans[0]
        assert source[a:b].strip() in chunk.content
    assert sum(c.content.count("83.52%") for c in chunks) == 30


def test_large_caption_or_definition_does_not_exhaust_a_structure_budget():
    long_caption = "Table 1 shows that " + "a useful observation " * 50
    long_definition = "where the explanatory paragraph contains " + "additional information " * 50
    table = "| Item | Value |\n|---|---|\n| SAMPLE | 3.52 |"
    chunks = structured(long_caption + "\n\n" + table + "\n\n$$k=0.1$$\n\n" + long_definition)
    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) == 1
    assert "Table 1 shows" not in tables[0].content
    assert sum(c.content.count("$$k=0.1$$") for c in chunks) == 1
    assert "additional information" in "\n".join(c.content for c in chunks)


def test_small_irregular_table_is_preserved_with_explicit_warning():
    table = "<table><tr><td>Header A</td><td>Header B</td></tr>" + (
        "<tr><td>Uncertain source row with one cell and a value of 3.52</td></tr>"
    ) * 3 + "</table>"
    chunks = structured(table, chunk_size=100)
    assert len(chunks) == 1
    assert table in chunks[0].content
    assert chunks[0].warnings


def test_named_html_headers_are_not_marked_as_inferred():
    table = "<table><tr><th>Item</th><th>Value</th></tr>" + "".join(
        f"<tr><td>DATA_{i}</td><td>3.52</td></tr>" for i in range(10)
    ) + "</table>"
    chunks = structured(table, chunk_size=200)
    assert all(c.header_rows == 1 and not c.header_inferred for c in chunks)


def test_table_budget_is_separate_from_prose_budget():
    table = "<table><tr><td>Item</td><td>Value</td></tr>" + "".join(
        f"<tr><td>DATA_{i}</td><td>3.52</td></tr>" for i in range(30)
    ) + "</table>"
    narrow = structured(table, chunk_size=200)
    wide = split_markdown(DOC_ID, table, 200, strategy="structured", table_chunk_size=1000)
    assert len(wide) < len(narrow)
    assert all(len(c.content) <= 1000 for c in wide)
