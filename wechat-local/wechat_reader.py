"""Read locally stored Windows WeChat 4 conversations."""

import argparse
import ctypes as ct
from ctypes import wintypes as wt
from contextlib import closing
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 4096
RESERVE = 80
SQLITE = b"SQLite format 3\0"
KEY_TOKEN = re.compile(rb"x'([0-9a-f]{64})(?:[0-9a-f]{32})?'", re.I)


class Region(ct.Structure):
    _fields_ = [
        ("base", ct.c_void_p), ("allocation", ct.c_void_p),
        ("allocation_protect", wt.DWORD), ("size", ct.c_size_t),
        ("state", wt.DWORD), ("protect", wt.DWORD), ("type", wt.DWORD),
    ]


def first_page_matches(page, key):
    if len(page) != PAGE:
        return False
    salt = bytes(value ^ 0x3A for value in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", key, salt, 2, 32)
    digest = hmac.digest(mac_key, page[16:-64] + (1).to_bytes(4, "little"), "sha512")
    return hmac.compare_digest(digest, page[-64:])


def process_keys(pages, diagnostics=None):
    command = "Get-Process -Name Weixin -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    pids = [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
    if not pids:
        raise ValueError("Open the signed-in Windows WeChat app to initialize local reading.")
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = (wt.HANDLE,)
    kernel.VirtualQueryEx.argtypes = (wt.HANDLE, ct.c_void_p, ct.POINTER(Region), ct.c_size_t)
    kernel.VirtualQueryEx.restype = ct.c_size_t
    kernel.ReadProcessMemory.argtypes = (
        wt.HANDLE, ct.c_void_p, ct.c_void_p, ct.c_size_t, ct.POINTER(ct.c_size_t),
    )
    keys = {}
    checked = set()
    opened = 0
    read_bytes = 0
    if diagnostics is not None:
        diagnostics.update(processes=len(pids), opened=0, read_bytes=0)
    salts = [
        (salt, salt.hex().encode(encoding), encoding, 64 if encoding == "ascii" else 128)
        for salt in pages
        for encoding in ("ascii", "utf-16le")
    ]
    readable = {2, 4, 8, 32, 64, 128}
    for pid in pids:
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle:
            continue
        opened += 1
        if diagnostics is not None:
            diagnostics["opened"] = opened
        try:
            info = Region()
            address = 0
            while kernel.VirtualQueryEx(handle, address, ct.byref(info), ct.sizeof(info)):
                base = info.base or 0
                address = base + info.size
                if not info.size or not address:
                    break
                if info.state != 0x1000 or info.protect not in readable:
                    continue
                tail = b""
                for start in range(base, address, 2**20):
                    length = min(2**20, address - start)
                    buf = ct.create_string_buffer(length)
                    count = ct.c_size_t()
                    ok = kernel.ReadProcessMemory(handle, start, buf, length, ct.byref(count))
                    if not ok:
                        tail = b""
                        continue
                    read_bytes += count.value
                    if diagnostics is not None:
                        diagnostics["read_bytes"] = read_bytes
                    data = tail + buf.raw[:count.value]
                    lower = data.lower()
                    for token in KEY_TOKEN.finditer(data):
                        key = bytes.fromhex(token[1].decode("ascii"))
                        if key in checked:
                            continue
                        checked.add(key)
                        for salt, page in pages.items():
                            if salt not in keys and first_page_matches(page, key):
                                keys[salt] = key
                    for salt, marker, encoding, key_width in salts:
                        if salt in keys:
                            continue
                        pos = lower.find(marker, key_width)
                        while pos >= 0:
                            try:
                                key = bytes.fromhex(data[pos-key_width:pos].decode(encoding))
                            except ValueError:
                                key = b""
                            if len(key) == 32 and first_page_matches(pages[salt], key):
                                keys[salt] = key
                                break
                            pos = lower.find(marker, pos + 1)
                    if keys.keys() == pages.keys():
                        return keys
                    tail = data[-192:]
        finally:
            kernel.CloseHandle(handle)
    if not opened:
        raise ValueError("Windows denied read access to all detected WeChat processes.")
    return keys


def decrypt(source, target, key):
    with source.open("rb") as inp, target.open("wb") as out:
        for number, page in enumerate(iter(lambda: inp.read(PAGE), b""), 1):
            if len(page) != PAGE:
                raise ValueError(f"Incomplete database page in {source.name}.")
            if number == 1:
                if not first_page_matches(page, key):
                    raise ValueError(f"Cannot authenticate {source.name}.")
                salt = bytes(value ^ 0x3A for value in page[:16])
                mac_key = hashlib.pbkdf2_hmac("sha512", key, salt, 2, 32)
            offset = 16 if number == 1 else 0
            payload = page[offset:-RESERVE]
            iv = page[-RESERVE:-64]
            digest = hmac.digest(mac_key, payload + iv + number.to_bytes(4, "little"), "sha512")
            if not hmac.compare_digest(digest, page[-64:]):
                raise ValueError(f"Page authentication failed in {source.name} at page {number}.")
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            if offset:
                out.write(SQLITE)
            out.write(cipher.update(payload) + cipher.finalize() + bytes(RESERVE))


def decoded(value):
    if not isinstance(value, bytes):
        return value
    if value.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            import zstandard
        except ImportError:
            script = (
                "const fs=require('node:fs'),z=require('node:zlib');"
                "process.stdout.write(z.zstdDecompressSync(fs.readFileSync(0),"
                "{maxOutputLength:8000000}));"
            )
            result = subprocess.run(
                ["node", "-e", script], input=value, capture_output=True,
                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise ValueError("Unable to decompress a WeChat message.")
            value = result.stdout
        else:
            with zstandard.ZstdDecompressor().stream_reader(value) as stream:
                value = stream.read(8_000_001)
        if len(value) > 8_000_000:
            raise ValueError("Message exceeds the decompression limit.")
    return value.decode("utf-8", errors="replace")


def contacts(db):
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return {
            username: remark or nickname or username
            for username, nickname, remark in conn.execute("SELECT username, nick_name, remark FROM contact")
        }


def query(db, chat_id, limit, since, until):
    table = "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=? COLLATE NOCASE", (table,)).fetchone():
            return []
        return conn.execute(
            f"""SELECT m.create_time, m.local_type, n.user_name, m.message_content
                FROM "{table}" AS m LEFT JOIN Name2Id AS n ON n.rowid=m.real_sender_id
                WHERE m.create_time>=? AND m.create_time<?
                ORDER BY m.create_time DESC, m.local_id DESC LIMIT ?""",
            (since, until, limit),
        ).fetchall()


def run(args):
    root = args.storage
    contact_db = root / "contact" / "contact.db"
    shards = sorted(path for path in (root / "message").glob("message_*.db")
                    if re.fullmatch(r"message_\d+\.db", path.name))
    verify = getattr(args, "verify", False)
    sources = [contact_db, *shards] if args.chat or verify else [contact_db]
    if not contact_db.is_file() or (args.chat and not shards):
        raise ValueError("Pass the account's db_storage directory.")
    headers = {}
    for path in sources:
        with path.open("rb") as file:
            headers[path] = file.read(PAGE)
    pages = {page[:16]: page for page in headers.values() if not page.startswith(SQLITE)}
    diagnostics = {}
    capture = getattr(args, "capture", None)
    if pages and capture:
        result = json.loads(capture.read_bytes())
        try:
            secret = bytes.fromhex(result["data"]["decrypt_key"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Capture file does not contain a valid database key.") from None
        if result.get("success") is not True or len(secret) != 32:
            raise ValueError("Capture file does not contain a valid database key.")
        keys = {}
        for salt, page in pages.items():
            key = hashlib.pbkdf2_hmac("sha512", secret, salt, 256000, 32)
            if first_page_matches(page, key):
                keys[salt] = key
    else:
        keys = process_keys(pages, diagnostics) if pages else {}
    if verify:
        return {
            "scan": diagnostics,
            "databases": {
                path.name: "plaintext" if page.startswith(SQLITE)
                else "verified" if page[:16] in keys else "unmatched"
                for path, page in headers.items()
            },
        }
    missing = [path.name for path, page in headers.items() if not page.startswith(SQLITE) and page[:16] not in keys]
    if missing:
        raise ValueError("No verified decryption key found for: " + ", ".join(missing))
    with tempfile.TemporaryDirectory(prefix="wechat-reader-", dir=getattr(args, "scratch", None)) as tmp:
        copies = {}
        for path, header in headers.items():
            if header.startswith(SQLITE):
                copies[path] = path
            else:
                copy = Path(tmp) / path.name
                decrypt(path, copy, keys[header[:16]])
                copies[path] = copy
        names = contacts(copies[contact_db])
        if not args.chat:
            return [{"id": uid, "name": name, "group": uid.endswith("@chatroom")} for uid, name in names.items()]
        rows = []
        for path in shards:
            rows.extend(query(copies[path], args.chat, args.limit, args.since, args.until))
        rows.sort(key=lambda row: row[0], reverse=True)
        return [
            {"timestamp": ts, "type": kind, "sender_id": sender,
             "sender_name": names.get(sender, sender), "content": decoded(content)}
            for ts, kind, sender, content in rows[:args.limit]
        ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("storage", type=Path)
    parser.add_argument("--chat", help="Exact conversation ID from the contact list")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--since", type=int, default=0, help="Inclusive Unix timestamp")
    parser.add_argument("--until", type=int, default=253402300799, help="Exclusive Unix timestamp")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", action="store_true", help="Report database compatibility without exporting records")
    parser.add_argument("--scratch", type=Path, help="Local directory for temporary decrypted copies")
    parser.add_argument("--capture", type=Path, help="Local WeFlow key-capture JSON")
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000 or args.since >= args.until:
        parser.error("Use a limit from 1 to 10000 and a valid time range.")
    if not args.verify and args.output is None:
        parser.error("--output is required when exporting records.")
    try:
        result = run(args)
        if args.verify:
            print(json.dumps(result, indent=2))
            return 0
        with args.output.open("x", encoding="utf-8") as file:
            json.dump({"messages" if args.chat else "contacts": result}, file, ensure_ascii=False, indent=2)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Exported {len(result)} records.")


if __name__ == "__main__":
    main()
