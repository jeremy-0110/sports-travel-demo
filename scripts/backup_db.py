"""Backup the SQLite database safely using SQLite's online backup API."""

from datetime import datetime
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import BACKUPS_DIR, DB_PATH


def backup_database(
    db_path: Path = DB_PATH,
    backups_dir: Path = BACKUPS_DIR,
) -> Path | None:
    """Create a timestamped backup of the SQLite database file."""
    if not db_path.exists():
        print(f"[backup] 資料庫檔案尚未建立 ({db_path})，略過備份。")
        return None

    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_path = backups_dir / f"sports_travel_{timestamp}.db"
    if target_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target_path = backups_dir / f"sports_travel_{timestamp}.db"

    src_conn = sqlite3.connect(str(db_path))
    dst_conn = sqlite3.connect(str(target_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    print(f"[backup] 資料庫已備份至: {target_path}")
    return target_path


if __name__ == "__main__":
    backup_database()
