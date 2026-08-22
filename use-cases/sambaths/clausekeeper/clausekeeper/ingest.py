import hashlib
import re
from pathlib import Path

from . import db


def role_for(filename: str) -> str:
    return "manual" if "manual" in filename.lower() else "procedure"


def init_session(client, conn, corpus_dir: str) -> dict:
    session = client.create_session()
    session_id = session["session_id"]
    db.set_meta(conn, "session_id", session_id)

    corpus = Path(corpus_dir)
    for path in sorted(corpus.glob("*.md")):
        upload = client.upload_document(session_id, str(path),
                                        open_mode="new_focused")
        slot_id = upload.get("document_id") or upload.get("doc_id")
        if not slot_id:
            raise RuntimeError(f"upload response missing document id: "
                               f"{list(upload)}")
        client.save_document(session_id, slot_id,
                             upload.get("html", ""), upload.get("html", ""))
        conn.execute(
            "INSERT OR REPLACE INTO documents(session_slot_id, role, name, "
            "version_hash, bound_at) VALUES(?, ?, ?, ?, ?)",
            (slot_id, role_for(path.name), path.stem, "", db.now()))

    bind_durable_ids(client, conn)
    roster = {d["document_id"]: d for d in
              client.roster(session_id, include_html=True)}
    for row in conn.execute("SELECT session_slot_id FROM documents").fetchall():
        entry = roster.get(row["session_slot_id"], {})
        html = entry.get("html", "")
        conn.execute(
            "UPDATE documents SET version_hash = ? WHERE session_slot_id = ?",
            (content_hash(html), row["session_slot_id"]))
    conn.commit()
    count = conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    unbound = conn.execute(
        "SELECT COUNT(*) c FROM documents WHERE durable_document_id IS NULL"
    ).fetchone()["c"]
    return {"session_id": session_id, "documents": count, "unbound": unbound}


def bind_durable_ids(client, conn):
    session_id = db.get_meta(conn, "session_id")
    for entry in client.roster(session_id):
        durable = entry.get("durable_document_id")
        if not durable:
            continue
        conn.execute(
            "UPDATE documents SET durable_document_id = ?, name = COALESCE(?, name)"
            ", bound_at = COALESCE(bound_at, ?) WHERE session_slot_id = ?",
            (durable, entry.get("name"), db.now(), entry["document_id"]))
    conn.commit()


def content_hash(html: str) -> str:
    return hashlib.sha256((html or "").encode()).hexdigest()


CHUNK_RE = re.compile(r'data-chunk-id="([^"]+)"')
HEADING_RE = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.S)
TAG_RE = re.compile(r"<[^>]+>")


def extract_chunks(html: str) -> list[dict]:
    chunks = []
    positions = [(m.start(), m.group(1)) for m in CHUNK_RE.finditer(html)]
    headings = [(m.start(), int(m.group(1)), TAG_RE.sub("", m.group(2)).strip())
                for m in HEADING_RE.finditer(html)]

    def heading_at(pos: int) -> str:
        stack: dict[int, str] = {}
        parts = []
        for hpos, level, text in headings:
            if hpos > pos:
                break
            stack[level] = text
            stack = {k: v for k, v in stack.items() if k <= level}
        for lvl in sorted(stack):
            parts.append(stack[lvl])
        return " > ".join(parts) if parts else "(front matter)"

    for idx, (pos, chunk_id) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(html)
        body = html[pos:end]
        text = TAG_RE.sub(" ", body)
        quote = re.sub(r"\s+", " ", text).strip()
        chunks.append({"chunk_id": chunk_id, "heading_path": heading_at(pos),
                       "quote_excerpt": quote[:200]})
    return chunks
