#!/usr/bin/env python3
"""BusTrack admin tool - developer-only, run from a terminal in the project folder.

    python admin.py users                   list all accounts
    python admin.py user asha@example.com   one account's details
    python admin.py stats                   totals
    python admin.py export users.csv        save the list as CSV
    python admin.py set-password EMAIL      set someone a new password
    python admin.py delete EMAIL            delete an account

Design rule: this tool can SEE everything about a user except the password.
Passwords are stored only as salted PBKDF2 hashes (one-way), and this file never
selects the `salt` or `pw_hash` columns, so they are never printed or exported.
There is no web page or API route for any of this - it only works for someone who
can already open this folder.
"""
import csv
import getpass
import hashlib
import secrets
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "bustrack.db"

# Only these columns are ever read. Do NOT add salt / pw_hash here.
SAFE_COLS = "id, name, email, created_at, last_login"


def connect():
    if not DB_PATH.exists():
        sys.exit(f"No database at {DB_PATH}. Start the server once to create it.")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def when(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def cmd_users(_):
    with connect() as con:
        rows = con.execute(f"SELECT {SAFE_COLS} FROM users ORDER BY id").fetchall()
    if not rows:
        return print("No accounts yet.")
    print(f"{'ID':<4} {'Name':<22} {'Email':<32} {'Joined':<17} Last login")
    for r in rows:
        print(f"{r['id']:<4} {r['name'][:21]:<22} {r['email'][:31]:<32} "
              f"{when(r['created_at']):<17} {when(r['last_login'])}")
    print(f"\n{len(rows)} account(s).")


def cmd_user(args):
    if not args:
        sys.exit("Usage: python admin.py user EMAIL")
    with connect() as con:
        r = con.execute(f"SELECT {SAFE_COLS} FROM users WHERE email = ?",
                        (args[0].strip().lower(),)).fetchone()
        if not r:
            sys.exit("No account with that email.")
        sessions = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND expires_at > strftime('%s','now')",
            (r["id"],)).fetchone()[0]
    print(f"ID:          {r['id']}\nName:        {r['name']}\nEmail:       {r['email']}\n"
          f"Joined:      {when(r['created_at'])}\nLast login:  {when(r['last_login'])}\n"
          f"Active sessions: {sessions}\nPassword:    (not stored - hashed, cannot be viewed)")


def cmd_stats(_):
    with connect() as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        week = con.execute(
            "SELECT COUNT(*) FROM users WHERE last_login > strftime('%s','now') - 7*86400").fetchone()[0]
        new = con.execute(
            "SELECT COUNT(*) FROM users WHERE created_at > strftime('%s','now') - 7*86400").fetchone()[0]
    print(f"Total accounts:          {total}\nActive in last 7 days:   {week}\nJoined in last 7 days:   {new}")


def cmd_export(args):
    if not args:
        sys.exit("Usage: python admin.py export FILE.csv")
    with connect() as con:
        rows = con.execute(f"SELECT {SAFE_COLS} FROM users ORDER BY id").fetchall()
    with open(args[0], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "email", "joined", "last_login"])
        for r in rows:
            w.writerow([r["id"], r["name"], r["email"], when(r["created_at"]), when(r["last_login"])])
    print(f"Wrote {len(rows)} account(s) to {args[0]} (no passwords). Keep this file private.")


def cmd_set_password(args):
    if not args:
        sys.exit("Usage: python admin.py set-password EMAIL")
    email = args[0].strip().lower()
    pw = getpass.getpass("New password (8-128 chars): ")
    if not 8 <= len(pw) <= 128:
        sys.exit("Password must be 8 to 128 characters.")
    if getpass.getpass("Confirm: ") != pw:
        sys.exit("Passwords did not match.")
    salt = secrets.token_bytes(16)
    pw_hash = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)  # same as main.py
    with connect() as con:
        cur = con.execute("UPDATE users SET salt = ?, pw_hash = ? WHERE email = ?", (salt, pw_hash, email))
        if not cur.rowcount:
            sys.exit("No account with that email.")
        con.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE email = ?)", (email,))
    print("Password updated and the user was signed out everywhere.")


def cmd_delete(args):
    if not args:
        sys.exit("Usage: python admin.py delete EMAIL")
    email = args[0].strip().lower()
    if input(f"Permanently delete {email}? Type the email again to confirm: ").strip().lower() != email:
        sys.exit("Cancelled.")
    with connect() as con:
        row = con.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            sys.exit("No account with that email.")
        for t in ("sessions", "password_resets"):
            con.execute(f"DELETE FROM {t} WHERE user_id = ?", (row["id"],))
        con.execute("DELETE FROM users WHERE id = ?", (row["id"],))
    print("Account deleted.")


COMMANDS = {"users": cmd_users, "user": cmd_user, "stats": cmd_stats,
            "export": cmd_export, "set-password": cmd_set_password, "delete": cmd_delete}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](sys.argv[2:])
