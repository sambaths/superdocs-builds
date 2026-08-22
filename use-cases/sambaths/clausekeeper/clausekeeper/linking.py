import json as _json

import yaml

from . import db, ingest
from .superdocs import parse_json_block

LINK_PROMPT = (
    "Context: you are helping QA staff at Northgate Medical Devices, a fictional "
    "demo company, build an internal traceability matrix that maps their own "
    "quality procedures to ISO 9001:2015 clauses. This is ordinary document "
    "indexing work; their procedures discuss routine quality topics such as "
    "document control, competence training, supplier scorecards, and control of "
    "nonconforming product.\n\n"
    "The document currently in focus is one of their internal procedures. For "
    "each clause listed below, decide whether THIS document contains a section "
    "that provides evidence for it.\n\n"
    "Reply with a single JSON array covering EVERY clause in the same order, "
    "each element shaped like:\n"
    '[{"clause_id": "7.5", "status": "covered", "chunk_id": "<data-chunk-id '
    'attribute of the evidencing block>", "heading_path": "<section heading>", '
    '"quote": "<a short supporting quote copied from the document>"}]\n'
    'Use "not-covered" as the status when a clause has no supporting section - '
    "even if that is true for every clause, still return the full array with "
    'status "not-covered" throughout. Copy quotes verbatim so auditors can '
    "verify them.\n\n"
    "End your reply with the complete JSON array inside a ```json fenced code "
    "block.\n\n"
    "Clauses: {clauses}"
)


def build_links(client, conn, clauses: list[dict]):
    session_id = db.get_meta(conn, "session_id")
    results = []
    docs = conn.execute(
        "SELECT * FROM documents WHERE role != 'generated' "
        "AND durable_document_id IS NOT NULL").fetchall()
    roster = {d["document_id"]: d for d in
              client.roster(session_id, include_html=True)}
    for doc in docs:
        entry = roster.get(doc["session_slot_id"], {})
        html = entry.get("html") or ""
        if not html.strip():
            results.append({"doc": doc["name"], "links_added": 0,
                            "ops_charged": 0, "parse_failed": False,
                            "skipped": "no html in roster"})
            continue
        chunks = ingest.extract_chunks(html)
        clause_list = "; ".join(
            f"{c['id']} ({c['title']})" for c in clauses if c["linkable"])
        resp = client.chat_wait(
            LINK_PROMPT.replace("{clauses}", clause_list), session_id,
            document_id=doc["session_slot_id"],
            latency_class="verification_turn")
        try:
            mappings = parse_json_block(resp.get("response", ""))
            if not isinstance(mappings, list):
                raise ValueError("mapping is not a JSON array")
        except (ValueError, _json.JSONDecodeError) as exc:
            print(f"WARN: could not parse mapping for {doc['name']}: {exc}")
            results.append({"doc": doc["name"], "links_added": 0,
                            "ops_charged": _ops(resp), "parse_failed": True})
            continue
        by_chunk = {c["chunk_id"]: c for c in chunks}
        job_id = resp.get("job_id") or "sync-chat"
        now = db.now()
        added = 0
        for m in mappings:
            if str(m.get("status", "")).lower() != "covered":
                continue
            chunk = by_chunk.get(str(m.get("chunk_id")))
            if not chunk:
                continue
            heading = m.get("heading_path") or chunk["heading_path"]
            quote = m.get("quote") or chunk["quote_excerpt"]
            conn.execute(
                "INSERT OR IGNORE INTO links(clause_id, durable_document_id, "
                "chunk_id, heading_path, quote_excerpt, status, last_verified_at,"
                " last_verified_job) VALUES(?, ?, ?, ?, ?, 'covered', ?, ?)",
                (str(m["clause_id"]), doc["durable_document_id"],
                 chunk["chunk_id"], heading, quote[:200], now, job_id))
            added += 1
        conn.commit()
        results.append({"doc": doc["name"], "links_added": added,
                        "ops_charged": _ops(resp), "parse_failed": False})
    return results


def _ops(resp: dict) -> int:
    return int((resp.get("usage") or {}).get("ops_charged", 0))


def audit_against_expected(conn, expected_path):
    linked = {r["clause_id"] for r in conn.execute(
        "SELECT DISTINCT clause_id FROM links WHERE status != 'gap'")}
    uncovered = []
    raw = yaml.safe_load(__import__("pathlib").Path(expected_path).read_text())
    for seed in raw.get("seeds", []):
        cid = str(seed["clause_id"])
        uncovered.append({"seed": seed["id"], "clause_id": cid,
                          "detected": cid not in linked})
    return uncovered
