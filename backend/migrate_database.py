import argparse
import os
import sqlite3
from dotenv import load_dotenv

def migrate_db(db_path: str) -> bool:
    if not os.path.exists(db_path):
        print(f"Error: DB file not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='deposit_records'")
        if not cursor.fetchone():
            print("Table deposit_records does not exist; nothing to migrate")
            return True

        cursor.execute("PRAGMA table_info(deposit_records)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        if columns.get('amount_wei') == 'TEXT' and columns.get('power_added') == 'TEXT':
            print("Schema is already up to date; nothing to migrate")
            return True

        print("Starting migration...")

        conn.execute()

        conn.execute()

        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_user ON deposit_records_new(user_address)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_tx ON deposit_records_new(tx_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_block ON deposit_records_new(block_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_ts ON deposit_records_new(block_timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_records_referrer ON deposit_records_new(referrer_address)")

        conn.execute("DROP TABLE deposit_records")

        conn.execute("ALTER TABLE deposit_records_new RENAME TO deposit_records")

        conn.commit()
        print("Migration completed.")
        return True

    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        return False
    finally:
        conn.close()

def main():
    ap = argparse.ArgumentParser(description="Migrate database schema")
    ap.add_argument("--db", help="DB path (overrides PRICE_DB_PATH)")
    args = ap.parse_args()

    load_dotenv(override=True)

    db_path = args.db or os.getenv("PRICE_DB_PATH", "price_history.db").strip() or "price_history.db"

    print(f"DB path: {db_path}")

    if migrate_db(db_path):
        return 0
    else:
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
