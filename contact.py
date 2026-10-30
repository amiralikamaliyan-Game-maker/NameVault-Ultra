# -*- coding: utf-8 -*-
"""
NAMEVAULT ULTRA v2.1
Advanced English-only terminal contact manager
Python 3.10+ | Standard library only
"""

import argparse
import csv
import difflib
import json
import sqlite3
import sys
import tempfile
import unittest

from pathlib import Path
from datetime import datetime, timezone

APP = "NAMEVAULT ULTRA"
VERSION = "2.1.0"

ROOT = Path(__file__).resolve().parent
DB = ROOT / "names.db"
BACKUPS = ROOT / "backups"
EXPORTS = ROOT / "exports"
LOG = ROOT / "namevault.log"


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def normalize(text):
    return " ".join(str(text).casefold().split())


def validate_name(name):
    if not isinstance(name, str):
        raise ValueError("Name must be text.")

    name = " ".join(name.split())

    if not name:
        raise ValueError("Name cannot be empty.")

    if name.casefold() in {"done", "exit", "quit"}:
        raise ValueError("That word is a reserved command.")

    if len(name) > 120:
        raise ValueError("Maximum name length is 120 characters.")

    if any(ord(ch) < 32 for ch in name):
        raise ValueError("Control characters are not allowed.")

    return name


class Vault:
    def __init__(self, path=DB):
        self.path = str(path)

        if self.path != ":memory:":
            Path(self.path).parent.mkdir(
                parents=True, exist_ok=True
            )

        self.setup()

    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")

        if self.path != ":memory:":
            con.execute("PRAGMA journal_mode=WAL")

        return con

    def setup(self):
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS contacts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'General',
                    tags TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    contact_id INTEGER,
                    before_data TEXT,
                    after_data TEXT,
                    created_at TEXT NOT NULL,
                    undone INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_contacts_deleted
                ON contacts(deleted_at);

                CREATE INDEX IF NOT EXISTS idx_contacts_name
                ON contacts(name);

                CREATE INDEX IF NOT EXISTS idx_events_state
                ON events(undone, id);
            """)

    def record(self, con, action, cid, before, after):
        con.execute("""
            INSERT INTO events(
                action, contact_id, before_data,
                after_data, created_at, undone
            ) VALUES (?, ?, ?, ?, ?, 0)
        """, (
            action,
            cid,
            json.dumps(before, ensure_ascii=False) if before else None,
            json.dumps(after, ensure_ascii=False) if after else None,
            now()
        ))

        # Discard the redo branch after a new action.
        con.execute("""
            DELETE FROM events
            WHERE undone=1
        """)

    def add(self, name, category="General", tags="", note=""):
        name = validate_name(name)
        category = (category.strip() or "General")[:80]

        tags = ", ".join(dict.fromkeys(
            tag.strip()
            for tag in tags.split(",")
            if tag.strip()
        ))[:500]

        note = note.strip()[:2000]
        stamp = now()

        with self.connect() as con:
            existing = con.execute("""
                SELECT name FROM contacts
                WHERE deleted_at IS NULL
            """).fetchall()

            if any(
                normalize(row["name"]) == normalize(name)
                for row in existing
            ):
                raise ValueError("This name already exists.")

            cursor = con.execute("""
                INSERT INTO contacts(
                    name, category, tags, note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                name, category, tags, note, stamp, stamp
            ))

            cid = cursor.lastrowid

            after = dict(con.execute(
                "SELECT * FROM contacts WHERE id=?", (cid,)
            ).fetchone())

            self.record(con, "add", cid, None, after)

            return cid

    def get(self, cid, include_deleted=False):
        sql = "SELECT * FROM contacts WHERE id=?"

        if not include_deleted:
            sql += " AND deleted_at IS NULL"

        with self.connect() as con:
            row = con.execute(sql, (cid,)).fetchone()

        return dict(row) if row else None

    def list(self, sort="name", category=None, deleted=False):
        orders = {
            "id": "id ASC",
            "name": "name COLLATE NOCASE ASC, id ASC",
            "newest": "created_at DESC, id DESC",
            "oldest": "created_at ASC, id ASC",
            "updated": "updated_at DESC, id DESC",
            "category": (
                "category COLLATE NOCASE, name COLLATE NOCASE"
            )
        }

        if sort not in orders:
            raise ValueError("Invalid sorting option.")

        clauses = [
            "deleted_at IS NOT NULL"
            if deleted else "deleted_at IS NULL"
        ]

        params = []

        if category:
            clauses.append("category=? COLLATE NOCASE")
            params.append(category)

        sql = (
            "SELECT * FROM contacts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY "
            + orders[sort]
        )

        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()

        return [dict(row) for row in rows]

    def search(self, query, fuzzy=False):
        query = normalize(query)

        if not query:
            return []

        items = self.list()

        matches = [
            item for item in items
            if query in normalize(item["name"])
            or query in normalize(item["category"])
            or query in normalize(item["tags"])
            or query in normalize(item["note"])
        ]

        if fuzzy and not matches:
            scored = []

            for item in items:
                score = difflib.SequenceMatcher(
                    None,
                    query,
                    normalize(item["name"])
                ).ratio()

                if score >= 0.45:
                    scored.append((score, item))

            matches = [
                item for score, item in sorted(
                    scored,
                    key=lambda pair: pair[0],
                    reverse=True
                )
            ]

        return matches[:500]

    def update(
        self, cid, name=None, category=None, tags=None, note=None
    ):
        with self.connect() as con:
            row = con.execute("""
                SELECT * FROM contacts
                WHERE id=? AND deleted_at IS NULL
            """, (cid,)).fetchone()

            if not row:
                return False

            before = dict(row)

            new_name = (
                validate_name(name)
                if name is not None and name.strip()
                else before["name"]
            )

            new_category = (
                (category.strip() or "General")[:80]
                if category is not None
                else before["category"]
            )

            new_tags = (
                ", ".join(dict.fromkeys(
                    tag.strip()
                    for tag in tags.split(",")
                    if tag.strip()
                ))[:500]
                if tags is not None
                else before["tags"]
            )

            new_note = (
                note.strip()[:2000]
                if note is not None
                else before["note"]
            )

            other_names = con.execute("""
                SELECT name FROM contacts
                WHERE deleted_at IS NULL AND id != ?
            """, (cid,)).fetchall()

            if any(
                normalize(item["name"]) == normalize(new_name)
                for item in other_names
            ):
                raise ValueError(
                    "Another contact already uses that name."
                )

            con.execute("""
                UPDATE contacts
                SET name=?, category=?, tags=?, note=?, updated_at=?
                WHERE id=?
            """, (
                new_name,
                new_category,
                new_tags,
                new_note,
                now(),
                cid
            ))

            after = dict(con.execute(
                "SELECT * FROM contacts WHERE id=?", (cid,)
            ).fetchone())

            self.record(con, "update", cid, before, after)

            return True

    def delete(self, cid):
        with self.connect() as con:
            row = con.execute("""
                SELECT * FROM contacts
                WHERE id=? AND deleted_at IS NULL
            """, (cid,)).fetchone()

            if not row:
                return False

            before = dict(row)
            stamp = now()

            con.execute("""
                UPDATE contacts
                SET deleted_at=?, updated_at=?
                WHERE id=?
            """, (stamp, stamp, cid))

            after = dict(con.execute(
                "SELECT * FROM contacts WHERE id=?", (cid,)
            ).fetchone())

            self.record(con, "delete", cid, before, after)

            return True

    def restore(self, cid):
        with self.connect() as con:
            row = con.execute("""
                SELECT * FROM contacts
                WHERE id=? AND deleted_at IS NOT NULL
            """, (cid,)).fetchone()

            if not row:
                return False

            before = dict(row)

            active = con.execute("""
                SELECT name FROM contacts
                WHERE deleted_at IS NULL
            """).fetchall()

            if any(
                normalize(item["name"]) == normalize(row["name"])
                for item in active
            ):
                raise ValueError(
                    "An active contact already has that name."
                )

            con.execute("""
                UPDATE contacts
                SET deleted_at=NULL, updated_at=?
                WHERE id=?
            """, (now(), cid))

            after = dict(con.execute(
                "SELECT * FROM contacts WHERE id=?", (cid,)
            ).fetchone())

            self.record(con, "restore", cid, before, after)

            return True

    def purge(self, cid):
        """Permanently remove a contact from the recycle bin."""
        with self.connect() as con:
            row = con.execute("""
                SELECT id FROM contacts
                WHERE id=? AND deleted_at IS NOT NULL
            """, (cid,)).fetchone()

            if not row:
                return False

            con.execute(
                "DELETE FROM contacts WHERE id=?", (cid,)
            )

            con.execute(
                "DELETE FROM events WHERE contact_id=?", (cid,)
            )

            return True

    def snapshot(self, con, data):
        con.execute("""
            INSERT INTO contacts(
                id, name, category, tags, note,
                created_at, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                category=excluded.category,
                tags=excluded.tags,
                note=excluded.note,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                deleted_at=excluded.deleted_at
        """, (
            data["id"],
            data["name"],
            data["category"],
            data["tags"],
            data["note"],
            data["created_at"],
            data["updated_at"],
            data["deleted_at"]
        ))

    def undo(self):
        with self.connect() as con:
            row = con.execute("""
                SELECT * FROM events
                WHERE undone=0
                ORDER BY id DESC LIMIT 1
            """).fetchone()

            if not row:
                return None

            before = (
                json.loads(row["before_data"])
                if row["before_data"] else None
            )

            after = (
                json.loads(row["after_data"])
                if row["after_data"] else None
            )

            if row["action"] == "add":
                con.execute(
                    "DELETE FROM contacts WHERE id=?",
                    (row["contact_id"],)
                )
            elif before:
                self.snapshot(con, before)

            con.execute(
                "UPDATE events SET undone=1 WHERE id=?",
                (row["id"],)
            )

            record = before or after or {}

            return f"Undo complete: {record.get('name', 'record')}"

    def redo(self):
        with self.connect() as con:
            row = con.execute("""
                SELECT * FROM events
                WHERE undone=1
                ORDER BY id ASC LIMIT 1
            """).fetchone()

            if not row:
                return None

            before = (
                json.loads(row["before_data"])
                if row["before_data"] else None
            )

            after = (
                json.loads(row["after_data"])
                if row["after_data"] else None
            )

            if after:
                self.snapshot(con, after)

            con.execute(
                "UPDATE events SET undone=0 WHERE id=?",
                (row["id"],)
            )

            record = before or after or {}

            return f"Redo complete: {record.get('name', 'record')}"

    def history(self, limit=50):
        with self.connect() as con:
            rows = con.execute("""
                SELECT * FROM events
                ORDER BY id DESC LIMIT ?
            """, (max(1, min(limit, 500)),)).fetchall()

        return [dict(row) for row in rows]

    def stats(self):
        with self.connect() as con:
            active = con.execute("""
                SELECT COUNT(*) FROM contacts
                WHERE deleted_at IS NULL
            """).fetchone()[0]

            deleted = con.execute("""
                SELECT COUNT(*) FROM contacts
                WHERE deleted_at IS NOT NULL
            """).fetchone()[0]

            events = con.execute(
                "SELECT COUNT(*) FROM events"
            ).fetchone()[0]

            categories = con.execute("""
                SELECT category, COUNT(*) AS count
                FROM contacts
                WHERE deleted_at IS NULL
                GROUP BY category
                ORDER BY count DESC
            """).fetchall()

        return {
            "active": active,
            "deleted": deleted,
            "events": events,
            "categories": [dict(row) for row in categories]
        }

    def export(self, fmt):
        if fmt not in {"csv", "json", "txt"}:
            raise ValueError("Choose csv, json, or txt.")

        EXPORTS.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = EXPORTS / f"names_{stamp}.{fmt}"
        items = self.list()

        if fmt == "json":
            path.write_text(
                json.dumps(items, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

        elif fmt == "csv":
            with path.open(
                "w", newline="", encoding="utf-8-sig"
            ) as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=[
                        "id", "name", "category", "tags", "note",
                        "created_at", "updated_at", "deleted_at"
                    ]
                )
                writer.writeheader()
                writer.writerows(items)

        else:
            lines = [APP, "=" * 50]

            for item in items:
                lines.append(
                    f"{item['id']}. {item['name']} | "
                    f"{item['category']} | {item['tags']}"
                )

                if item["note"]:
                    lines.append(f"   Note: {item['note']}")

            path.write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8"
            )

        return path

    def import_file(self, filename):
        path = Path(filename).expanduser()

        if not path.is_file():
            raise FileNotFoundError(path)

        if path.suffix.lower() == ".json":
            records = json.loads(
                path.read_text(encoding="utf-8-sig")
            )

            if not isinstance(records, list):
                raise ValueError("JSON must contain a list.")

        elif path.suffix.lower() == ".csv":
            with path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as file:
                records = list(csv.DictReader(file))

        else:
            raise ValueError(
                "Only CSV and JSON files are supported."
            )

        added = 0
        skipped = 0

        for record in records:
            if not isinstance(record, dict) or not record.get("name"):
                skipped += 1
                continue

            try:
                self.add(
                    str(record["name"]),
                    str(record.get("category") or "General"),
                    str(record.get("tags") or ""),
                    str(record.get("note") or "")
                )
                added += 1

            except ValueError:
                skipped += 1

        return added, skipped

    def backup(self):
        if self.path == ":memory:":
            raise ValueError(
                "In-memory databases cannot be backed up."
            )

        BACKUPS.mkdir(parents=True, exist_ok=True)

        path = BACKUPS / (
            f"backup_{datetime.now():%Y%m%d_%H%M%S_%f}.db"
        )

        with self.connect() as source:
            with sqlite3.connect(path) as target:
                source.backup(target)

        return path

    def restore_backup(self, filename):
        source_path = Path(filename).expanduser()

        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        if Path(self.path).resolve() == source_path.resolve():
            raise ValueError(
                "Backup path must differ from the live database."
            )

        with sqlite3.connect(source_path) as source:
            check = source.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]

            if check != "ok":
                raise ValueError("Backup database is damaged.")

        self.backup()

        with sqlite3.connect(source_path) as source:
            with self.connect() as target:
                source.backup(target)

    def check(self):
        with self.connect() as con:
            return con.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]


def show(items):
    if not items:
        print("No records found.")
        return

    print(
        f"\n{'ID':<5} {'NAME':<24} {'CATEGORY':<16} TAGS"
    )
    print("-" * 75)

    for item in items:
        print(
            f"{item['id']:<5} "
            f"{item['name'][:23]:<24} "
            f"{item['category'][:15]:<16} "
            f"{item['tags'][:25]}"
        )

        if item["note"]:
            print(f"      Note: {item['note'][:100]}")


def show_stats(vault):
    data = vault.stats()

    print("\nSTATISTICS")
    print("-" * 35)
    print("Active contacts:", data["active"])
    print("Recycle bin:", data["deleted"])
    print("Recorded events:", data["events"])
    print("Categories:")

    for item in data["categories"]:
        print(f"  {item['category']}: {item['count']}")


def run_tests():
    class VaultTests(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory()
            self.vault = Vault(
                Path(self.temp.name) / "test.db"
            )

        def tearDown(self):
            self.temp.cleanup()

        def test_add(self):
            self.vault.add("Alice")
            self.assertEqual(
                self.vault.list()[0]["name"], "Alice"
            )

        def test_duplicate(self):
            self.vault.add("Alice")
            with self.assertRaises(ValueError):
                self.vault.add("ALICE")

        def test_reserved_name(self):
            with self.assertRaises(ValueError):
                self.vault.add("done")

        def test_search(self):
            self.vault.add("Sarah Johnson")
            self.assertEqual(
                len(self.vault.search("SARAH")), 1
            )

        def test_update_undo_redo(self):
            cid = self.vault.add("Mina")
            self.vault.update(cid, name="Mina Noor")

            self.assertEqual(
                self.vault.get(cid)["name"], "Mina Noor"
            )

            self.vault.undo()

            self.assertEqual(
                self.vault.get(cid)["name"], "Mina"
            )

            self.vault.redo()

            self.assertEqual(
                self.vault.get(cid)["name"], "Mina Noor"
            )

        def test_delete_undo(self):
            cid = self.vault.add("Robert")
            self.vault.delete(cid)

            self.assertIsNone(self.vault.get(cid))

            self.vault.undo()

            self.assertIsNotNone(self.vault.get(cid))

        def test_restore(self):
            cid = self.vault.add("Nora")
            self.vault.delete(cid)

            self.assertTrue(self.vault.restore(cid))
            self.assertIsNotNone(self.vault.get(cid))

        def test_integrity(self):
            self.assertEqual(self.vault.check(), "ok")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        VaultTests
    )

    result = unittest.TextTestRunner(verbosity=2).run(suite)

    return result.wasSuccessful()


def interactive():
    vault = Vault()

    while True:
        print("\n" + "=" * 60)
        print(f"  {APP} v{VERSION}")
        print("  Advanced Terminal Contact Manager")
        print("=" * 60)
        print(" 1. Add Contact")
        print(" 2. List Contacts")
        print(" 3. Search Contacts")
        print(" 4. Fuzzy Search")
        print(" 5. Edit Contact")
        print(" 6. Move Contact to Recycle Bin")
        print(" 7. View Recycle Bin")
        print(" 8. Restore Contact")
        print(" 9. Sort and Filter")
        print("10. Statistics")
        print("11. Export Data")
        print("12. Import Data")
        print("13. Undo")
        print("14. Redo")
        print("15. View History")
        print("16. Create Backup")
        print("17. Restore Backup")
        print("18. Check Database Integrity")
        print("19. Run Automated Tests")
        print("20. Greet All Contacts")
        print("21. Permanently Delete Contact")
        print(" 0. Exit")
        print("-" * 60)

        choice = input("Choose an option: ").strip()

        try:
            if choice == "1":
                name = input("Name: ")
                category = input(
                    "Category (General): "
                ).strip() or "General"
                tags = input("Tags (comma-separated): ")
                note = input("Note: ")

                cid = vault.add(
                    name, category, tags, note
                )

                print("Contact added successfully. ID:", cid)

            elif choice == "2":
                show(vault.list(sort="id"))

            elif choice in ("3", "4"):
                query = input("Search query: ")

                show(vault.search(
                    query,
                    fuzzy=(choice == "4")
                ))

            elif choice == "5":
                raw = input("Contact ID: ").strip()

                if not raw.isdigit():
                    print("Invalid contact ID.")
                else:
                    item = vault.get(int(raw))

                    if not item:
                        print("Contact not found.")
                    else:
                        print("Leave a field blank to keep its value.")

                        name = input(
                            f"Name [{item['name']}]: "
                        )
                        category = input(
                            f"Category [{item['category']}]: "
                        )
                        tags = input(
                            f"Tags [{item['tags']}]: "
                        )
                        note = input(
                            f"Note [{item['note']}]: "
                        )

                        changed = vault.update(
                            int(raw),
                            name or None,
                            category or None,
                            tags if tags != "" else None,
                            note if note != "" else None
                        )

                        print(
                            "Contact updated."
                            if changed else "Contact not found."
                        )

            elif choice == "6":
                show(vault.list(sort="id"))
                raw = input("Contact ID to delete: ").strip()

                if raw.isdigit() and vault.delete(int(raw)):
                    print("Contact moved to recycle bin.")
                else:
                    print("Invalid ID or contact not found.")

            elif choice == "7":
                show(vault.list(deleted=True))

            elif choice == "8":
                show(vault.list(deleted=True))
                raw = input("Contact ID to restore: ").strip()

                if raw.isdigit() and vault.restore(int(raw)):
                    print("Contact restored successfully.")
                else:
                    print("Contact not found.")

            elif choice == "9":
                print(
                    "1 = Name | 2 = Newest | 3 = Oldest | "
                    "4 = Recently Updated | 5 = Category"
                )

                order = {
                    "1": "name",
                    "2": "newest",
                    "3": "oldest",
                    "4": "updated",
                    "5": "category"
                }.get(input("Sort option: ").strip())

                if order:
                    category = input(
                        "Category filter (blank for all): "
                    ).strip() or None

                    show(vault.list(
                        sort=order,
                        category=category
                    ))
                else:
                    print("Invalid sorting option.")

            elif choice == "10":
                show_stats(vault)

            elif choice == "11":
                fmt = input(
                    "Export format (csv/json/txt): "
                ).strip().lower()

                print("Export created:", vault.export(fmt))

            elif choice == "12":
                path = input(
                    "CSV or JSON file path: "
                ).strip().strip('"')

                added, skipped = vault.import_file(path)

                print("Imported:", added)
                print("Skipped:", skipped)

            elif choice == "13":
                print(vault.undo() or "Nothing to undo.")

            elif choice == "14":
                print(vault.redo() or "Nothing to redo.")

            elif choice == "15":
                for event in vault.history():
                    print(
                        f"#{event['id']} "
                        f"{event['created_at']} | "
                        f"{event['action']} | "
                        f"Contact ID={event['contact_id']} | "
                        f"Undone={bool(event['undone'])}"
                    )

            elif choice == "16":
                print("Backup created:", vault.backup())

            elif choice == "17":
                path = input("Backup file path: ").strip().strip('"')

                confirm = input(
                    "This replaces current data. Type RESTORE: "
                ).strip()

                if confirm == "RESTORE":
                    vault.restore_backup(path)
                    print(
                        "Database restored. A safety backup was created."
                    )
                else:
                    print("Operation cancelled.")

            elif choice == "18":
                print("Database integrity:", vault.check())

            elif choice == "19":
                passed = run_tests()
                print(
                    "All tests passed."
                    if passed else "Some tests failed."
                )

            elif choice == "20":
                records = vault.list()

                if not records:
                    print("No contacts to greet.")

                for item in records:
                    print(f"Hello, {item['name']}!")

            elif choice == "21":
                show(vault.list(deleted=True))

                raw = input(
                    "Contact ID to permanently delete: "
                ).strip()

                if raw.isdigit():
                    confirm = input(
                        "This cannot be undone. Type DELETE: "
                    ).strip()

                    if confirm == "DELETE":
                        if vault.purge(int(raw)):
                            print("Contact permanently deleted.")
                        else:
                            print("Contact not found.")
                    else:
                        print("Operation cancelled.")
                else:
                    print("Invalid contact ID.")

            elif choice == "0":
                print("Goodbye!")
                break

            else:
                print("Invalid option. Please try again.")

        except (
            ValueError,
            OSError,
            sqlite3.Error,
            json.JSONDecodeError
        ) as exc:
            print(f"Error: {exc}")

            try:
                with LOG.open("a", encoding="utf-8") as file:
                    file.write(
                        f"[{now()}] "
                        f"{type(exc).__name__}: {exc}\n"
                    )
            except OSError:
                pass

        input("\nPress Enter to continue...")


def cli():
    parser = argparse.ArgumentParser(
        description="NAMEVAULT ULTRA Contact Manager"
    )

    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="Add a contact")
    add.add_argument("name")
    add.add_argument("--category", default="General")
    add.add_argument("--tags", default="")
    add.add_argument("--note", default="")

    search = sub.add_parser("search", help="Search contacts")
    search.add_argument("query")
    search.add_argument("--fuzzy", action="store_true")

    listing = sub.add_parser("list", help="List contacts")
    listing.add_argument(
        "--sort",
        choices=[
            "id", "name", "newest",
            "oldest", "updated", "category"
        ],
        default="name"
    )

    delete = sub.add_parser(
        "delete", help="Move a contact to the recycle bin"
    )
    delete.add_argument("id", type=int)

    export = sub.add_parser("export", help="Export contacts")
    export.add_argument(
        "format", choices=["csv", "json", "txt"]
    )

    restore = sub.add_parser(
        "restore-backup", help="Restore a database backup"
    )
    restore.add_argument("path")

    sub.add_parser("stats")
    sub.add_parser("undo")
    sub.add_parser("redo")
    sub.add_parser("backup")
    sub.add_parser("check")
    sub.add_parser("test")

    args = parser.parse_args()

    if not args.command:
        interactive()
        return 0

    try:
        vault = Vault()

        if args.command == "add":
            cid = vault.add(
                args.name,
                args.category,
                args.tags,
                args.note
            )
            print("Contact added. ID:", cid)

        elif args.command == "search":
            show(vault.search(args.query, args.fuzzy))

        elif args.command == "list":
            show(vault.list(sort=args.sort))

        elif args.command == "delete":
            print(
                "Contact moved to recycle bin."
                if vault.delete(args.id)
                else "Contact not found."
            )

        elif args.command == "stats":
            show_stats(vault)

        elif args.command == "export":
            print("Export created:", vault.export(args.format))

        elif args.command == "undo":
            print(vault.undo() or "Nothing to undo.")

        elif args.command == "redo":
            print(vault.redo() or "Nothing to redo.")

        elif args.command == "backup":
            print("Backup created:", vault.backup())

        elif args.command == "restore-backup":
            confirm = input(
                "This replaces current data. Type RESTORE: "
            ).strip()

            if confirm != "RESTORE":
                print("Operation cancelled.")
                return 1

            vault.restore_backup(args.path)
            print("Database restored successfully.")

        elif args.command == "check":
            result = vault.check()
            print(result)
            return 0 if result == "ok" else 2

        elif args.command == "test":
            return 0 if run_tests() else 1

        return 0

    except (
        ValueError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError
    ) as exc:
        print("Error:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
