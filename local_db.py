import os
import sqlite3
import pandas as pd
from pathlib import Path
from typing import Any, List, Dict, Optional
from services.normalization import clean_name, clean_email, clean_phone, clean_address

DB_PATH = Path(__file__).resolve().parent / "customer360.db"
BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "data" / "raw"
OUTPUT_DIR = BASE_DIR / "data" / "output"


def init_database():
    """Initializes and seeds SQLite database if needed."""
    need_seed = not DB_PATH.exists() or DB_PATH.stat().st_size == 0

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()

    # Tables
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name TEXT UNIQUE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reference_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        short_form TEXT UNIQUE,
        long_form TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS source_customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER,
        source_customer_id TEXT,
        name TEXT,
        email TEXT,
        phone TEXT,
        dob TEXT,
        address TEXT,
        normalized_name TEXT,
        normalized_email TEXT,
        normalized_phone TEXT,
        normalized_address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(source_id, source_customer_id)
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS golden_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        golden_id TEXT UNIQUE,
        name TEXT,
        email TEXT,
        phone TEXT,
        address TEXT,
        dob TEXT,
        name_source TEXT,
        email_source TEXT,
        phone_source TEXT,
        address_source TEXT,
        dob_source TEXT,
        representative_source TEXT,
        source TEXT,
        source_id TEXT,
        matched_source_ids TEXT,
        match_group_size INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS match_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_customer_id_1 INTEGER,
        source_customer_id_2 INTEGER,
        match_method TEXT,
        match_score REAL,
        name_score REAL,
        address_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS golden_record_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        golden_record_id INTEGER,
        source_customer_id INTEGER,
        match_method TEXT,
        match_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        txn_id TEXT UNIQUE,
        account_id TEXT,
        amount REAL,
        transaction_date TEXT,
        transaction_type TEXT,
        golden_id TEXT,
        source_system TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT,
        entity_id TEXT,
        field_name TEXT,
        old_value TEXT,
        new_value TEXT,
        changed_by TEXT,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()

    # Check if rows exist
    cursor.execute("SELECT COUNT(*) FROM golden_records")
    g_count = cursor.fetchone()[0]
    if g_count > 0:
        conn.close()
        return

    # Seed data
    print("Seeding local Customer 360 database from CSV data...")

    # 1. Sources
    cursor.executemany("INSERT OR IGNORE INTO sources (id, source_name) VALUES (?, ?)", [
        (1, "SourceA"),
        (2, "SourceB"),
        (3, "SourceC")
    ])

    # 2. Reference data
    ref_map = {}
    ref_csv = RAW_DIR / "reference_data.csv"
    if ref_csv.exists():
        ref_df = pd.read_csv(ref_csv, dtype=str).fillna("")
        for _, r in ref_df.iterrows():
            s = str(r["Short_Form"]).strip().lower()
            l = str(r["Long_Form"]).strip().lower()
            if s:
                ref_map[s] = l
                cursor.execute("INSERT OR REPLACE INTO reference_data (short_form, long_form) VALUES (?, ?)", (s, l))

    # 3. Source Customers
    files = [
        {"source": "SourceA", "source_id": 1, "file": "core_banking.csv", "id": "Customer_ID", "name": "Name", "email": "Email", "phone": "Phone", "dob": "DOB", "address": "Address"},
        {"source": "SourceB", "source_id": 2, "file": "credit_card.csv", "id": "Cust_Ref", "name": "Name", "email": "Email", "phone": "Phone", "dob": None, "address": "Address"},
        {"source": "SourceC", "source_id": 3, "file": "loan_system.csv", "id": "Loan_Cust_ID", "name": "Name", "email": None, "phone": "Phone", "dob": None, "address": "Address"}
    ]

    source_cust_map = {}
    for spec in files:
        fpath = RAW_DIR / spec["file"]
        if not fpath.exists():
            continue
        df = pd.read_csv(fpath, dtype=str).fillna("")
        for _, r in df.iterrows():
            sid = spec["source_id"]
            scid = str(r[spec["id"]]).strip()
            name = r[spec["name"]] if spec["name"] else ""
            email = r[spec["email"]] if spec["email"] else ""
            phone = r[spec["phone"]] if spec["phone"] else ""
            addr = r[spec["address"]] if spec["address"] else ""
            dob = r[spec["dob"]] if spec["dob"] else ""
            dob_val = None
            if dob.strip():
                p = pd.to_datetime(dob, errors="coerce")
                if pd.notna(p):
                    dob_val = p.strftime("%Y-%m-%d")

            cursor.execute("""
            INSERT OR REPLACE INTO source_customers 
            (source_id, source_customer_id, name, email, phone, dob, address, normalized_name, normalized_email, normalized_phone, normalized_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sid, scid, name or None, email or None, phone or None, dob_val, addr or None,
                clean_name(name) or None, clean_email(email) or None, clean_phone(phone) or None, clean_address(addr, ref_map) or None
            ))
            row_id = cursor.lastrowid
            source_cust_map[(sid, scid)] = row_id
            source_cust_map[scid] = row_id

    # 4. Golden Records
    golden_csv = OUTPUT_DIR / "golden_records.csv"
    golden_db_ids = {}
    if golden_csv.exists():
        golden_df = pd.read_csv(golden_csv, dtype=str).fillna("")
        for _, r in golden_df.iterrows():
            gid = r["GOLDEN_ID"].strip()
            rep_source = r.get("source", "") or "SourceA"
            group_size = int(r.get("match_group_size", 1))
            cursor.execute("""
            INSERT OR REPLACE INTO golden_records 
            (golden_id, name, email, phone, address, dob, name_source, email_source, phone_source, address_source, dob_source, representative_source, source, source_id, matched_source_ids, match_group_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                gid, r.get("name") or None, r.get("email") or None, r.get("phone") or None, r.get("address") or None, r.get("dob") or None,
                r.get("name_source") or None, r.get("email_source") or None, r.get("phone_source") or None, r.get("address_source") or None, r.get("dob_source") or None,
                rep_source, rep_source, r.get("source_id") or None, r.get("matched_source_ids") or None, group_size
            ))
            golden_db_ids[gid] = cursor.lastrowid

    # 5. Match Links
    match_csv = OUTPUT_DIR / "match_results.csv"
    match_lookup = {}
    if match_csv.exists():
        match_df = pd.read_csv(match_csv, dtype=str).fillna("")
        for _, r in match_df.iterrows():
            sa = r["source_a"].strip()
            sb = r["source_b"].strip()
            id_a = source_cust_map.get(sa)
            id_b = source_cust_map.get(sb)
            if id_a and id_b:
                first = min(id_a, id_b)
                second = max(id_a, id_b)
                score = float(r.get("score", 1.0)) if r.get("score") else 1.0
                n_score = float(r.get("name_score", 0)) if r.get("name_score") else None
                a_score = float(r.get("address_score", 0)) if r.get("address_score") else None
                method = r.get("method", "EXACT")
                match_lookup[(first, second)] = (method, score)
                cursor.execute("""
                INSERT INTO match_links (source_customer_id_1, source_customer_id_2, match_method, match_score, name_score, address_score)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (first, second, method, score, n_score, a_score))

    # 6. Golden Record Sources
    if golden_csv.exists():
        for gid, g_db_id in golden_db_ids.items():
            row_matches = golden_df[golden_df["GOLDEN_ID"] == gid]
            if row_matches.empty:
                continue
            row_golden = row_matches.iloc[0]
            matched_sids = [s.strip() for s in str(row_golden["matched_source_ids"]).split("|") if s.strip()]
            cust_ids = [source_cust_map[sid] for sid in matched_sids if sid in source_cust_map]
            for cid in cust_ids:
                mm = "PRIMARY"
                ms = 1.0
                for other_cid in cust_ids:
                    if cid != other_cid:
                        pair = (min(cid, other_cid), max(cid, other_cid))
                        if pair in match_lookup:
                            mm, ms = match_lookup[pair]
                            break
                cursor.execute("""
                INSERT INTO golden_record_sources (golden_record_id, source_customer_id, match_method, match_score)
                VALUES (?, ?, ?, ?)
                """, (g_db_id, cid, mm, ms))

    # 7. Transactions
    tx_csv = OUTPUT_DIR / "transactions_with_golden_id.csv"
    if tx_csv.exists():
        tx_df = pd.read_csv(tx_csv, dtype=str).fillna("")
        for _, r in tx_df.iterrows():
            amt = float(r["Amount"]) if r.get("Amount") else None
            cursor.execute("""
            INSERT OR REPLACE INTO transactions (txn_id, account_id, amount, transaction_date, transaction_type, golden_id, source_system)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                r.get("Txn_ID"), r.get("Account_ID"), amt, r.get("Date"), r.get("Type"), r.get("GOLDEN_ID"), r.get("source_system")
            ))

    conn.commit()
    conn.close()
    print("Database seeding completed.")


class Response:
    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class QueryBuilder:
    def __init__(self, db_path: Path, table_name: str):
        self.db_path = str(db_path)
        self.table_name = table_name
        self._select_cols = "*"
        self._count_mode = None
        self._head = False
        self._where = []
        self._params = []
        self._order = None
        self._limit = None
        self._offset = None
        self._action = "select"
        self._action_data = None
        self._on_conflict = None

    def select(self, columns: str = "*", count: Optional[str] = None, head: bool = False):
        self._select_cols = columns
        self._count_mode = count
        self._head = head
        return self

    def eq(self, column: str, value: Any):
        self._where.append(f'"{column}" = ?')
        self._params.append(value)
        return self

    def neq(self, column: str, value: Any):
        self._where.append(f'"{column}" != ?')
        self._params.append(value)
        return self

    def gt(self, column: str, value: Any):
        self._where.append(f'"{column}" > ?')
        self._params.append(value)
        return self

    def gte(self, column: str, value: Any):
        self._where.append(f'"{column}" >= ?')
        self._params.append(value)
        return self

    def lt(self, column: str, value: Any):
        self._where.append(f'"{column}" < ?')
        self._params.append(value)
        return self

    def lte(self, column: str, value: Any):
        self._where.append(f'"{column}" <= ?')
        self._params.append(value)
        return self

    def ilike(self, column: str, pattern: str):
        self._where.append(f'LOWER("{column}") LIKE LOWER(?)')
        self._params.append(pattern)
        return self

    def is_(self, column: str, value: Any):
        if value is None or str(value).lower() in ("null", "none"):
            self._where.append(f'"{column}" IS NULL')
        else:
            self._where.append(f'"{column}" IS NOT NULL')
        return self

    def in_(self, column: str, values: List[Any]):
        if not values:
            self._where.append("1 = 0")
        else:
            placeholders = ",".join(["?"] * len(values))
            self._where.append(f'"{column}" IN ({placeholders})')
            self._params.extend(values)
        return self

    def order(self, column: str, desc: bool = False):
        direction = "DESC" if desc else "ASC"
        self._order = f'"{column}" {direction}'
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def range(self, start: int, end: int):
        self._offset = start
        self._limit = max(0, end - start + 1)
        return self

    def insert(self, data: Any):
        self._action = "insert"
        self._action_data = data
        return self

    def update(self, data: Dict[str, Any]):
        self._action = "update"
        self._action_data = data
        return self

    def upsert(self, data: Any, on_conflict: Optional[str] = None):
        self._action = "upsert"
        self._action_data = data
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._action = "delete"
        return self

    def execute(self) -> Response:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        where_sql = (" WHERE " + " AND ".join(self._where)) if self._where else ""

        if self._action == "select":
            count = None
            if self._count_mode:
                c_sql = f'SELECT COUNT(*) FROM "{self.table_name}"{where_sql}'
                cursor.execute(c_sql, self._params)
                count = cursor.fetchone()[0]

            if self._head:
                conn.close()
                return Response(data=[], count=count)

            cols = self._select_cols
            if cols != "*":
                cols_split = [c.strip() for c in cols.split(",") if c.strip()]
                clean_cols = ", ".join([f'"{c}"' for c in cols_split])
            else:
                clean_cols = "*"

            sql = f'SELECT {clean_cols} FROM "{self.table_name}"{where_sql}'
            if self._order:
                sql += f' ORDER BY {self._order}'
            if self._limit is not None:
                sql += f' LIMIT {self._limit}'
            if self._offset is not None:
                sql += f' OFFSET {self._offset}'

            cursor.execute(sql, self._params)
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return Response(data=rows, count=count if count is not None else len(rows))

        elif self._action == "insert":
            items = self._action_data if isinstance(self._action_data, list) else [self._action_data]
            inserted = []
            for item in items:
                keys = list(item.keys())
                col_names = ", ".join([f'"{k}"' for k in keys])
                placeholders = ", ".join(["?"] * len(keys))
                vals = [item[k] for k in keys]
                cursor.execute(f'INSERT INTO "{self.table_name}" ({col_names}) VALUES ({placeholders})', vals)
                new_id = cursor.lastrowid
                row_copy = dict(item)
                row_copy["id"] = new_id
                inserted.append(row_copy)
            conn.commit()
            conn.close()
            return Response(data=inserted)

        elif self._action == "upsert":
            items = self._action_data if isinstance(self._action_data, list) else [self._action_data]
            upserted = []
            for item in items:
                keys = list(item.keys())
                col_names = ", ".join([f'"{k}"' for k in keys])
                placeholders = ", ".join(["?"] * len(keys))
                vals = [item[k] for k in keys]
                cursor.execute(f'INSERT OR REPLACE INTO "{self.table_name}" ({col_names}) VALUES ({placeholders})', vals)
                new_id = cursor.lastrowid
                row_copy = dict(item)
                if "id" not in row_copy or not row_copy["id"]:
                    row_copy["id"] = new_id
                upserted.append(row_copy)
            conn.commit()
            conn.close()
            return Response(data=upserted)

        elif self._action == "update":
            data = self._action_data or {}
            cursor.execute(f'SELECT id FROM "{self.table_name}"{where_sql}', self._params)
            matched_ids = [r[0] for r in cursor.fetchall()]

            set_clauses = [f'"{k}" = ?' for k in data.keys()]
            set_sql = ", ".join(set_clauses)
            update_sql = f'UPDATE "{self.table_name}" SET {set_sql}{where_sql}'
            cursor.execute(update_sql, list(data.values()) + self._params)
            conn.commit()

            updated_rows = []
            if matched_ids:
                q_marks = ",".join(["?"] * len(matched_ids))
                cursor.execute(f'SELECT * FROM "{self.table_name}" WHERE id IN ({q_marks})', matched_ids)
                updated_rows = [dict(r) for r in cursor.fetchall()]

            conn.close()
            return Response(data=updated_rows)

        elif self._action == "delete":
            cursor.execute(f'SELECT id FROM "{self.table_name}"{where_sql}', self._params)
            matched_ids = [r[0] for r in cursor.fetchall()]
            cursor.execute(f'DELETE FROM "{self.table_name}"{where_sql}', self._params)
            conn.commit()
            conn.close()
            return Response(data=[{"id": i} for i in matched_ids])

        conn.close()
        return Response(data=[])


class LocalSupabaseClient:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        init_database()

    def table(self, table_name: str) -> QueryBuilder:
        return QueryBuilder(self.db_path, table_name)


# Singleton local client
local_client = LocalSupabaseClient()
