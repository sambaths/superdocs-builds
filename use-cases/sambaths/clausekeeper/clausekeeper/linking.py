import yaml

from . import db, ingest
from .superdocs import parse_json_block

LINK_PROMPT = (
    "You are mapping a quality-system document to ISO 9001:2015 clauses. For each "
    "clause below, decide whether THIS document contains a section that evidences it. "
    "Respond with ONLY a JSON array, one object per clause: "
    '[{"clause_id": "...", "status": "covered"|"not-covered", "chunk_id": "<the '
    'data-chunk-id of the evidencing section>", "heading_path": "<section heading>", '
    '"quote": "<short verbatim quote proving coverage>"}]. Clauses: {clauses}'
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
        chunks = ingest.extract_chunks(html)
        clause_list = "; ".join(
            f"{c['id']} ({c['title']})" for c in clauses if c["linkable"])
        resp = client.chat_wait(
            LINK_PROMPT.replace("{clauses}", clause_list), session_id,
            document_id=doc["session_slot_id"],
            latency_class="verification_turn")
        mappings = parse_json_block(resp.get("response", ""))
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
                        "ops_charged": _ops(resp)})
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
