"""Database module for storing analyses in SQLite."""
import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.environ.get("ANALYSIS_DB", "/app/analyses.db")
DB_DIR = os.path.dirname(DB_PATH)


def get_db():
    """Get database connection."""
    if DB_DIR and not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Initialize database tables."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            game_url TEXT NOT NULL,
            game_id TEXT NOT NULL,
            white TEXT,
            black TEXT,
            result TEXT,
            white_elo TEXT,
            black_elo TEXT,
            eco TEXT,
            pgn TEXT,
            analysis TEXT,
            evaluations_json TEXT,
            moves_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON analyses(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON analyses(created_at DESC)")
    conn.commit()
    conn.close()


def save_analysis(session_id, game_url, game_id, metadata, pgn, analysis_text, evaluations, moves):
    """Save an analysis to the database."""
    conn = get_db()
    conn.execute("""
        INSERT INTO analyses (session_id, game_url, game_id, white, black, result,
                              white_elo, black_elo, eco, pgn, analysis, evaluations_json, moves_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id,
        game_url,
        game_id,
        metadata.get("white", "?"),
        metadata.get("black", "?"),
        metadata.get("result", "?"),
        metadata.get("white_elo", "?"),
        metadata.get("black_elo", "?"),
        metadata.get("eco", "?"),
        pgn,
        analysis_text,
        json.dumps(evaluations),
        json.dumps(moves),
    ))
    conn.commit()
    analysis_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return analysis_id


def get_session_analyses(session_id, limit=50):
    """Get all analyses for a session, newest first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM analyses WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit)
    ).fetchall()
    results = []
    for row in rows:
        results.append(dict(row))
    conn.close()
    return results


def get_analysis(analysis_id, session_id):
    """Get a single analysis by ID (ownership check)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM analyses WHERE id = ? AND session_id = ?",
        (analysis_id, session_id)
    ).fetchone()
    result = dict(row) if row else None
    if result and result.get("evaluations_json"):
        result["evaluations"] = json.loads(result["evaluations_json"])
    if result and result.get("moves_json"):
        result["moves"] = json.loads(result["moves_json"])
    conn.close()
    return result


def delete_analysis(analysis_id, session_id):
    """Delete an analysis by ID (ownership check)."""
    conn = get_db()
    conn.execute("DELETE FROM analyses WHERE id = ? AND session_id = ?", (analysis_id, session_id))
    conn.commit()
    conn.close()


# Initialize on import
init_db()
