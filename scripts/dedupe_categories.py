"""
支出・収入カテゴリの重複データを整理するワンタイムスクリプト。

同じ user_id + category_name の行が複数ある場合、
最も id の小さい行を残し、他の行を削除する。
削除される側のカテゴリIDを参照している expenses / income の行は
残す側のIDへ付け替えてから削除する。

実行前に kakeibo.db のバックアップを自動作成する。

使い方:
    python scripts/dedupe_categories.py
"""
import os
import shutil
import sqlite3
import datetime

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kakeibo.db")


def backup_db():
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = f"{DB}.bak_{ts}"
    shutil.copy2(DB, backup_path)
    print(f"バックアップ作成: {backup_path}")


def dedupe(cur, table, ref_table, fk_col):
    cur.execute(f"SELECT id, user_id, category_name FROM {table} ORDER BY id")
    rows = cur.fetchall()
    seen = {}
    for row_id, user_id, category_name in rows:
        key = (user_id, category_name)
        if key not in seen:
            seen[key] = row_id
        else:
            keep_id = seen[key]
            dup_id = row_id
            cur.execute(f"UPDATE {ref_table} SET {fk_col} = ? WHERE {fk_col} = ?", (keep_id, dup_id))
            cur.execute(f"DELETE FROM {table} WHERE id = ?", (dup_id,))
            print(f"{table}: id={dup_id} を id={keep_id} に統合 (key={key})")


def main():
    if not os.path.exists(DB):
        print(f"DBが見つかりません: {DB}")
        return
    backup_db()
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    dedupe(cur, "categories", "expenses", "category_id")
    dedupe(cur, "income_categories", "income", "category_id")
    conn.commit()
    conn.close()
    print("完了")


if __name__ == "__main__":
    main()
