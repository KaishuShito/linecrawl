#!/usr/bin/env python3
import argparse
import base64
import datetime as dt
import glob
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_DB = Path.home() / ".linecrawl" / "linecrawl.db"
DEFAULT_DOWNLOADS = Path.home() / "Downloads"
DEFAULT_DESKTOP_SAVE_TIMEOUT = 120.0
DEFAULT_LABEL = "com.linecrawl.watch"
DEFAULT_LINE_DATA = (
    Path.home()
    / "Library"
    / "Containers"
    / "jp.naver.line.mac"
    / "Data"
    / "Library"
    / "Containers"
    / "jp.naver.line"
    / "Data"
)
DEFAULT_EDB_SNAPSHOT_ROOT = Path.home() / ".linecrawl" / "edb_snapshots"
DEFAULT_CHROME_DEBUG_URL = "http://127.0.0.1:9222"
DEFAULT_WEB_LABEL = "com.linecrawl.webwatch"
MEDIA_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/heic": ".heic",
    "image/avif": ".avif",
}
DATE_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\s+(.+)$")
MSG_RE = re.compile(r"^(\d{1,2}):(\d{2})\s+(.+?)(?:\s+(.*))?$")
WEB_LINE_URL_PREFIX = "chrome-extension://ophjlpahpchlmihnnnihgmmeilfjmjjc/index.html"
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)?\b", re.IGNORECASE)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys=on")
    conn.execute("pragma journal_mode=wal")
    init_db(conn)
    return conn


def init_db(conn):
    conn.executescript(
        """
        create table if not exists chats (
            id text primary key,
            name text not null unique,
            kind text not null default 'line_chat',
            created_at text not null,
            updated_at text not null
        );

        create table if not exists members (
            id text primary key,
            display_name text not null unique,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists source_files (
            id text primary key,
            path text not null unique,
            chat_id text not null references chats(id) on delete cascade,
            size integer not null,
            mtime real not null,
            sha256 text not null,
            imported_at text not null,
            message_count integer not null
        );

        create table if not exists messages (
            id text primary key,
            chat_id text not null references chats(id) on delete cascade,
            sender_id text references members(id),
            sender_name text not null,
            created_at text not null,
            local_date text not null,
            local_time text not null,
            content text not null,
            source_file_id text not null references source_files(id) on delete cascade,
            source_line integer not null,
            raw_json text not null,
            imported_at text not null
        );

        create table if not exists media (
            id text primary key,
            chat_id text not null references chats(id) on delete cascade,
            message_id text,
            kind text not null default 'image',
            path text not null,
            sha256 text not null,
            bytes integer not null,
            width integer,
            height integer,
            content_type text,
            origin_url text,
            source text,
            quality text not null default 'thumbnail',
            captured_at text not null
        );

        create index if not exists idx_messages_chat_created on messages(chat_id, created_at, id);
        create index if not exists idx_messages_sender_created on messages(sender_id, created_at, id);
        create index if not exists idx_messages_created on messages(created_at);
        create index if not exists idx_media_message on media(message_id);
        create index if not exists idx_media_chat_captured on media(chat_id, captured_at);
        """
    )
    media_columns = {row[1] for row in conn.execute("pragma table_info(media)")}
    if "quality" not in media_columns:
        conn.execute("alter table media add column quality text not null default 'thumbnail'")
    conn.execute(
        """
        create virtual table if not exists message_fts
        using fts5(content, sender_name, chat_name, tokenize='unicode61')
        """
    )


def stable_id(*parts):
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:32]


def file_sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def chat_name_from_path(path):
    name = path.stem
    if name.startswith("[LINE]"):
        name = name[len("[LINE]") :]
    return name.strip() or path.stem


def parse_line_export(path):
    current_date = None
    current_weekday = None
    current = None
    messages = []

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()

    def flush():
        nonlocal current
        if current is not None:
            current["content"] = "\n".join(current["content_lines"]).strip()
            current.pop("content_lines", None)
            messages.append(current)
            current = None

    for lineno, line in enumerate(lines, start=1):
        date_match = DATE_RE.match(line)
        if date_match:
            flush()
            year, month, day, weekday = date_match.groups()
            current_date = f"{year}-{month}-{day}"
            current_weekday = weekday
            continue

        msg_match = MSG_RE.match(line)
        if current_date and msg_match:
            flush()
            hour, minute, sender, content = msg_match.groups()
            local_time = f"{int(hour):02d}:{minute}"
            current = {
                "local_date": current_date,
                "local_time": local_time,
                "weekday": current_weekday,
                "sender_name": sender.strip(),
                "content_lines": [(content or "").strip()],
                "source_line": lineno,
            }
            continue

        if current is not None:
            current["content_lines"].append(line)

    flush()
    return [m for m in messages if m["sender_name"] and (m["content"] or m["sender_name"])]


def parse_web_date_label(label, today=None):
    text = str(label or "").strip()
    if not text:
        return None
    today = today or dt.date.today()
    lower = text.lower()
    if lower == "today":
        return today.isoformat()
    if lower == "yesterday":
        return (today - dt.timedelta(days=1)).isoformat()

    year = today.year
    match = re.match(r"^(?:May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Jan|Feb|Mar|Apr)\s+\d{1,2}(?:\([A-Za-z]+\))?$", text)
    if match:
        cleaned = re.sub(r"\([^)]+\)", "", text)
        for fmt in ("%b %d", "%B %d"):
            try:
                parsed = dt.datetime.strptime(cleaned, fmt)
                return dt.date(year, parsed.month, parsed.day).isoformat()
            except ValueError:
                pass

    match = re.match(r"^(\d{4})[./-](\d{1,2})[./-](\d{1,2})", text)
    if match:
        y, m, d = [int(v) for v in match.groups()]
        return dt.date(y, m, d).isoformat()

    match = re.match(r"^(\d{1,2})[./-](\d{1,2})", text)
    if match:
        m, d = [int(v) for v in match.groups()]
        return dt.date(year, m, d).isoformat()
    return None


def normalize_web_time(value):
    match = TIME_RE.search(str(value or ""))
    if not match:
        return ""
    hour = int(match.group(1))
    minute = match.group(2)
    ampm = match.group(3)
    if ampm:
        ampm = ampm.upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
    return f"{hour:02d}:{minute}"


def decode_media_data_url(data_url, item):
    match = re.match(r"data:([^;,]+);base64,(.+)$", str(data_url or ""), re.DOTALL)
    if not match:
        return None
    content_type = match.group(1).strip().lower()
    try:
        blob = base64.b64decode(match.group(2))
    except ValueError:
        return None
    if not blob:
        return None
    return {
        "sha256": hashlib.sha256(blob).hexdigest(),
        "data_bytes": blob,
        "ext": MEDIA_EXTENSIONS.get(content_type, ".bin"),
        "content_type": content_type,
        "width": item.get("natural_width"),
        "height": item.get("natural_height"),
        "origin_url": str(item.get("src") or ""),
        "quality": "thumbnail",
    }


def decode_image_row(item):
    thumbnail = decode_media_data_url(item.get("data"), item)
    full = decode_media_data_url(item.get("full_data"), item)
    if full:
        full["quality"] = "full"
        full["content_type"] = str(item.get("full_content_type") or full["content_type"]).strip().lower()
        full["ext"] = MEDIA_EXTENSIONS.get(full["content_type"], full["ext"])
        full["width"] = item.get("full_width") or full["width"]
        full["height"] = item.get("full_height") or full["height"]
    chosen = full or thumbnail
    if not chosen:
        return None
    # The thumbnail hash keys both the [Photo] message id and the media row, so
    # a later full-resolution capture upgrades in place instead of duplicating.
    chosen["key_sha"] = (thumbnail or full)["sha256"]
    return chosen


def sanitize_media_dir(name):
    cleaned = re.sub(r"[\\/:\x00-\x1f]+", "_", str(name or "")).strip(" .")
    return cleaned[:80] or "chat"


def media_root_for_db(db_path):
    return Path(db_path).expanduser().parent / "media"


def save_media_record(conn, media_root, chat_id, chat_name, message_id, media_item, stamp):
    data = media_item.get("data_bytes")
    sha = media_item.get("sha256")
    if not data or not sha or media_root is None:
        return False
    quality = media_item.get("quality") or "thumbnail"
    key_sha = media_item.get("key_sha") or sha
    media_id = stable_id("media", chat_id, key_sha)
    existing = conn.execute("select path, quality from media where id=?", (media_id,)).fetchone()
    if existing and existing["quality"] == "full" and quality != "full":
        # Never replace a stored full-resolution capture with a thumbnail.
        conn.execute("update media set message_id=? where id=?", (message_id, media_id))
        return True
    directory = Path(media_root).expanduser() / sanitize_media_dir(chat_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha[:24]}{media_item.get('ext') or '.bin'}"
    if not path.exists():
        path.write_bytes(data)
    conn.execute(
        """
        insert into media(
            id, chat_id, message_id, kind, path, sha256, bytes,
            width, height, content_type, origin_url, source, quality, captured_at
        ) values(?, ?, ?, 'image', ?, ?, ?, ?, ?, ?, ?, 'line-web', ?, ?)
        on conflict(id) do update set
            message_id=excluded.message_id,
            path=excluded.path,
            sha256=excluded.sha256,
            bytes=excluded.bytes,
            width=excluded.width,
            height=excluded.height,
            content_type=excluded.content_type,
            origin_url=excluded.origin_url,
            quality=excluded.quality,
            captured_at=excluded.captured_at
        """,
        (
            media_id,
            chat_id,
            message_id,
            str(path),
            sha,
            len(data),
            media_item.get("width"),
            media_item.get("height"),
            media_item.get("content_type"),
            media_item.get("origin_url"),
            quality,
            stamp,
        ),
    )
    if existing and existing["path"] != str(path):
        old_path = Path(existing["path"])
        shared = conn.execute(
            "select count(*) from media where path=? and id<>?", (existing["path"], media_id)
        ).fetchone()[0]
        # Only ever unlink files inside the media root, so a tampered DB path
        # can never make us delete an arbitrary file on disk.
        root = Path(media_root).expanduser().resolve()
        try:
            inside_root = old_path.resolve().is_relative_to(root)
        except (OSError, ValueError):
            inside_root = False
        if inside_root and old_path.exists() and not shared:
            old_path.unlink()
    return True


def normalize_web_line_dump(payload, owner_name="Me", today=None):
    today = today or dt.date.today()
    chat_name = (payload.get("chat_name") or payload.get("chat") or "LINE Web").strip()
    messages = []
    current_date = today.isoformat()
    for index, item in enumerate(payload.get("messages") or [], start=1):
        date_label = item.get("date_label")
        parsed_date = parse_web_date_label(date_label, today=today)
        if parsed_date:
            current_date = parsed_date
        local_date = parsed_date or item.get("local_date") or current_date
        local_time = normalize_web_time(item.get("time") or item.get("local_time")) or "00:00"
        if item.get("kind") == "image":
            media_item = decode_image_row(item)
            if not media_item:
                continue
            direction = item.get("direction")
            sender_name = item.get("sender_name") or (owner_name if direction == "outgoing" else chat_name)
            created_at = f"{local_date}T{local_time}:00"
            raw = {k: v for k, v in item.items() if k not in ("data", "full_data")}
            messages.append(
                {
                    "chat_name": chat_name,
                    "sender_name": sender_name,
                    "created_at": created_at,
                    "local_date": local_date,
                    "local_time": local_time,
                    "content": "[Photo]",
                    "source_line": index,
                    "internal_id": f"img:{media_item['key_sha'][:32]}",
                    "raw": raw,
                    "media": [media_item],
                }
            )
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        direction = item.get("direction")
        sender_name = item.get("sender_name") or (owner_name if direction == "outgoing" else chat_name)
        created_at = f"{local_date}T{local_time}:00"
        messages.append(
            {
                "chat_name": chat_name,
                "sender_name": sender_name,
                "created_at": created_at,
                "local_date": local_date,
                "local_time": local_time,
                "content": content,
                "source_line": index,
                "internal_id": item.get("id") or stable_id("line-web-dom", chat_name, created_at, sender_name, content),
                "raw": item,
            }
        )
    return messages


def applescript_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


LINE_WEB_DUMP_JS = r"""
(() => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 20 && r.height > 8 && s.visibility !== "hidden" && s.display !== "none";
  };
  const textOf = el => (el.innerText || el.textContent || "").replace(/\u200c|\u00ad/g, "").trim();
  const timeRe = /\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b/i;
  const dateRe = /^(Today|Yesterday|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:\([A-Za-z]+\))?|\d{1,4}[./-]\d{1,2}(?:[./-]\d{1,2})?)$/;

  function chatName() {
    const header = Array.from(document.querySelectorAll("button,[role=button],h1,h2,h3"))
      .map(textOf)
      .find(t => t && t.length <= 80 && !/^(All|Friends|Groups|Official accounts|more button)$/i.test(t));
    if (header) return header;
    const selected = Array.from(document.querySelectorAll('[aria-selected="true"], .selected, [class*="selected"]'))
      .map(textOf)
      .find(t => t && t.length <= 80);
    return selected || document.title.replace(/^LINE\s*[-–]\s*/i, "").trim() || "LINE Web";
  }

  function scrollRoot() {
    const candidates = Array.from(document.querySelectorAll("div,main,section"))
      .filter(el => visible(el) && el.scrollHeight > el.clientHeight + 80)
      .map(el => ({ el, r: el.getBoundingClientRect(), score: el.scrollHeight * el.clientHeight }));
    candidates.sort((a, b) => b.score - a.score);
    return candidates.find(c => c.r.left > 220 && c.r.top < innerHeight - 120)?.el || candidates[0]?.el || document.scrollingElement;
  }

  function collectVisible() {
    const root = scrollRoot();
    const input = document.querySelector("textarea,[contenteditable=true]");
    const inputTop = input ? input.getBoundingClientRect().top : innerHeight;
    const mainLeft = Math.max(240, innerWidth * 0.18);
    const rows = [];
    for (const el of Array.from(document.querySelectorAll("div,li,p,span"))) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.left < mainLeft || r.top < 0 || r.top > inputTop - 8) continue;
      const text = textOf(el);
      if (!text || text.length < 2) continue;
      if (dateRe.test(text)) {
        rows.push({ kind: "date", date_label: text, top: r.top, left: r.left });
        continue;
      }
      if (/^(Read|Enter a message|\u200c)$/.test(text) || timeRe.test(text) && text.length <= 12) continue;
      if (Array.from(el.children).some(child => textOf(child) === text)) continue;
      const parentText = el.parentElement ? textOf(el.parentElement) : "";
      const time = (parentText.match(timeRe) || text.match(timeRe) || [""])[0];
      const direction = r.left > innerWidth * 0.55 ? "outgoing" : "incoming";
      rows.push({
        kind: "message",
        id: `${direction}:${time}:${text}`,
        direction,
        time,
        content: text.replace(timeRe, "").replace(/\bRead\b/g, "").trim(),
        top: r.top,
        left: r.left,
      });
    }
    for (const img of Array.from(document.images)) {
      if (!visible(img)) continue;
      const r = img.getBoundingClientRect();
      if (r.left < mainLeft || r.top < -r.height || r.top > inputTop - 8) continue;
      if (r.width < 48 || r.height < 48) continue;
      const src = img.currentSrc || img.src || "";
      if (!src) continue;
      const parentText = img.parentElement ? textOf(img.parentElement) : "";
      const time = (parentText.match(timeRe) || [""])[0];
      const direction = r.left > innerWidth * 0.55 ? "outgoing" : "incoming";
      rows.push({
        kind: "image",
        id: `image:${direction}:${src}`,
        direction,
        time,
        src,
        natural_width: img.naturalWidth,
        natural_height: img.naturalHeight,
        top: r.top,
        left: r.left,
      });
    }
    rows.sort((a, b) => a.top - b.top || a.left - b.left);
    return { root, rows };
  }

  function dump() {
    const mode = window.__linecrawlMode || "dump";
    if (mode === "reset" || !window.__linecrawlSeen) {
      window.__linecrawlSeen = {};
      const root = scrollRoot();
      window.__linecrawlOriginalTop = root ? root.scrollTop : 0;
    }
    const seen = window.__linecrawlSeen;
    const { root, rows } = collectVisible();
    let lastDate = "";
    for (const row of rows) {
      if (row.kind === "date") {
        lastDate = row.date_label;
        continue;
      }
      if (row.kind === "message" && !row.content) continue;
      if (row.kind !== "message" && row.kind !== "image") continue;
      row.date_label = row.date_label || lastDate;
      seen[row.id] = row;
    }
    if (mode === "scroll" && root) {
      root.scrollTop = Math.max(0, root.scrollTop - Math.max(300, root.clientHeight * 0.85));
    }
    if (mode === "restore" && root && typeof window.__linecrawlOriginalTop === "number") {
      root.scrollTop = window.__linecrawlOriginalTop;
    }
    return JSON.stringify({
      ok: true,
      source: "line-web-chrome",
      url: location.href,
      title: document.title,
      chat_name: chatName(),
      extracted_at: new Date().toISOString(),
      messages: Object.values(seen).sort((a, b) => a.top - b.top || a.left - b.left),
    });
  }
  return dump();
})()
"""


LINE_WEB_MEDIA_FETCH_JS = r"""
(() => {
  const wanted = window.__linecrawlMediaSrcs || [];
  const cache = (window.__linecrawlImgCache = window.__linecrawlImgCache || {});
  const viaCanvas = src => {
    const img = Array.from(document.images).find(i => (i.currentSrc || i.src) === src);
    if (!img || !img.naturalWidth) throw new Error("image element not available");
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.getContext("2d").drawImage(img, 0, 0);
    return { src, data: canvas.toDataURL("image/jpeg", 0.92), content_type: "image/jpeg" };
  };
  const fetchOne = async src => {
    if (cache[src] && cache[src].data) return cache[src];
    let item = { src };
    try {
      const resp = await fetch(src);
      if (!resp.ok) throw new Error("http " + resp.status);
      const blob = await resp.blob();
      item.content_type = blob.type || "";
      item.data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error || new Error("read failed"));
        reader.readAsDataURL(blob);
      });
    } catch (err) {
      try {
        item = viaCanvas(src);
      } catch (err2) {
        item.error = String(err) + "; " + String(err2);
      }
    }
    cache[src] = item;
    return item;
  };
  return Promise.all(wanted.map(fetchOne)).then(items => JSON.stringify({ ok: true, images: items }));
})()
"""


LINE_WEB_MEDIA_CANVAS_JS = r"""
(() => {
  const wanted = window.__linecrawlMediaSrcs || [];
  const cache = (window.__linecrawlImgCache = window.__linecrawlImgCache || {});
  const items = wanted.map(src => {
    if (cache[src] && cache[src].data) return cache[src];
    let item = { src };
    try {
      const img = Array.from(document.images).find(i => (i.currentSrc || i.src) === src);
      if (!img || !img.naturalWidth) throw new Error("image element not available");
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      canvas.getContext("2d").drawImage(img, 0, 0);
      item.data = canvas.toDataURL("image/jpeg", 0.92);
      item.content_type = "image/jpeg";
    } catch (err) {
      item.error = String(err);
    }
    cache[src] = item;
    return item;
  });
  return JSON.stringify({ ok: true, images: items });
})()
"""


LINE_WEB_MEDIA_FULL_JS = r"""
(() => {
  const wanted = window.__linecrawlFullSrcs || [];
  const cache = (window.__linecrawlImgCache = window.__linecrawlImgCache || {});
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const isShown = el => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const toDataUrl = async el => {
    const src = el.currentSrc || el.src;
    try {
      const resp = await fetch(src);
      if (!resp.ok) throw new Error("http " + resp.status);
      const blob = await resp.blob();
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error || new Error("read failed"));
        reader.readAsDataURL(blob);
      });
      return { data, content_type: blob.type || "" };
    } catch (err) {
      const canvas = document.createElement("canvas");
      canvas.width = el.naturalWidth;
      canvas.height = el.naturalHeight;
      canvas.getContext("2d").drawImage(el, 0, 0);
      return { data: canvas.toDataURL("image/jpeg", 0.95), content_type: "image/jpeg" };
    }
  };
  const pressEscape = () => {
    for (const target of [document.activeElement, document.body, document]) {
      if (!target || !target.dispatchEvent) continue;
      for (const type of ["keydown", "keyup"]) {
        target.dispatchEvent(new KeyboardEvent(type, {
          key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true,
        }));
      }
    }
  };
  const closeViewer = async (fullImg, beforeButtons) => {
    for (let attempt = 0; attempt < 5; attempt++) {
      if (!isShown(fullImg)) return true;
      pressEscape();
      await sleep(200);
      if (!isShown(fullImg)) return true;
      const closers = Array.from(document.querySelectorAll("button,[role=button]"))
        .filter(el => !beforeButtons.has(el) && isShown(el))
        .filter(el => /close|dismiss|back/i.test(
          (el.getAttribute("aria-label") || "") + " " + (el.className || "") + " " + (el.title || "")
        ));
      if (closers.length) closers[0].click();
      await sleep(200);
    }
    return !isShown(fullImg);
  };
  const captureFull = async src => {
    const cached = cache["full:" + src];
    if (cached && cached.full_data) return cached;
    const item = { src };
    const img = Array.from(document.images).find(i => (i.currentSrc || i.src) === src);
    if (!img) {
      item.full_error = "image element not found";
      return item;
    }
    const beforeSrcs = new Set(Array.from(document.images).map(i => i.currentSrc || i.src));
    const beforeButtons = new Set(document.querySelectorAll("button,[role=button]"));
    const clickTarget = img.closest("button,[role=button],a") || img;
    clickTarget.click();
    let full = null;
    for (let i = 0; i < 40; i++) {
      await sleep(120);
      const fresh = Array.from(document.images).filter(candidate => {
        const candidateSrc = candidate.currentSrc || candidate.src;
        return candidateSrc && !beforeSrcs.has(candidateSrc) && candidate.complete &&
          candidate.naturalWidth >= Math.max(320, img.naturalWidth * 1.1) && isShown(candidate);
      });
      fresh.sort((a, b) => b.naturalWidth * b.naturalHeight - a.naturalWidth * a.naturalHeight);
      if (fresh.length) {
        full = fresh[0];
        break;
      }
    }
    if (full) {
      try {
        const encoded = await toDataUrl(full);
        item.full_data = encoded.data;
        item.full_content_type = encoded.content_type;
        item.full_width = full.naturalWidth;
        item.full_height = full.naturalHeight;
      } catch (err) {
        item.full_error = String(err);
      }
    } else {
      item.full_error = "full-resolution image did not appear after in-page click";
    }
    const closed = await closeViewer(full, beforeButtons);
    if (!closed) item.viewer_stuck = true;
    cache["full:" + src] = item;
    return item;
  };
  const run = async () => {
    const results = [];
    let stuck = false;
    for (const src of wanted) {
      if (stuck) {
        results.push({ src, full_error: "skipped: previous viewer failed to close" });
        continue;
      }
      const item = await captureFull(src);
      if (item.viewer_stuck) stuck = true;
      results.push(item);
    }
    return JSON.stringify({ ok: true, images: results, viewer_stuck: stuck });
  };
  return run();
})()
"""


def image_row_srcs(payload):
    srcs = []
    for row in payload.get("messages") or []:
        if row.get("kind") == "image" and row.get("src") and not row.get("data"):
            srcs.append(row["src"])
    return sorted(set(srcs))


def image_row_full_srcs(payload):
    srcs = []
    for row in payload.get("messages") or []:
        if row.get("kind") == "image" and row.get("src") and not row.get("full_data"):
            srcs.append(row["src"])
    return sorted(set(srcs))


def merge_media_items(payload, media_items):
    by_src = {item.get("src"): item for item in media_items or [] if item.get("src")}
    fetched = 0
    for row in payload.get("messages") or []:
        if row.get("kind") != "image":
            continue
        item = by_src.get(row.get("src"))
        if not item:
            continue
        if item.get("data"):
            row["data"] = item["data"]
            row["content_type"] = item.get("content_type") or ""
            fetched += 1
        elif item.get("error"):
            row["media_error"] = item["error"]
    payload["media_fetched"] = fetched
    return payload


def merge_full_media_items(payload, media_items):
    by_src = {item.get("src"): item for item in media_items or [] if item.get("src")}
    fetched = 0
    for row in payload.get("messages") or []:
        if row.get("kind") != "image":
            continue
        item = by_src.get(row.get("src"))
        if not item:
            continue
        if item.get("full_data"):
            row["full_data"] = item["full_data"]
            row["full_content_type"] = item.get("full_content_type") or ""
            row["full_width"] = item.get("full_width")
            row["full_height"] = item.get("full_height")
            fetched += 1
        elif item.get("full_error"):
            row["full_media_error"] = item["full_error"]
    payload["full_media_fetched"] = fetched
    return payload


def applescript_execute_line_js(js):
    script = f"""
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      if (URL of t) starts with "{WEB_LINE_URL_PREFIX}" then
        return execute t javascript {applescript_string(js)}
      end if
    end repeat
  end repeat
end tell
return ""
"""
    result = subprocess.run(["osascript"], input=script, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if "JavaScript" in detail and "Apple Events" in detail:
            raise RuntimeError(
                "Chrome blocks AppleScript JavaScript. Enable Chrome menu View > Developer > Allow JavaScript from Apple Events, then retry."
            )
        raise RuntimeError(detail)
    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError("No LINE Web tab found in Google Chrome.")
    return raw


def applescript_fetch_line_media(srcs):
    if not srcs:
        return []
    js = f"window.__linecrawlMediaSrcs={json.dumps(srcs)}; {LINE_WEB_MEDIA_CANVAS_JS};"
    raw = applescript_execute_line_js(js)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Chrome returned non-JSON media output: {raw[:200]}") from exc
    return payload.get("images") or []


def chrome_line_web_dump(scroll_steps=0, with_media=False, full_media=False):
    def execute_line_js(mode):
        js = f"window.__linecrawlMode={json.dumps(mode)}; {LINE_WEB_DUMP_JS};"
        return applescript_execute_line_js(js)

    execute_line_js("reset")
    for _ in range(max(0, int(scroll_steps))):
        execute_line_js("scroll")
        time.sleep(0.55)
    raw = execute_line_js("restore")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Chrome returned non-JSON output: {raw[:200]}") from exc
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "LINE Web dump failed.")
    payload["media_transport"] = "applescript"
    if with_media or full_media:
        try:
            merge_media_items(payload, applescript_fetch_line_media(image_row_srcs(payload)))
        except Exception as exc:
            payload["media_error"] = str(exc)
    if full_media:
        payload["full_media_error"] = (
            "full-resolution capture requires the CDP method; the AppleScript route cannot await the in-page viewer."
        )
    return payload


def chrome_line_tab_urls():
    script = """
tell application "Google Chrome"
  set out to {}
  repeat with w in windows
    repeat with t in tabs of w
      set end of out to URL of t
    end repeat
  end repeat
  return out
end tell
"""
    result = subprocess.run(["osascript"], input=script, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return [u.strip() for u in result.stdout.strip().split(", ") if u.strip()]


def chrome_applescript_js_allowed():
    script = """
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      if (URL of t) starts with "chrome-extension://ophjlpahpchlmihnnnihgmmeilfjmjjc/index.html" then
        return execute t javascript "JSON.stringify({ok:true})"
      end if
    end repeat
  end repeat
end tell
return ""
"""
    result = subprocess.run(["osascript"], input=script, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return bool(result.stdout.strip()), result.stdout.strip()


def normalize_ax_line_dump(payload, today=None):
    today = today or dt.date.today()
    chat_name = (payload.get("chat_name") or "LINE Web").strip()
    texts = []
    seen = set()
    for item in payload.get("items") or []:
        text = str(item.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if text in {"LINE", "All", "Friends", "Groups", "Official accounts", "Enter a message"}:
            continue
        frame = item.get("frame") or {}
        if len(text) < 2:
            continue
        texts.append(
            {
                "kind": "message",
                "id": stable_id("line-web-ax", chat_name, text, frame.get("x"), frame.get("y")),
                "direction": "outgoing" if float(frame.get("x") or 0) > 700 else "incoming",
                "time": normalize_web_time(text),
                "date_label": "Today",
                "content": TIME_RE.sub("", text).replace("Read", "").strip(),
                "top": frame.get("y") or 0,
                "left": frame.get("x") or 0,
                "raw": item,
            }
        )
    payload = dict(payload)
    payload["messages"] = [m for m in texts if m["content"]]
    payload["chat_name"] = chat_name
    payload.setdefault("extracted_at", now_iso())
    return payload


AX_LINE_DUMP_SWIFT = r'''
import Cocoa
import ApplicationServices
import Foundation

func attr(_ element: AXUIElement, _ name: String) -> Any? {
    var value: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    if err == .success {
        return value
    }
    return nil
}

func stringAttr(_ element: AXUIElement, _ name: String) -> String {
    if let value = attr(element, name) as? String {
        return value
    }
    if let value = attr(element, name) as? NSNumber {
        return value.stringValue
    }
    return ""
}

func frame(_ element: AXUIElement) -> [String: Double] {
    var point = CGPoint.zero
    var size = CGSize.zero
    if let value = attr(element, kAXPositionAttribute) as! AXValue? {
        AXValueGetValue(value, .cgPoint, &point)
    }
    if let value = attr(element, kAXSizeAttribute) as! AXValue? {
        AXValueGetValue(value, .cgSize, &size)
    }
    return ["x": point.x, "y": point.y, "width": size.width, "height": size.height]
}

func walk(_ element: AXUIElement, _ depth: Int, _ out: inout [[String: Any]]) {
    if depth > 18 { return }
    let role = stringAttr(element, kAXRoleAttribute)
    let title = stringAttr(element, kAXTitleAttribute)
    let value = stringAttr(element, kAXValueAttribute)
    let desc = stringAttr(element, kAXDescriptionAttribute)
    let text = [title, value, desc].filter { !$0.isEmpty }.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
    if !text.isEmpty {
        out.append(["role": role, "title": title, "value": value, "description": desc, "text": text, "frame": frame(element), "depth": depth])
    }
    if let children = attr(element, kAXChildrenAttribute) as? [AXUIElement] {
        for child in children {
            walk(child, depth + 1, &out)
        }
    }
}

let apps = NSWorkspace.shared.runningApplications.filter { $0.bundleIdentifier == "com.google.Chrome" }
var result: [String: Any] = ["ok": false, "source": "line-web-ax", "items": []]
for app in apps {
    let root = AXUIElementCreateApplication(app.processIdentifier)
    guard let windows = attr(root, kAXWindowsAttribute) as? [AXUIElement] else { continue }
    for window in windows {
        let title = stringAttr(window, kAXTitleAttribute)
        if !title.contains("LINE") { continue }
        var items: [[String: Any]] = []
        walk(window, 0, &items)
        result = ["ok": true, "source": "line-web-ax", "title": title, "chat_name": title.replacingOccurrences(of: "LINE - ", with: ""), "extracted_at": ISO8601DateFormatter().string(from: Date()), "items": items]
        break
    }
}
let data = try JSONSerialization.data(withJSONObject: result, options: [])
print(String(data: data, encoding: .utf8)!)
'''


def ax_line_web_dump():
    result = subprocess.run(["swift", "-"], input=AX_LINE_DUMP_SWIFT, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AX dump returned non-JSON output: {result.stdout[:200]}") from exc
    if not payload.get("ok"):
        raise RuntimeError("No readable LINE Web Chrome accessibility window found.")
    normalized = normalize_ax_line_dump(payload)
    if not normalized.get("messages"):
        raise RuntimeError("LINE Web accessibility window was found, but no message-like text was readable.")
    return normalized


def web_payload_sha(payload):
    body = json.dumps(payload.get("messages") or [], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def cmd_web_dump_js(args):
    if args.json:
        print_json({"ok": True, "script": LINE_WEB_DUMP_JS})
    else:
        print(LINE_WEB_DUMP_JS)
    return 0


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Set once from --allow-remote-cdp in main(). Off by default so every CDP
# HTTP/WebSocket connection is enforced to loopback and the tool's
# "local only, no remote network" guarantee stays honest.
ALLOW_REMOTE_CDP = False


def require_loopback_cdp(url, allow_remote=None):
    """Refuse a non-loopback Chrome DevTools HTTP or WebSocket URL.

    This is enforced at the network primitives (cdp_json and cdp_evaluate) so it
    covers every route into CDP — the default ``--method auto`` path, an
    explicit ``--method cdp``, ``web-doctor``, and the ``webSocketDebuggerUrl``
    that Chrome hands back (which is never trusted blindly). Pass
    ``--allow-remote-cdp`` to opt out.
    """
    if ALLOW_REMOTE_CDP if allow_remote is None else allow_remote:
        return
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise RuntimeError(
            f"Refusing non-loopback Chrome DevTools URL {url!r}. "
            "linecrawl only talks to a local Chrome by default; "
            "pass --allow-remote-cdp to override."
        )


def cdp_json(debug_url, path, timeout=1.5):
    require_loopback_cdp(debug_url)
    url = debug_url.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def cdp_line_tab(debug_url):
    try:
        tabs = cdp_json(debug_url, "/json/list")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"Chrome DevTools is not available at {debug_url}; launch Chrome with --remote-debugging-port=9222."
            ) from exc
        raise
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach Chrome DevTools at {debug_url}: {exc.reason}") from exc
    for tab in tabs:
        if str(tab.get("url") or "").startswith(WEB_LINE_URL_PREFIX):
            websocket_url = tab.get("webSocketDebuggerUrl")
            if websocket_url:
                return tab
    return None


def cdp_evaluate(websocket_url, expression, timeout=5.0, await_promise=False):
    require_loopback_cdp(websocket_url)
    try:
        import websocket
    except ImportError as exc:
        raise RuntimeError("Python websocket-client package is required for CDP import.") from exc

    ws = websocket.create_connection(websocket_url, timeout=timeout)
    try:
        payload = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": bool(await_promise),
            },
        }
        ws.send(json.dumps(payload))
        while True:
            message = json.loads(ws.recv())
            if message.get("id") != 1:
                continue
            if "error" in message:
                raise RuntimeError(message["error"].get("message") or str(message["error"]))
            result = message.get("result", {}).get("result", {})
            if "exceptionDetails" in message.get("result", {}):
                details = message["result"]["exceptionDetails"]
                raise RuntimeError(details.get("text") or "CDP Runtime.evaluate failed.")
            return result.get("value", "")
    finally:
        ws.close()


def cdp_fetch_line_media(websocket_url, srcs, timeout=45.0):
    if not srcs:
        return []
    js = f"window.__linecrawlMediaSrcs={json.dumps(srcs)}; {LINE_WEB_MEDIA_FETCH_JS};"
    raw = cdp_evaluate(websocket_url, js, timeout=timeout, await_promise=True)
    if not raw:
        raise RuntimeError("Chrome DevTools returned an empty LINE media payload.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Chrome DevTools returned non-JSON media output: {raw[:200]}") from exc
    return payload.get("images") or []


def cdp_fetch_line_full_media(websocket_url, srcs, timeout=None):
    if not srcs:
        return []
    if timeout is None:
        # Each image needs an in-page viewer open/capture/close cycle.
        timeout = min(20.0 + 8.0 * len(srcs), 180.0)
    js = f"window.__linecrawlFullSrcs={json.dumps(srcs)}; {LINE_WEB_MEDIA_FULL_JS};"
    raw = cdp_evaluate(websocket_url, js, timeout=timeout, await_promise=True)
    if not raw:
        raise RuntimeError("Chrome DevTools returned an empty full-media payload.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Chrome DevTools returned non-JSON full-media output: {raw[:200]}") from exc
    return payload.get("images") or []


def cdp_line_web_dump(debug_url=DEFAULT_CHROME_DEBUG_URL, scroll_steps=0, with_media=False, full_media=False):
    tab = cdp_line_tab(debug_url)
    if not tab:
        raise RuntimeError(f"No LINE Web tab found in Chrome DevTools at {debug_url}.")
    websocket_url = tab["webSocketDebuggerUrl"]

    def execute_line_js(mode):
        js = f"window.__linecrawlMode={json.dumps(mode)}; {LINE_WEB_DUMP_JS};"
        raw = cdp_evaluate(websocket_url, js)
        if not raw:
            raise RuntimeError("Chrome DevTools returned an empty LINE Web dump.")
        return raw

    execute_line_js("reset")
    for _ in range(max(0, int(scroll_steps))):
        execute_line_js("scroll")
        time.sleep(0.55)
    raw = execute_line_js("restore")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Chrome DevTools returned non-JSON output: {raw[:200]}") from exc
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "LINE Web CDP dump failed.")
    payload["media_transport"] = "cdp"
    if with_media or full_media:
        try:
            merge_media_items(payload, cdp_fetch_line_media(websocket_url, image_row_srcs(payload)))
        except Exception as exc:
            payload["media_error"] = str(exc)
    if full_media:
        try:
            items = cdp_fetch_line_full_media(websocket_url, image_row_full_srcs(payload))
            merge_full_media_items(payload, items)
            if any(item.get("viewer_stuck") for item in items):
                payload["full_media_error"] = "LINE Web image viewer could not be closed; remaining captures were skipped."
        except Exception as exc:
            payload["full_media_error"] = str(exc)
    return payload


def line_web_dump(method="auto", debug_url=DEFAULT_CHROME_DEBUG_URL, scroll_steps=0, with_media=False, full_media=False):
    errors = []
    if method in ("auto", "cdp"):
        try:
            return cdp_line_web_dump(
                debug_url=debug_url, scroll_steps=scroll_steps, with_media=with_media, full_media=full_media
            )
        except Exception as exc:
            errors.append(f"cdp: {exc}")
            if method == "cdp":
                raise
    if method in ("auto", "applescript"):
        try:
            return chrome_line_web_dump(scroll_steps=scroll_steps, with_media=with_media, full_media=full_media)
        except Exception as exc:
            errors.append(f"applescript: {exc}")
            if method == "applescript":
                raise
    if method in ("auto", "ax"):
        try:
            return ax_line_web_dump()
        except Exception as exc:
            errors.append(f"ax: {exc}")
            if method == "ax":
                raise
    raise RuntimeError("; ".join(errors) or "No LINE Web import method available.")


def upsert_chat(conn, name):
    chat_id = stable_id("chat", name)
    stamp = now_iso()
    conn.execute(
        """
        insert into chats(id, name, created_at, updated_at)
        values(?, ?, ?, ?)
        on conflict(id) do update set name=excluded.name, updated_at=excluded.updated_at
        """,
        (chat_id, name, stamp, stamp),
    )
    return chat_id


def upsert_member(conn, name):
    member_id = stable_id("member", name)
    stamp = now_iso()
    conn.execute(
        """
        insert into members(id, display_name, created_at, updated_at)
        values(?, ?, ?, ?)
        on conflict(id) do update set display_name=excluded.display_name, updated_at=excluded.updated_at
        """,
        (member_id, name, stamp, stamp),
    )
    return member_id


def import_file(conn, path, force=False):
    path = path.expanduser().resolve()
    chat_name = chat_name_from_path(path)
    chat_id = upsert_chat(conn, chat_name)
    stat = path.stat()
    sha = file_sha256(path)
    source_id = stable_id("source", str(path))

    existing = conn.execute(
        "select sha256, message_count from source_files where id=?", (source_id,)
    ).fetchone()
    if existing and existing["sha256"] == sha and not force:
        return {"path": str(path), "chat": chat_name, "status": "unchanged", "messages": existing["message_count"]}

    if existing:
        conn.execute("delete from message_fts where rowid in (select rowid from messages where source_file_id=?)", (source_id,))
        conn.execute("delete from messages where source_file_id=?", (source_id,))
        conn.execute("delete from source_files where id=?", (source_id,))

    messages = parse_line_export(path)
    stamp = now_iso()
    conn.execute(
        """
        insert into source_files(id, path, chat_id, size, mtime, sha256, imported_at, message_count)
        values(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, str(path), chat_id, stat.st_size, stat.st_mtime, sha, stamp, len(messages)),
    )

    for msg in messages:
        sender_id = upsert_member(conn, msg["sender_name"])
        content = msg["content"]
        created_at = f"{msg['local_date']}T{msg['local_time']}:00"
        msg_id = stable_id(
            "message",
            chat_id,
            created_at,
            msg["sender_name"],
            content,
            msg["source_line"],
        )
        raw = json.dumps(msg, ensure_ascii=False)
        conn.execute(
            """
            insert or replace into messages(
                id, chat_id, sender_id, sender_name, created_at, local_date, local_time,
                content, source_file_id, source_line, raw_json, imported_at
            ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id,
                chat_id,
                sender_id,
                msg["sender_name"],
                created_at,
                msg["local_date"],
                msg["local_time"],
                content,
                source_id,
                msg["source_line"],
                raw,
                stamp,
            ),
        )
        rowid = conn.execute("select rowid from messages where id=?", (msg_id,)).fetchone()[0]
        conn.execute(
            "insert into message_fts(rowid, content, sender_name, chat_name) values(?, ?, ?, ?)",
            (rowid, content, msg["sender_name"], chat_name),
        )

    return {"path": str(path), "chat": chat_name, "status": "imported", "messages": len(messages)}


def import_normalized_messages(conn, source_key, source_label, source_stat, source_sha, messages, force=False, media_root=None):
    source_id = stable_id("source", source_key)
    existing = conn.execute(
        "select sha256, message_count from source_files where id=?", (source_id,)
    ).fetchone()
    if existing and existing["sha256"] == source_sha and not force:
        return {
            "path": source_label,
            "status": "unchanged",
            "messages": existing["message_count"],
        }

    if existing:
        conn.execute(
            "delete from message_fts where rowid in (select rowid from messages where source_file_id=?)",
            (source_id,),
        )
        conn.execute("delete from messages where source_file_id=?", (source_id,))
        conn.execute("delete from source_files where id=?", (source_id,))

    stamp = now_iso()
    fallback_chat = Path(source_label.split("#", 1)[0]).stem or "LINE EDB"
    chat_names = {m.get("chat_name") or fallback_chat for m in messages}
    if len(chat_names) == 1:
        source_chat = next(iter(chat_names))
    else:
        source_chat = f"EDB import: {fallback_chat}"
    source_chat_id = upsert_chat(conn, source_chat)

    conn.execute(
        """
        insert into source_files(id, path, chat_id, size, mtime, sha256, imported_at, message_count)
        values(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            source_label,
            source_chat_id,
            source_stat["size"],
            source_stat["mtime"],
            source_sha,
            stamp,
            len(messages),
        ),
    )

    imported = 0
    media_saved = 0
    for index, msg in enumerate(messages, start=1):
        chat_name = msg.get("chat_name") or fallback_chat
        chat_id = upsert_chat(conn, chat_name)
        sender_name = msg.get("sender_name") or "(unknown)"
        sender_id = upsert_member(conn, sender_name)
        content = msg.get("content") or ""
        created_at = msg["created_at"]
        local_date = msg.get("local_date") or created_at[:10]
        local_time = msg.get("local_time") or created_at[11:16]
        source_line = msg.get("source_line") or index
        internal_id = msg.get("internal_id")
        if internal_id is not None and str(internal_id).startswith("img:"):
            # Image identity is content-hash based; keep the id independent of
            # the viewport position so re-dumps do not duplicate photo messages.
            msg_id = stable_id("edb-message", chat_id, internal_id, created_at, sender_name, content)
        else:
            msg_id = stable_id(
                "edb-message",
                chat_id,
                internal_id if internal_id is not None else source_id,
                created_at,
                sender_name,
                content,
                source_line,
            )
        raw = json.dumps(msg.get("raw") or msg, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            insert or replace into messages(
                id, chat_id, sender_id, sender_name, created_at, local_date, local_time,
                content, source_file_id, source_line, raw_json, imported_at
            ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id,
                chat_id,
                sender_id,
                sender_name,
                created_at,
                local_date,
                local_time,
                content,
                source_id,
                source_line,
                raw,
                stamp,
            ),
        )
        rowid = conn.execute("select rowid from messages where id=?", (msg_id,)).fetchone()[0]
        conn.execute(
            "insert or replace into message_fts(rowid, content, sender_name, chat_name) values(?, ?, ?, ?)",
            (rowid, content, sender_name, chat_name),
        )
        imported += 1
        for media_item in msg.get("media") or []:
            if save_media_record(conn, media_root, chat_id, chat_name, msg_id, media_item, stamp):
                media_saved += 1

    return {"path": source_label, "status": "imported", "messages": imported, "media": media_saved}


def print_table(rows, columns):
    widths = {c: len(c) for c in columns}
    rendered = []
    for row in rows:
        item = {c: str(row[c] if isinstance(row, sqlite3.Row) else row.get(c, "")) for c in columns}
        rendered.append(item)
        for c in columns:
            widths[c] = min(max(widths[c], len(item[c])), 60)
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for item in rendered:
        print("  ".join(item[c][: widths[c]].ljust(widths[c]) for c in columns))


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_import(args):
    conn = connect(args.db)
    paths = []
    for raw in args.paths:
        raw_path = Path(raw)
        if raw_path.exists():
            expanded = [raw_path]
        elif any(ch in raw for ch in "*?["):
            expanded = [Path(p) for p in glob.glob(raw)]
        else:
            expanded = [raw_path]
        paths.extend(expanded)
    if not paths:
        if args.json:
            print_json({"ok": False, "error": {"code": "no_files_matched", "message": "No files matched."}})
        else:
            print("No files matched.", file=sys.stderr)
        return 1
    with conn:
        results = [import_file(conn, p, force=args.force) for p in paths]
    if args.json:
        print_json({"ok": True, "results": results})
    else:
        print_table(results, ["status", "messages", "chat", "path"])
    return 0


def cmd_import_downloads(args):
    args.paths = [str(p) for p in sorted(args.downloads.glob("[[]LINE[]]*.txt"))]
    return cmd_import(args)


def import_web_payload(conn, payload, owner_name="Me", force=False, media_root=None):
    messages = normalize_web_line_dump(payload, owner_name=owner_name)
    source_url = payload.get("url") or "line-web"
    extracted_at = payload.get("extracted_at") or now_iso()
    source_stat = {
        "size": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        "mtime": time.time(),
    }
    source_sha = web_payload_sha(payload)
    source_label = f"{source_url}#dom:{source_sha[:16]}"
    source_key = f"line-web:{source_url}:{source_sha[:16]}"
    result = import_normalized_messages(
        conn,
        source_key,
        source_label,
        source_stat,
        source_sha,
        messages,
        force=force,
        media_root=media_root,
    )
    result["chat"] = payload.get("chat_name") or "LINE Web"
    result["url"] = source_url
    result["extracted_at"] = extracted_at
    return result


def cmd_web_dump_current(args):
    try:
        payload = line_web_dump(
            method=args.method,
            debug_url=args.chrome_debug_url,
            scroll_steps=args.scroll_steps,
            with_media=args.with_media or args.full_media,
            full_media=args.full_media,
        )
    except Exception as exc:
        if args.json:
            print_json({"ok": False, "error": {"code": "web_dump_failed", "message": str(exc)}})
        else:
            print(f"web dump failed: {exc}", file=sys.stderr)
        return 1
    if args.output:
        args.output.expanduser().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print_json(payload)
    else:
        print(f"chat: {payload.get('chat_name')}")
        print(f"messages: {len(payload.get('messages') or [])}")
        print(f"url: {payload.get('url')}")
        if args.output:
            print(f"output: {args.output.expanduser()}")
    return 0


def cmd_web_import_current(args):
    try:
        payload = line_web_dump(
            method=args.method,
            debug_url=args.chrome_debug_url,
            scroll_steps=args.scroll_steps,
            with_media=args.with_media or args.full_media,
            full_media=args.full_media,
        )
    except Exception as exc:
        if args.json:
            print_json({"ok": False, "error": {"code": "web_dump_failed", "message": str(exc)}})
        else:
            print(f"web dump failed: {exc}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    with conn:
        result = import_web_payload(
            conn, payload, owner_name=args.owner_name, force=args.force, media_root=media_root_for_db(args.db)
        )
    if args.json:
        print_json({"ok": True, "import": result})
    else:
        print_table([result], ["status", "messages", "media", "chat", "path"])
    return 0


def cmd_web_watch_current(args):
    conn = connect(args.db)
    media_root = media_root_for_db(args.db)
    if not args.json:
        print(f"Watching LINE Web tab every {args.interval:g}s", flush=True)
    while True:
        try:
            payload = line_web_dump(
                method=args.method,
                debug_url=args.chrome_debug_url,
                scroll_steps=args.scroll_steps,
                with_media=args.with_media or args.full_media,
                full_media=args.full_media,
            )
            with conn:
                result = import_web_payload(
                    conn, payload, owner_name=args.owner_name, force=args.force, media_root=media_root
                )
            if args.json:
                print_json({"ok": True, "import": result})
            elif args.verbose or result["status"] != "unchanged":
                print(
                    f"{result['status']}: {result['chat']} ({result['messages']} messages, "
                    f"{result.get('media', 0)} media) {result['extracted_at']}",
                    flush=True,
                )
        except Exception as exc:
            if args.json:
                print_json({"ok": False, "error": {"code": "web_watch_failed", "message": str(exc)}})
            else:
                print(f"web watch failed: {exc}", file=sys.stderr, flush=True)
            if args.fail_fast:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_web_import_json(args):
    payload = json.loads(args.path.expanduser().read_text(encoding="utf-8"))
    conn = connect(args.db)
    with conn:
        result = import_web_payload(
            conn, payload, owner_name=args.owner_name, force=args.force, media_root=media_root_for_db(args.db)
        )
    if args.json:
        print_json({"ok": True, "import": result})
    else:
        print_table([result], ["status", "messages", "media", "chat", "path"])
    return 0


def cmd_web_doctor(args):
    profile_root = Path(args.chrome_profile_root).expanduser()
    indexeddb = profile_root / "IndexedDB" / "chrome-extension_ophjlpahpchlmihnnnihgmmeilfjmjjc_0.indexeddb.leveldb"
    local_settings = profile_root / "Local Extension Settings" / "ophjlpahpchlmihnnnihgmmeilfjmjjc"
    payload = {
        "ok": True,
        "chrome_debug_url": args.chrome_debug_url,
        "cdp_available": False,
        "cdp_line_tab": False,
        "applescript_line_tab": False,
        "applescript_js_allowed": False,
        "ax_line_window_readable": False,
        "extension_indexeddb_exists": indexeddb.exists(),
        "extension_local_settings_exists": local_settings.exists(),
    }
    try:
        tabs = cdp_json(args.chrome_debug_url, "/json/list")
        payload["cdp_available"] = True
        payload["cdp_line_tab"] = any(str(tab.get("url") or "").startswith(WEB_LINE_URL_PREFIX) for tab in tabs)
    except Exception as exc:
        payload["cdp_error"] = str(exc)

    try:
        urls = chrome_line_tab_urls()
        payload["applescript_line_tab"] = any(u.startswith(WEB_LINE_URL_PREFIX) for u in urls)
        payload["line_tab_urls"] = [u for u in urls if u.startswith(WEB_LINE_URL_PREFIX)]
    except Exception as exc:
        payload["applescript_tabs_error"] = str(exc)

    allowed, detail = chrome_applescript_js_allowed()
    payload["applescript_js_allowed"] = allowed
    if not allowed and detail:
        payload["applescript_js_error"] = detail

    try:
        ax_payload = ax_line_web_dump()
        payload["ax_line_window_readable"] = True
        payload["ax_messages"] = len(ax_payload.get("messages") or [])
    except Exception as exc:
        payload["ax_error"] = str(exc)

    if args.json:
        print_json(payload)
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


def recent_line_exports(root, since):
    root = root.expanduser()
    if not root.exists():
        return []
    return [
        p
        for p in root.glob("[[]LINE[]]*.txt")
        if p.is_file() and p.stat().st_mtime >= since
    ]


def newest_line_export(root, before, since):
    before_paths = {str(p.resolve()) for p in before}
    candidates = [
        p
        for p in recent_line_exports(root, since)
        if str(p.resolve()) not in before_paths
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def run_osascript(script):
    return subprocess.run(
        ["osascript", "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )


def line_window_geometry():
    script = r'''
tell application "System Events" to tell process "LINE"
  set targetWindow to window "LINE"
  set p to position of targetWindow
  set s to size of targetWindow
  return (item 1 of p as text) & "," & (item 2 of p as text) & "," & (item 1 of s as text) & "," & (item 2 of s as text)
end tell
'''
    result = run_osascript(script)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    x, y, w, h = [int(float(v)) for v in result.stdout.strip().split(",")]
    return {"x": x, "y": y, "width": w, "height": h}


def swift_click(x, y):
    swift = f"""
import CoreGraphics
import Foundation
let x = {float(x)}
let y = {float(y)}
for t in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {{
    let e = CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left)!
    e.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
}}
"""
    result = subprocess.run(["swift", "-e", swift], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def activate_line():
    result = run_osascript('tell application id "jp.naver.line.mac" to activate')
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def desktop_save_click_sequence(args):
    activate_line()
    time.sleep(args.ui_delay)
    geom = line_window_geometry()

    menu_x = geom["x"] + geom["width"] - args.menu_right_offset
    menu_y = geom["y"] + args.menu_top_offset
    save_x = geom["x"] + geom["width"] - args.save_right_offset
    save_y = geom["y"] + args.save_top_offset

    if args.json and args.dry_run:
        print_json(
            {
                "ok": True,
                "geometry": geom,
                "clicks": {
                    "open_menu": None if args.menu_already_open else {"x": menu_x, "y": menu_y},
                    "save_chat": {"x": save_x, "y": save_y},
                },
            }
        )
        return None

    if args.pre_click_delay > 0:
        time.sleep(args.pre_click_delay)

    if not args.menu_already_open:
        swift_click(menu_x, menu_y)
        time.sleep(args.ui_delay)
    swift_click(save_x, save_y)
    return geom


def cmd_desktop_save_current(args):
    watch_dir = args.watch_dir.expanduser()
    before = list(watch_dir.glob("[[]LINE[]]*.txt")) if watch_dir.exists() else []
    started = time.time()

    try:
        geom = desktop_save_click_sequence(args)
    except Exception as exc:
        if args.json:
            print_json({"ok": False, "error": {"code": "desktop_click_failed", "message": str(exc)}})
        else:
            print(f"desktop click failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        if not args.json:
            print(f"geometry: {geom}")
        return 0

    deadline = time.time() + args.timeout
    exported = None
    while time.time() < deadline:
        exported = newest_line_export(watch_dir, before, started - 1.0)
        if exported:
            break
        time.sleep(1.0)

    if not exported:
        payload = {
            "ok": False,
            "error": {
                "code": "desktop_save_timeout",
                "message": f"No new [LINE]*.txt appeared in {watch_dir} within {args.timeout:.0f}s.",
            },
            "watch_dir": str(watch_dir),
        }
        if args.json:
            print_json(payload)
        else:
            print(payload["error"]["message"], file=sys.stderr)
        return 2

    if args.import_after:
        conn = connect(args.db)
        with conn:
            result = import_file(conn, exported, force=args.force)
        payload = {"ok": True, "exported": str(exported), "import": result}
    else:
        payload = {"ok": True, "exported": str(exported)}

    if args.json:
        print_json(payload)
    else:
        print(f"exported: {exported}")
        if args.import_after:
            print_table([payload["import"]], ["status", "messages", "chat", "path"])
    return 0


def cmd_chats(args):
    conn = connect(args.db)
    rows = conn.execute(
        """
        select c.name as chat, count(m.id) as messages,
               min(m.created_at) as first_message, max(m.created_at) as last_message
        from chats c
        left join messages m on m.chat_id=c.id
        group by c.id
        order by last_message desc nulls last, c.name
        """
    ).fetchall()
    if args.json:
        print_json({"ok": True, "chats": [dict(r) for r in rows]})
    else:
        print_table(rows, ["chat", "messages", "first_message", "last_message"])
    return 0


def chat_clause(args, params):
    if not args.chat:
        return ""
    params.append(args.chat)
    return " and c.name like ?"


def cmd_search(args):
    conn = connect(args.db)
    params = [args.query]
    extra = chat_clause(args, params)
    params.append(args.limit)
    rows = []
    try:
        rows = conn.execute(
            f"""
            select c.name as chat, m.created_at, m.sender_name, snippet(message_fts, 0, '[', ']', '...', 12) as snippet
            from message_fts
            join messages m on m.rowid=message_fts.rowid
            join chats c on c.id=m.chat_id
            where message_fts match ? {extra}
            order by bm25(message_fts), m.created_at desc
            limit ?
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    if not rows:
        like_params = [f"%{args.query}%"]
        like_extra = chat_clause(args, like_params)
        like_params.append(args.limit)
        rows = conn.execute(
            f"""
            select c.name as chat, m.created_at, m.sender_name,
                   replace(substr(m.content, 1, 180), char(10), ' ') as snippet
            from messages m
            join chats c on c.id=m.chat_id
            where (m.content like ? or m.sender_name like ?) {like_extra}
            order by m.created_at desc, m.id desc
            limit ?
            """,
            [like_params[0], like_params[0], *like_params[1:]],
        ).fetchall()
    if args.json:
        print_json({"ok": True, "results": [dict(r) for r in rows]})
    else:
        print_table(rows, ["chat", "created_at", "sender_name", "snippet"])
    return 0


def cmd_messages(args):
    conn = connect(args.db)
    params = []
    where = ["1=1"]
    if args.chat:
        where.append("c.name like ?")
        params.append(args.chat)
    if args.days:
        since = dt.datetime.now() - dt.timedelta(days=args.days)
        where.append("m.created_at >= ?")
        params.append(since.strftime("%Y-%m-%dT%H:%M:%S"))
    params.append(args.limit)
    rows = conn.execute(
        f"""
        select m.id as message_id, c.name as chat, m.created_at, m.sender_name, m.content
        from messages m
        join chats c on c.id=m.chat_id
        where {' and '.join(where)}
        order by m.created_at desc, m.id desc
        limit ?
        """,
        params,
    ).fetchall()
    messages = [dict(r) for r in rows]
    media_map = attach_media(conn, [m["message_id"] for m in messages])
    for message in messages:
        message["media"] = media_map.get(message["message_id"], [])
    if args.json:
        print_json({"ok": True, "messages": messages})
    else:
        for message in messages:
            print(f"[{message['created_at']}] {message['chat']} / {message['sender_name']}")
            print(message["content"])
            for media in message["media"]:
                print(f"  media: {media['path']}")
            print()
    return 0


def attach_media(conn, message_ids):
    media_map = {}
    if not message_ids:
        return media_map
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"""
        select message_id, path, width, height, bytes, content_type, quality
        from media
        where message_id in ({placeholders})
        order by captured_at, id
        """,
        message_ids,
    ).fetchall()
    for row in rows:
        media_map.setdefault(row["message_id"], []).append(
            {
                "path": row["path"],
                "width": row["width"],
                "height": row["height"],
                "bytes": row["bytes"],
                "content_type": row["content_type"],
                "quality": row["quality"],
            }
        )
    return media_map


def cmd_media(args):
    conn = connect(args.db)
    params = []
    where = ["1=1"]
    if args.chat:
        where.append("c.name like ?")
        params.append(args.chat)
    if args.days:
        since = dt.datetime.now() - dt.timedelta(days=args.days)
        where.append("coalesce(m.created_at, md.captured_at) >= ?")
        params.append(since.strftime("%Y-%m-%dT%H:%M:%S"))
    params.append(args.limit)
    rows = conn.execute(
        f"""
        select c.name as chat, coalesce(m.created_at, md.captured_at) as created_at,
               m.sender_name, md.path, md.width, md.height, md.bytes, md.quality, md.captured_at
        from media md
        join chats c on c.id=md.chat_id
        left join messages m on m.id=md.message_id
        where {' and '.join(where)}
        order by created_at desc, md.id desc
        limit ?
        """,
        params,
    ).fetchall()
    if args.json:
        print_json({"ok": True, "media": [dict(r) for r in rows]})
    else:
        print_table(rows, ["chat", "created_at", "sender_name", "quality", "path"])
    return 0


def cmd_sql(args):
    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}. Import some chats first.")
    conn = connect_sqlite_readonly(args.db)
    cur = conn.execute(args.query)
    rows = cur.fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    elif rows:
        print_table(rows, rows[0].keys())
    return 0


def process_running(pattern):
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    return result.returncode == 0


def launchd_loaded(label):
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def cmd_doctor(args):
    conn = connect(args.db)
    db_exists = args.db.exists()
    writable = os.access(args.db.parent if args.db.parent.exists() else args.db.parent.parent, os.W_OK)
    media_root = media_root_for_db(args.db)
    media_paths = [row["path"] for row in conn.execute("select path from media")]
    counts = {
        "ok": True,
        "db": str(args.db),
        "db_exists": db_exists,
        "db_parent_writable": writable,
        "auth_required": False,
        "auth_source": "not_required",
        "offline_mode": True,
        "chats": conn.execute("select count(*) from chats").fetchone()[0],
        "members": conn.execute("select count(*) from members").fetchone()[0],
        "messages": conn.execute("select count(*) from messages").fetchone()[0],
        "sources": conn.execute("select count(*) from source_files").fetchone()[0],
        "latest": conn.execute("select coalesce(max(created_at), '') from messages").fetchone()[0],
        "media": len(media_paths),
        "media_full": conn.execute("select count(*) from media where quality='full'").fetchone()[0],
        "media_dir": str(media_root),
        "media_latest_captured": conn.execute("select coalesce(max(captured_at), '') from media").fetchone()[0],
        "media_files_missing": sum(1 for p in media_paths if not Path(p).exists()),
        "web_watch_running": process_running("linecrawl(\\.py)? .*web-watch-current"),
        "downloads_watch_launchd_loaded": launchd_loaded(DEFAULT_LABEL),
        "web_watch_launchd_loaded": launchd_loaded(DEFAULT_WEB_LABEL),
    }
    if args.json:
        print_json(counts)
    else:
        for key, value in counts.items():
            print(f"{key}: {value}")
    return 0


def cmd_stats(args):
    conn = connect(args.db)
    stats = {
        "db": str(args.db),
        "chats": conn.execute("select count(*) from chats").fetchone()[0],
        "members": conn.execute("select count(*) from members").fetchone()[0],
        "messages": conn.execute("select count(*) from messages").fetchone()[0],
        "sources": conn.execute("select count(*) from source_files").fetchone()[0],
        "first_message": conn.execute("select coalesce(min(created_at), '') from messages").fetchone()[0],
        "latest_message": conn.execute("select coalesce(max(created_at), '') from messages").fetchone()[0],
    }
    if args.json:
        print_json({"ok": True, "stats": stats})
    else:
        for key, value in stats.items():
            print(f"{key}: {value}")
    return 0


def cmd_watch(args):
    conn = connect(args.db)
    print(f"Watching {args.downloads} for [LINE]*.txt", flush=True)
    seen = {}
    while True:
        paths = sorted(args.downloads.glob("[[]LINE[]]*.txt"))
        with conn:
            for path in paths:
                sig = (path.stat().st_size, path.stat().st_mtime)
                if seen.get(path) == sig:
                    continue
                result = import_file(conn, path)
                seen[path] = sig
                if result["status"] != "unchanged" or args.verbose:
                    print(f"{result['status']}: {result['chat']} ({result['messages']} messages)", flush=True)
        time.sleep(args.interval)


def launchd_paths(args):
    label = args.label
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    log_dir = Path.home() / ".linecrawl" / "logs"
    return label, plist_path, log_dir


def cmd_launchd_install(args):
    label, plist_path, log_dir = launchd_paths(args)
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()
    program_args = [
        sys.executable,
        str(script_path),
        "--db",
        str(args.db),
        "watch",
        "--downloads",
        str(args.downloads.expanduser()),
        "--interval",
        str(args.interval),
    ]
    if args.verbose:
        program_args.append("--verbose")

    plist = {
        "Label": label,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "watch.out.log"),
        "StandardErrorPath": str(log_dir / "watch.err.log"),
        "WorkingDirectory": str(script_path.parent),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"),
        },
    }
    with plist_path.open("wb") as f:
        plistlib.dump(plist, f)

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)
    print(f"installed: {plist_path}")
    print(f"logs: {log_dir}")
    return 0


def cmd_launchd_install_web(args):
    label, plist_path, log_dir = launchd_paths(args)
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()
    program_args = [
        sys.executable,
        str(script_path),
        "--db",
        str(args.db),
        "web-watch-current",
        "--interval",
        str(args.interval),
        "--scroll-steps",
        str(args.scroll_steps),
        "--method",
        args.method,
        "--chrome-debug-url",
        args.chrome_debug_url,
    ]
    if not args.with_media:
        program_args.append("--no-media")
    if args.full_media:
        program_args.append("--full-media")

    plist = {
        "Label": label,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "webwatch.out.log"),
        "StandardErrorPath": str(log_dir / "webwatch.err.log"),
        "WorkingDirectory": str(script_path.parent),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"),
        },
    }
    with plist_path.open("wb") as f:
        plistlib.dump(plist, f)

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)
    print(f"installed: {plist_path}")
    print(f"logs: {log_dir}")
    return 0


def cmd_launchd_status(args):
    label, plist_path, log_dir = launchd_paths(args)
    domain_label = f"gui/{os.getuid()}/{label}"
    result = subprocess.run(["launchctl", "print", domain_label], text=True, capture_output=True)
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(result.stderr.strip() or result.stdout.strip())
        print(f"plist: {plist_path}")
        return result.returncode
    print(f"plist: {plist_path}")
    print(f"stdout: {log_dir / 'watch.out.log'}")
    print(f"stderr: {log_dir / 'watch.err.log'}")
    return 0


def cmd_launchd_uninstall(args):
    label, plist_path, _log_dir = launchd_paths(args)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if plist_path.exists():
        plist_path.unlink()
    print(f"uninstalled: {label}")
    return 0


def file_entropy(path):
    import math
    from collections import Counter

    data = path.read_bytes()
    if not data:
        return 0.0
    counts = Counter(data)
    entropy = 0.0
    for count in counts.values():
        p = count / len(data)
        entropy -= p * math.log2(p)
    return entropy


def line_db_dir(line_data):
    line_data = line_data.expanduser()
    nested = line_data / "db"
    if nested.exists():
        return nested
    return line_data


def edb_family_paths(path):
    path = path.expanduser()
    paths = [path]
    for suffix in ("-wal", "-shm"):
        companion = path.with_name(path.name + suffix)
        if companion.exists():
            paths.append(companion)
    return paths


def edb_family_stat(paths):
    stats = [p.stat() for p in paths if p.exists()]
    return {
        "size": sum(s.st_size for s in stats),
        "mtime": max((s.st_mtime for s in stats), default=0.0),
    }


def edb_family_sha256(paths):
    h = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        h.update(path.name.encode("utf-8"))
        h.update(b"\0")
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()


def snapshot_edb_family(edb_path, snapshot_root):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_dir = snapshot_root.expanduser() / f"{stamp}-{edb_path.stem}"
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    copied = []
    for source in edb_family_paths(edb_path):
        target = snapshot_dir / source.name
        shutil.copy2(source, target)
        copied.append(target)
    return snapshot_dir, copied[0]


def connect_sqlite_readonly(path):
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("select name from sqlite_master limit 1").fetchall()
    return conn


def pick_column(columns, names):
    by_lower = {c.lower(): c for c in columns}
    for name in names:
        hit = by_lower.get(name.lower())
        if hit:
            return hit
    return None


def quote_sql_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def coerce_edb_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000_000:
            number /= 1_000_000
        elif number > 10_000_000_000:
            number /= 1_000
        try:
            return dt.datetime.fromtimestamp(number).strftime("%Y-%m-%dT%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return coerce_edb_timestamp(int(text))

    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, normalized.replace(" ", "T", 1)):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            return parsed.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    return None


def extract_message_tables_from_sqlite(path):
    conn = connect_sqlite_readonly(path)
    try:
        table_rows = conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        ).fetchall()
        candidates = []
        for table_row in table_rows:
            table = table_row["name"]
            table_sql = quote_sql_identifier(table)
            cols = [row["name"] for row in conn.execute(f"pragma table_info({table_sql})")]
            chat_col = pick_column(cols, ("chat_name", "chat", "room_name", "chat_title", "thread_name"))
            sender_col = pick_column(cols, ("sender_name", "sender", "from_name", "author", "display_name"))
            content_col = pick_column(cols, ("content", "text", "message", "body", "plain_text"))
            created_col = pick_column(cols, ("created_at", "timestamp", "created_time", "createdtime", "sent_at", "time"))
            date_col = pick_column(cols, ("local_date", "date"))
            time_col = pick_column(cols, ("local_time", "localtime"))
            id_col = pick_column(cols, ("id", "message_id", "messageid", "server_id", "local_id"))
            if not content_col or not (created_col or (date_col and time_col)):
                continue

            order_col = created_col or date_col
            quoted_cols = ", ".join(quote_sql_identifier(c) for c in cols)
            rows = conn.execute(
                f"select {quoted_cols} from {table_sql} order by {quote_sql_identifier(order_col)}"
            ).fetchall()
            messages = []
            for index, row in enumerate(rows, start=1):
                if created_col:
                    created_at = coerce_edb_timestamp(row[created_col])
                else:
                    created_at = coerce_edb_timestamp(f"{row[date_col]} {row[time_col]}")
                content = str(row[content_col] or "")
                if not created_at or not content:
                    continue
                messages.append(
                    {
                        "chat_name": str(row[chat_col]) if chat_col and row[chat_col] is not None else path.stem,
                        "sender_name": str(row[sender_col]) if sender_col and row[sender_col] is not None else "(unknown)",
                        "created_at": created_at,
                        "local_date": created_at[:10],
                        "local_time": created_at[11:16],
                        "content": content,
                        "source_line": index,
                        "internal_id": str(row[id_col]) if id_col and row[id_col] is not None else None,
                        "raw": {c: row[c] for c in cols},
                    }
                )
            if messages:
                candidates.append({"table": table, "messages": messages})
        return candidates
    finally:
        conn.close()


def cmd_edb_doctor(args):
    data_dir = args.line_data.expanduser()
    db_dir = line_db_dir(data_dir)
    if not args.json:
        print(f"line_data: {data_dir}")
        print(f"db_dir: {db_dir}")
    if not db_dir.exists():
        if args.json:
            print_json({"ok": False, "status": "db_directory_not_found", "line_data": str(data_dir), "db_dir": str(db_dir)})
        else:
            print("status: db directory not found")
        return 1

    edbs = sorted(db_dir.glob("*.edb"))
    if not edbs:
        if args.json:
            print_json({"ok": False, "status": "no_edb_files_found", "line_data": str(data_dir), "db_dir": str(db_dir)})
        else:
            print("status: no .edb files found")
        return 1

    rows = []
    for path in edbs:
        data = path.read_bytes()
        rows.append(
            {
                "file": path.name,
                "size": path.stat().st_size,
                "entropy": f"{file_entropy(path):.4f}",
                "sqlite_header": "yes" if data.startswith(b"SQLite format 3") else "no",
                "wal": "yes" if path.with_name(path.name + "-wal").exists() else "no",
            }
        )
    if args.json:
        print_json({"ok": True, "line_data": str(data_dir), "db_dir": str(db_dir), "files": rows})
    else:
        print_table(rows, ["file", "size", "entropy", "sqlite_header", "wal"])
        print()
        print("interpretation:")
        print("- sqlite_header=no and entropy close to 8.0 means the file is encrypted or wrapped.")
        print("- Phase 1 import remains the reliable route until the LINE StorageService key path is decoded.")
    return 0


def cmd_edb_import(args):
    if args.paths:
        candidates = [Path(p).expanduser() for p in args.paths]
    else:
        db_dir = line_db_dir(args.line_data)
        candidates = sorted(db_dir.glob("*.edb"), key=lambda p: p.stat().st_size, reverse=True)

    if not candidates:
        if args.json:
            print_json({"ok": False, "error": {"code": "no_edb_files_matched", "message": "No .edb files matched."}})
        else:
            print("No .edb files matched.", file=sys.stderr)
        return 1

    results = []
    ready_imports = []
    for edb_path in candidates:
        if not edb_path.exists():
            results.append({"status": "missing", "messages": 0, "path": str(edb_path)})
            continue

        snapshot_dir, snapshot_path = snapshot_edb_family(edb_path, args.snapshot_root)
        family = edb_family_paths(snapshot_path)
        try:
            tables = extract_message_tables_from_sqlite(snapshot_path)
        except sqlite3.DatabaseError as exc:
            data = snapshot_path.read_bytes()[:32]
            sqlite_header = "yes" if data.startswith(b"SQLite format 3") else "no"
            entropy = file_entropy(snapshot_path)
            results.append(
                {
                    "status": "unsupported-encrypted-or-wrapped",
                    "messages": 0,
                    "path": str(edb_path),
                    "detail": f"snapshot={snapshot_dir}; sqlite_header={sqlite_header}; entropy={entropy:.4f}; {exc}",
                }
            )
            continue

        if not tables:
            results.append(
                {
                    "status": "unsupported-schema",
                    "messages": 0,
                    "path": str(edb_path),
                    "detail": f"snapshot={snapshot_dir}; no table with message/content timestamp columns",
                }
            )
            continue

        source_stat = edb_family_stat(family)
        source_sha = edb_family_sha256(family)
        for table in tables:
            label = f"{edb_path}#{table['table']}"
            source_key = f"edb:{edb_path.resolve()}#{table['table']}"
            table_sha = hashlib.sha256(f"{source_sha}\0{table['table']}".encode("utf-8")).hexdigest()
            ready_imports.append(
                {
                    "source_key": source_key,
                    "source_label": label,
                    "source_stat": source_stat,
                    "source_sha": table_sha,
                    "messages": table["messages"],
                }
            )

    if args.dry_run:
        for item in ready_imports:
            results.append(
                {
                    "status": "ready",
                    "messages": len(item["messages"]),
                    "path": item["source_label"],
                }
            )
    elif ready_imports:
        conn = connect(args.db)
        with conn:
            for item in ready_imports:
                results.append(
                    import_normalized_messages(
                        conn,
                        item["source_key"],
                        item["source_label"],
                        item["source_stat"],
                        item["source_sha"],
                        item["messages"],
                        force=args.force,
                    )
                )

    if args.json:
        print_json({"ok": bool(ready_imports), "results": results})
    else:
        print_table(results, ["status", "messages", "path"])
        detailed = [r for r in results if r.get("detail")]
        if detailed:
            print()
            print("details:")
            for row in detailed:
                print(f"- {row['path']}: {row['detail']}")

    if ready_imports:
        return 0
    return 2


def build_parser():
    parser = argparse.ArgumentParser(prog="linecrawl", description="Local CLI for LINE Save chat exports.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite DB path. Default: {DEFAULT_DB}")
    parser.add_argument("--json", action="store_true", help="Emit stable JSON for commands that support it.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="Import LINE Save chat text files.")
    p.add_argument("paths", nargs="+")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("import-downloads", help="Import ~/Downloads/[LINE]*.txt.")
    p.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOADS)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_import_downloads)

    p = sub.add_parser("desktop-save-current", help="Use LINE Desktop UI to Save chat for the currently open chat.")
    p.add_argument("--watch-dir", type=Path, default=DEFAULT_DOWNLOADS, help="Directory where LINE writes Save chat text files. Default: ~/Downloads.")
    p.add_argument("--timeout", type=float, default=DEFAULT_DESKTOP_SAVE_TIMEOUT)
    p.add_argument("--import", dest="import_after", action="store_true", help="Import the newly saved export into the linecrawl DB.")
    p.add_argument("--force", action="store_true", help="Re-import the saved file even if unchanged.")
    p.add_argument("--dry-run", action="store_true", help="Only print detected window geometry and planned click points.")
    p.add_argument("--menu-already-open", action="store_true", help="Skip opening the chat menu and click only the Save chat menu item.")
    p.add_argument("--pre-click-delay", type=float, default=0.0, help="Wait before clicking, useful when a human opens the LINE menu after starting the command.")
    p.add_argument("--ui-delay", type=float, default=0.35, help="Delay between UI operations.")
    p.add_argument("--menu-right-offset", type=int, default=102, help="Pixels from LINE window right edge to the chat menu button.")
    p.add_argument("--menu-top-offset", type=int, default=75, help="Pixels from LINE window top edge to the chat menu button.")
    p.add_argument("--save-right-offset", type=int, default=95, help="Pixels from LINE window right edge to the Save chat menu item.")
    p.add_argument("--save-top-offset", type=int, default=414, help="Pixels from LINE window top edge to the Save chat menu item.")
    p.set_defaults(func=cmd_desktop_save_current)

    p = sub.add_parser("web-dump-current", help="Dump visible messages from the open LINE Web Chrome tab.")
    p.add_argument("--scroll-steps", type=int, default=0, help="Scroll upward this many viewports while collecting messages.")
    p.add_argument("--method", choices=("auto", "cdp", "applescript", "ax"), default="auto", help="Chrome control method. Default: auto.")
    p.add_argument("--chrome-debug-url", default=DEFAULT_CHROME_DEBUG_URL, help=f"Chrome DevTools URL for --method cdp. Default: {DEFAULT_CHROME_DEBUG_URL}.")
    p.add_argument("--allow-remote-cdp", action="store_true", help="Allow a non-loopback Chrome DevTools URL. Off by default so linecrawl stays local-only.")
    p.add_argument("--output", type=Path, help="Optional JSON output path for the raw DOM dump.")
    p.add_argument("--with-media", action="store_true", help="Fetch visible message images as base64 data URLs.")
    p.add_argument("--full-media", action="store_true", help="Also capture full-resolution images via the in-page viewer (CDP only; briefly opens the photo viewer inside the LINE tab).")
    p.set_defaults(func=cmd_web_dump_current)

    p = sub.add_parser("web-import-current", help="Import messages from the open LINE Web Chrome tab.")
    p.add_argument("--scroll-steps", type=int, default=0, help="Scroll upward this many viewports while collecting messages.")
    p.add_argument("--method", choices=("auto", "cdp", "applescript", "ax"), default="auto", help="Chrome control method. Default: auto.")
    p.add_argument("--chrome-debug-url", default=DEFAULT_CHROME_DEBUG_URL, help=f"Chrome DevTools URL for --method cdp. Default: {DEFAULT_CHROME_DEBUG_URL}.")
    p.add_argument("--allow-remote-cdp", action="store_true", help="Allow a non-loopback Chrome DevTools URL. Off by default so linecrawl stays local-only.")
    p.add_argument("--owner-name", default="Me", help="Sender name to use for outgoing LINE Web messages.")
    p.add_argument("--force", action="store_true", help="Re-import even if the current web dump is unchanged.")
    p.add_argument("--no-media", dest="with_media", action="store_false", help="Skip image capture.")
    p.add_argument("--full-media", action="store_true", help="Also capture full-resolution images via the in-page viewer (CDP only; briefly opens the photo viewer inside the LINE tab).")
    p.set_defaults(func=cmd_web_import_current, with_media=True)

    p = sub.add_parser("web-watch-current", help="Poll the open LINE Web Chrome tab and import changed DOM dumps.")
    p.add_argument("--interval", type=float, default=30.0, help="Seconds between imports. Default: 30.")
    p.add_argument("--scroll-steps", type=int, default=0, help="Scroll upward this many viewports while collecting messages.")
    p.add_argument("--method", choices=("auto", "cdp", "applescript", "ax"), default="auto", help="Chrome control method. Default: auto.")
    p.add_argument("--chrome-debug-url", default=DEFAULT_CHROME_DEBUG_URL, help=f"Chrome DevTools URL for --method cdp. Default: {DEFAULT_CHROME_DEBUG_URL}.")
    p.add_argument("--allow-remote-cdp", action="store_true", help="Allow a non-loopback Chrome DevTools URL. Off by default so linecrawl stays local-only.")
    p.add_argument("--owner-name", default="Me", help="Sender name to use for outgoing LINE Web messages.")
    p.add_argument("--force", action="store_true", help="Re-import even if the current web dump is unchanged.")
    p.add_argument("--verbose", action="store_true", help="Print unchanged imports too.")
    p.add_argument("--fail-fast", action="store_true", help="Exit on the first dump/import error.")
    p.add_argument("--once", action="store_true", help="Run one watch iteration, useful for smoke tests.")
    p.add_argument("--no-media", dest="with_media", action="store_false", help="Skip image capture.")
    p.add_argument("--full-media", action="store_true", help="Also capture full-resolution images via the in-page viewer (CDP only; briefly opens the photo viewer inside the LINE tab).")
    p.set_defaults(func=cmd_web_watch_current, with_media=True)

    p = sub.add_parser("web-import-json", help="Import a saved LINE Web DOM dump JSON file.")
    p.add_argument("path", type=Path)
    p.add_argument("--owner-name", default="Me", help="Sender name to use for outgoing LINE Web messages.")
    p.add_argument("--force", action="store_true", help="Re-import even if the web dump is unchanged.")
    p.set_defaults(func=cmd_web_import_json)

    p = sub.add_parser("web-doctor", help="Check LINE Web Chrome import prerequisites.")
    p.add_argument("--chrome-debug-url", default=DEFAULT_CHROME_DEBUG_URL, help=f"Chrome DevTools URL. Default: {DEFAULT_CHROME_DEBUG_URL}.")
    p.add_argument("--allow-remote-cdp", action="store_true", help="Allow a non-loopback Chrome DevTools URL. Off by default so linecrawl stays local-only.")
    p.add_argument("--chrome-profile-root", default=Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default")
    p.set_defaults(func=cmd_web_doctor)

    p = sub.add_parser("web-dump-js", help="Print the LINE Web DOM extraction JavaScript.")
    p.set_defaults(func=cmd_web_dump_js)

    p = sub.add_parser("chats", help="List imported chats.")
    p.set_defaults(func=cmd_chats)

    p = sub.add_parser("search", help="Full-text search messages.")
    p.add_argument("query")
    p.add_argument("--chat", help="SQL LIKE pattern, e.g. '%Podcast%'.")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("messages", help="Print recent messages.")
    p.add_argument("--chat", help="SQL LIKE pattern.")
    p.add_argument("--days", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("media", help="List captured media files with local paths.")
    p.add_argument("--chat", help="SQL LIKE pattern.")
    p.add_argument("--days", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_media)

    p = sub.add_parser("sql", help="Run a read query against the DB.")
    p.add_argument("query")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("doctor", help="Show DB status.")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("stats", help="Show aggregate database statistics.")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("watch", help="Poll Downloads and import changed LINE exports.")
    p.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOADS)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("launchd-install", help="Install the Downloads watcher as a LaunchAgent.")
    p.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOADS)
    p.add_argument("--interval", type=float, default=10.0)
    p.add_argument("--label", default=DEFAULT_LABEL)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_launchd_install)

    p = sub.add_parser("launchd-install-web", help="Install the LINE Web tab watcher (with image capture) as a LaunchAgent.")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--scroll-steps", type=int, default=0)
    p.add_argument("--method", choices=("auto", "cdp", "applescript", "ax"), default="auto")
    p.add_argument("--chrome-debug-url", default=DEFAULT_CHROME_DEBUG_URL)
    p.add_argument("--label", default=DEFAULT_WEB_LABEL)
    p.add_argument("--no-media", dest="with_media", action="store_false", help="Skip image capture.")
    p.add_argument("--full-media", action="store_true", help="Also capture full-resolution images via the in-page viewer (CDP only; briefly opens the photo viewer inside the LINE tab).")
    p.set_defaults(func=cmd_launchd_install_web, with_media=True)

    p = sub.add_parser("launchd-status", help="Show LaunchAgent status.")
    p.add_argument("--label", default=DEFAULT_LABEL)
    p.set_defaults(func=cmd_launchd_status)

    p = sub.add_parser("launchd-uninstall", help="Remove the LaunchAgent.")
    p.add_argument("--label", default=DEFAULT_LABEL)
    p.set_defaults(func=cmd_launchd_uninstall)

    p = sub.add_parser("edb-doctor", help="Inspect LINE Desktop .edb files without decrypting them.")
    p.add_argument("--line-data", type=Path, default=DEFAULT_LINE_DATA)
    p.set_defaults(func=cmd_edb_doctor)

    p = sub.add_parser("edb-import", help="Snapshot and import readable LINE .edb message stores when supported.")
    p.add_argument("paths", nargs="*", help="Specific .edb files. Defaults to *.edb under --line-data/db.")
    p.add_argument("--line-data", type=Path, default=DEFAULT_LINE_DATA)
    p.add_argument("--snapshot-root", type=Path, default=DEFAULT_EDB_SNAPSHOT_ROOT)
    p.add_argument("--dry-run", action="store_true", help="Probe and report importable tables without writing the linecrawl DB. Note: this still copies the target .edb family into --snapshot-root and reads the snapshot read-only.")
    p.add_argument("--force", action="store_true", help="Re-import even if the source snapshot hash is unchanged.")
    p.set_defaults(func=cmd_edb_import)

    return parser


def main(argv=None):
    global ALLOW_REMOTE_CDP
    parser = build_parser()
    args = parser.parse_args(argv)
    ALLOW_REMOTE_CDP = getattr(args, "allow_remote_cdp", False)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
