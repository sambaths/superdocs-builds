import yaml
from pathlib import Path

from . import db, guards, ingest, narrative


def detect_changed(client, conn) -> dict:
    session_id = db.get_meta(conn, "session_id")
    cursor = int(db.get_meta(conn, "doc_events_cursor", "0"))
    feed = client.doc_events(session_id, after_id=cursor)

    changed, renamed, skipped_echo, skipped_roles = {}, 0, 0, 0
    max_event = cursor
    for event in feed.get("events", []):
        event_id = int(event.get("event_id", max_event))
        max_event = max(max_event, event_id)
        slot = event.get("document_id")
        row = conn.execute(
            "SELECT * FROM documents WHERE session_slot_id = ?",
            (slot,)).fetchone()
        if not row:
            continue
        etype = event.get("type", "")
        if etype == "document_renamed":
            conn.execute("UPDATE documents SET name = ? WHERE"
                         " session_slot_id = ?",
                         (event.get("new_name") or row["name"], slot))
            renamed += 1
            continue
        if guards.is_echo(event, conn):
            skipped_echo += 1
            continue
        if not guards.role_allows(dict(row)):
            skipped_roles += 1
            continue
        changed[slot] = dict(row)
    conn.commit()
    db.set_meta(conn, "doc_events_cursor", str(max_event))

    roster_by_slot = {}
    for entry in client.roster(session_id, include_html=True,
                               report_changed=True):
        roster_by_slot[entry.get("document_id")] = entry
        slot = entry.get("document_id")
        if not entry.get("changed"):
            continue
        row = conn.execute(
            "SELECT * FROM documents WHERE session_slot_id = ?",
            (slot,)).fetchone()
        if row and guards.role_allows(dict(row)):
            changed[slot] = dict(row)

    checked_now, already_checked = [], []
    for slot, doc in changed.items():
        html = (roster_by_slot.get(slot) or {}).get("html", "")
        new_hash = ingest.content_hash(html)
        doc["version_hash"] = new_hash
        run_key = f"{slot}:{new_hash}"
        doc["run_key"] = run_key
        if guards.already_run(conn, run_key):
            already_checked.append(doc)
        else:
            checked_now.append(doc)
    return {"changed": checked_now, "already_checked": already_checked,
            "renamed": renamed, "skipped_echo": skipped_echo,
            "skipped_roles": skipped_roles}


def affected_clauses(conn, durable_id) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT l.clause_id, c.title, c.expectation FROM links l "
        "JOIN clauses c ON c.clause_id = l.clause_id "
        "WHERE l.durable_document_id = ?", (durable_id,)).fetchall()
    return [dict(r) for r in rows]


def build_plan(client, conn, detection: dict, scope_clauses=None) -> dict:
    items = []
    est_ops = 0
    for doc in detection["changed"]:
        clauses = affected_clauses(conn, doc["durable_document_id"])
        if scope_clauses:
            allowed = set(scope_clauses)
            clauses = [c for c in clauses if c["clause_id"] in allowed]
        if not clauses:
            continue
        est_ops += 1
        items.append({"name": doc["name"], "slot": doc["session_slot_id"],
                      "clauses": [c["clause_id"] for c in clauses],
                      "estimated_ops": 1})
    return {"items": items, "estimated_ops": est_ops,
            "skips": len(detection["already_checked"])}


VERIFY_PROMPT = (
    "Re-verify this document against the listed ISO 9001:2015 clauses after a "
    "recent edit. For each clause decide covered or gap based ONLY on current "
    "document content. Respond with ONLY a JSON array: [{\"clause_id\": \"...\","
    " \"status\": \"covered\"|\"gap\", \"chunk_id\": \"<data-chunk-id or null>\","
    " \"heading_path\": \"<heading or null>\", \"quote\": \"<short quote or"
    " null>\"}]. Clauses: {clauses}"
)


def run_recheck(client, conn, scope_clauses=None) -> dict:
    detection = detect_changed(client, conn)
    session_id = db.get_meta(conn, "session_id")
    ops_before = sum(u["ops_charged"] for u in client.usage_log)
    turns = 0
    results = []
    for doc in detection["changed"]:
        if turns >= config_max_turns():
            results.append({"name": doc["name"], "status": "deferred",
                            "reason": "max-turns cap"})
            continue
        clauses = affected_clauses(conn, doc["durable_document_id"])
        if scope_clauses:
            allowed = set(scope_clauses)
            clauses = [c for c in clauses if c["clause_id"] in allowed]
        if not clauses:
            results.append({"name": doc["name"], "status": "skipped",
                            "reason": "no affected clauses in scope"})
            continue
        turns += 1
        clause_list = "; ".join(
            f"{c['clause_id']} ({c['title']}): {c['expectation']}"
            for c in clauses) or "(no linked clauses)"
        resp = client.chat(
            VERIFY_PROMPT.replace("{clauses}", clause_list), session_id,
            document_id=doc["session_slot_id"])
        verdicts = parse_verdicts(resp.get("response", ""))
        job_id = resp.get("job_id") or "sync-chat"
        now = db.now()
        gaps_created = _apply_verdicts(conn, doc, verdicts, job_id, now)
        charged = _ops(resp)
        guards.mark_run(conn, doc["run_key"], "recheck", charged)
        results.append({"name": doc["name"], "clauses_checked": len(clauses),
                        "gaps": gaps_created, "ops_charged": charged})
    total_ops = sum(u["ops_charged"] for u in client.usage_log) - ops_before
    return {"results": results, "ops_spent": total_ops,
            "already_checked": [d["name"] for d in detection["already_checked"]],
            "echo_suppressed": detection["skipped_echo"],
            "roles_filtered": detection["skipped_roles"]}


def config_max_turns() -> int:
    from . import config
    return config.MAX_TURNS_PER_RUN


def parse_verdicts(text: str) -> list[dict]:
    from .superdocs import parse_json_block
    out = parse_json_block(text)
    return out if isinstance(out, list) else []


def _ops(resp: dict) -> int:
    return int((resp.get("usage") or {}).get("ops_charged", 0))


def _apply_verdicts(conn, doc, verdicts, job_id, now) -> int:
    gaps_created = 0
    for v in verdicts:
        cid = str(v.get("clause_id"))
        status = str(v.get("status", "")).lower()
        if status == "covered":
            chunk_id = v.get("chunk_id") or "unknown"
            heading = v.get("heading_path") or "(section)"
            quote = (v.get("quote") or "")[:200]
            existing = conn.execute(
                "SELECT link_id FROM links WHERE clause_id = ? AND"
                " durable_document_id = ? AND chunk_id = ?",
                (cid, doc["durable_document_id"], chunk_id)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE links SET status='covered', heading_path=?,"
                    " quote_excerpt=?, last_verified_at=?, last_verified_job=?"
                    " WHERE link_id=?",
                    (heading, quote, now, job_id, existing["link_id"]))
                conn.execute(
                    "DELETE FROM gaps WHERE clause_id = ? AND"
                    " durable_document_id = ?",
                    (cid, doc["durable_document_id"]))
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO links(clause_id,"
                    " durable_document_id, chunk_id, heading_path,"
                    " quote_excerpt, status, last_verified_at,"
                    " last_verified_job) VALUES(?,?,?,?,?,'covered',?,?)",
                    (cid, doc["durable_document_id"], chunk_id, heading,
                     quote, now, job_id))
        elif status == "gap":
            updated = conn.execute(
                "UPDATE links SET status='gap', last_verified_at=?,"
                " last_verified_job=? WHERE clause_id=? AND"
                " durable_document_id=? AND status != 'gap'",
                (now, job_id, cid, doc["durable_document_id"]))
            if updated.rowcount > 0 and not conn.execute(
                    "SELECT 1 FROM gaps WHERE clause_id = ? AND"
                    " durable_document_id = ?",
                    (cid, doc["durable_document_id"])).fetchone():
                cause = conn.execute(
                    "SELECT edit_id FROM edits WHERE durable_document_id = ?"
                    " ORDER BY edit_id DESC LIMIT 1",
                    (doc["durable_document_id"],)).fetchone()
                text = narrative.verification_gap_narrative(cid, job_id, now)
                conn.execute(
                    "INSERT INTO gaps(clause_id, durable_document_id,"
                    " cause_edit_id, detected_at, narrative) VALUES(?,?,?,?,?)",
                    (cid, doc["durable_document_id"],
                     cause["edit_id"] if cause else 0, now, text))
                gaps_created += 1
    conn.commit()
    return gaps_created


def load_scope(expected_path=None):
    if expected_path and Path(expected_path).exists():
        raw = yaml.safe_load(Path(expected_path).read_text())
        seeds = [str(s["clause_id"]) for s in raw.get("seeds", [])]
        scripted = raw.get("scripted_edit", {})
        if scripted.get("expected_gap_clause"):
            seeds.append(str(scripted["expected_gap_clause"]))
        return seeds
    return None
