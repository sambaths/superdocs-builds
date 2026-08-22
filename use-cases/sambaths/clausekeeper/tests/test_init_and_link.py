from conftest import CORPUS, client_for, seeded

from clausekeeper import ingest, linking


def test_init_binds_durable_ids_and_roles(conn):
    client = client_for("init_link.json")
    result = ingest.init_session(client, conn, str(CORPUS))
    assert result["documents"] == 6
    assert result["unbound"] == 0
    rows = conn.execute(
        "SELECT session_slot_id, durable_document_id, role, name"
        " FROM documents").fetchall()
    assert all(r["durable_document_id"] for r in rows)
    roles = {r["session_slot_id"]: r["role"] for r in rows}
    assert roles["doc_qm01"] == "manual"
    assert roles["doc_p04"] == "procedure"
    assert conn.execute("SELECT COUNT(*) c FROM documents WHERE role = 'manual'"
                        ).fetchone()["c"] == 1


def test_link_records_provenance(seeded_conn):
    row = seeded_conn.execute(
        "SELECT l.*, d.name AS doc FROM links l JOIN documents d ON"
        " d.durable_document_id = l.durable_document_id WHERE l.clause_id='8.7'"
        ).fetchone()
    assert row is not None
    assert row["chunk_id"] == "c_ncr_disp"
    assert row["status"] == "covered"
    assert "Disposition" in row["heading_path"]
    assert "QA Manager" in row["quote_excerpt"]
    assert row["last_verified_at"]


def test_seed_holes_detected_by_audit(seeded_conn):
    audit = linking.audit_against_expected(seeded_conn, str(CORPUS /
                                         "expected-gaps.yaml"))
    by_seed = {a["seed"]: a for a in audit}
    assert by_seed["SEED-H1"]["clause_id"] == "9.2"
    assert by_seed["SEED-H1"]["detected"] is True
    assert by_seed["SEED-H2"]["clause_id"] == "7.4"
    assert by_seed["SEED-H2"]["detected"] is True


def test_link_costs_one_op_per_document(conn):
    client = client_for("init_link.json")
    ingest.init_session(client, conn, str(CORPUS))
    from clausekeeper.library import LIBRARY_PATH, load_library
    linking.build_links(client, conn, load_library(LIBRARY_PATH))
    link_calls = [c for c in client.t.calls if c == ("POST", "/v1/chat")]
    assert len(link_calls) == 6
    assert len(client.usage_log) == 6
    assert all(u["ops_charged"] == 1 for u in client.usage_log)
