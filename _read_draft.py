import sqlite3, json, sys, os

db_path = os.path.join(os.path.dirname(__file__), "qdrant_data", "checkpoint_drafts.db")
print(f"DB path: {db_path}")
print(f"DB exists: {os.path.exists(db_path)}")

try:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Check tables
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print(f"Tables: {[t[0] for t in tables]}")
    
    # Check columns
    cols = conn.execute("PRAGMA table_info(checkpoint_drafts)").fetchall()
    print(f"Columns: {[c['name'] for c in cols]}")
    
    # Count rows
    count = conn.execute("SELECT COUNT(*) FROM checkpoint_drafts").fetchone()[0]
    print(f"Total rows: {count}")
    
    # Search for draft
    rows = conn.execute("SELECT draft_id, version, status FROM checkpoint_drafts WHERE draft_id LIKE ?", ('4449%',)).fetchall()
    print(f"Found by pattern: {[dict(r) for r in rows]}")
    
    # Try task_id search
    rows2 = conn.execute("SELECT draft_id, version, status, preview FROM checkpoint_drafts WHERE task_id LIKE ? ORDER BY created_at DESC LIMIT 5", ('2f9ccc48%',)).fetchall()
    print(f"By task_id: {[dict(r) for r in rows2]}")
    for r in rows2:
        print(f"--- PREVIEW ---")
        print(r['preview'][:1000])
    
    conn.close()
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
