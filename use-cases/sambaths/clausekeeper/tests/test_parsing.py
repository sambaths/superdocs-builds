from clausekeeper import ingest
from clausekeeper.superdocs import parse_json_block

SAMPLE = ("<h1>Procedure NM-PRO-04</h1>"
          '<div data-chunk-id="c87_hold"><h2>Identifying and containing'
          ' nonconforming output</h2><p>Suspect product is quarantined.</p></div>'
          '<div data-chunk-id="c_ncr_disp"><h2>Disposition of nonconforming'
          ' devices</h2><p>Dispositions are decided by the QA Manager.</p></div>'
          '<div data-chunk-id="c102_capa"><h2>Corrective action</h2><p>Root-cause'
          ' analysis is documented in the CAPA record.</p></div>')


def test_chunks_carry_their_own_section_heading():
    chunks = ingest.extract_chunks(SAMPLE)
    by_id = {c["chunk_id"]: c for c in chunks}
    assert "Identifying" in by_id["c87_hold"]["heading_path"]
    assert "Disposition of nonconforming devices" in \
        by_id["c_ncr_disp"]["heading_path"]
    assert "Corrective action" in by_id["c102_capa"]["heading_path"]


def test_resolver_matches_leaf_heading_with_numbering():
    chunks = ingest.extract_chunks(SAMPLE)
    hit = ingest.find_chunk_by_heading(
        chunks, "3 Disposition of nonconforming devices")
    assert hit is not None
    assert hit["chunk_id"] == "c_ncr_disp"


def test_resolver_rejects_unknown_headings():
    chunks = ingest.extract_chunks(SAMPLE)
    assert ingest.find_chunk_by_heading(chunks, "Employee Handbook") is None
    assert ingest.find_chunk_by_heading(chunks, None) is None


def test_parser_accepts_dict_wrapped_arrays():
    text = 'Sure. {"mappings": [{"clause_id": "8.7", "status": "gap"}]}'
    out = parse_json_block(text)
    assert isinstance(out, list)
    assert out[0]["clause_id"] == "8.7"


def test_parser_prefers_fenced_array_and_reports_garbage():
    out = parse_json_block('note [1] here\n```json\n[{"a": 1}]\n```')
    assert out == [{"a": 1}]
    import pytest
    with pytest.raises(ValueError):
        parse_json_block("no structure at all in this reply")


def test_keyfile_parser_handles_export_and_comments():
    from clausekeeper.config import _parse_env_line
    assert _parse_env_line("export SUPERDOCS_API_KEY=sk_x # note") == \
        ("SUPERDOCS_API_KEY", "sk_x")
    assert _parse_env_line("PLAIN=\"abc\"") == ("PLAIN", "abc")
    assert _parse_env_line("# comment") is None
    assert _parse_env_line("no equals here") is None
