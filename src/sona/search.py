"""Full-text search over transcripts, subtitles and manuscripts (SQLite FTS5)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MAX_QUERY_LENGTH = 200
SNIPPET_RADIUS = 40

_SOURCES = {
    "transcription": ("transcription_results", "audio_id",
                      "COALESCE((SELECT name FROM audio_files WHERE id=NEW.audio_id), '')", "NEW.text", None),
    "subtitle": ("subtitle_results", "import_id",
                 "COALESCE((SELECT name FROM podcast_imports WHERE id=NEW.import_id), '')", "NEW.text", None),
    "manuscript": ("manuscripts", "id", "NEW.title", "NEW.body", "NEW.status='completed'"),
}


def _create_table(connection) -> None:
    try:
        connection.execute("""CREATE VIRTUAL TABLE search_index USING fts5(
            kind UNINDEXED, ref_id UNINDEXED, title, body, tokenize='trigram')""")
    except sqlite3.OperationalError:
        # SQLite without FTS5/trigram: keep the same columns and search with LIKE.
        connection.execute("CREATE TABLE search_index(kind TEXT, ref_id TEXT, title TEXT, body TEXT)")


def ensure_index(connection, *, rebuild: bool = False) -> None:
    """Create the index and its triggers; backfill when new or when asked."""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='search_index'").fetchone()
    if not exists:
        _create_table(connection)
        rebuild = True
    for kind, (table, key, title, body, condition) in _SOURCES.items():
        guard = f"WHERE {condition}" if condition else ""
        insert = (f"INSERT INTO search_index(kind, ref_id, title, body) "
                  f"SELECT '{kind}', NEW.{key}, {title}, {body} {guard};")
        remove_new = f"DELETE FROM search_index WHERE kind='{kind}' AND ref_id=NEW.{key};"
        remove_old = f"DELETE FROM search_index WHERE kind='{kind}' AND ref_id=OLD.{key};"
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS search_{kind}_insert AFTER INSERT ON {table}
            BEGIN {remove_new} {insert} END""")
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS search_{kind}_update AFTER UPDATE ON {table}
            BEGIN {remove_old} {remove_new} {insert} END""")
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS search_{kind}_delete AFTER DELETE ON {table}
            BEGIN {remove_old} END""")
    if rebuild:
        connection.execute("DELETE FROM search_index")
        connection.execute("""INSERT INTO search_index(kind, ref_id, title, body)
            SELECT 'transcription', r.audio_id, COALESCE(a.name, ''), r.text
            FROM transcription_results r LEFT JOIN audio_files a ON a.id=r.audio_id""")
        connection.execute("""INSERT INTO search_index(kind, ref_id, title, body)
            SELECT 'subtitle', s.import_id, COALESCE(p.name, ''), s.text
            FROM subtitle_results s LEFT JOIN podcast_imports p ON p.id=s.import_id""")
        connection.execute("""INSERT INTO search_index(kind, ref_id, title, body)
            SELECT 'manuscript', id, title, body FROM manuscripts WHERE status='completed'""")


def uses_fts(connection) -> bool:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name='search_index'").fetchone()
    return bool(row and "fts5" in row[0].lower())


def _terms(query) -> list[str]:
    if not isinstance(query, str):
        raise ValueError("请输入搜索内容。")
    query = query.strip()
    if not query:
        raise ValueError("请输入搜索内容。")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"搜索内容不能超过 {MAX_QUERY_LENGTH} 个字符。")
    return [term for term in re.split(r"\s+", query) if term][:8]


def _snippet(text: str, terms: list[str]) -> str:
    lowered = text.lower()
    positions = [lowered.find(term.lower()) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = min(positions) if positions else 0
    begin = max(0, start - SNIPPET_RADIUS)
    end = min(len(text), start + SNIPPET_RADIUS * 2)
    snippet = re.sub(r"\s+", " ", text[begin:end]).strip()
    return ("…" if begin else "") + snippet + ("…" if end < len(text) else "")


def search(database: Path, query, limit: int = 20) -> list[dict]:
    terms = _terms(query)
    limit = max(1, min(int(limit), 50))
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        if uses_fts(connection) and all(len(term) >= 3 for term in terms):
            expression = " ".join('"' + term.replace('"', '""') + '"' for term in terms)
            rows = connection.execute(
                """SELECT kind, ref_id, title, body FROM search_index
                   WHERE search_index MATCH ? ORDER BY rank LIMIT ?""", (expression, limit)).fetchall()
        else:
            # Trigram cannot match 1-2 character terms; fall back to substring scan.
            clauses = " AND ".join("instr(lower(title || ' ' || body), ?) > 0" for _ in terms)
            rows = connection.execute(
                f"SELECT kind, ref_id, title, body FROM search_index WHERE {clauses} LIMIT ?",
                (*(term.lower() for term in terms), limit)).fetchall()
    except sqlite3.OperationalError as error:
        raise ValueError("搜索暂时不可用，请稍后重试。") from error
    finally:
        connection.close()
    return [{"kind": row["kind"], "id": row["ref_id"], "title": row["title"],
             "snippet": _snippet(row["body"] or row["title"] or "", terms)} for row in rows]
