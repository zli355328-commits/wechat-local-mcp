"""Read article cards from explicitly selected chats and fetch public full text."""

import json
import re
import base64
import binascii
import time
import xml.etree.ElementTree as ET
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from wechat_reader import run

ZONE = timezone(timedelta(hours=8))


def account_home(args):
    biz = args.get("biz", "")
    if not biz and args.get("article_url"):
        parts = urlsplit(args["article_url"])
        if parts.netloc != "mp.weixin.qq.com" or parts.scheme != "https":
            raise ValueError("Expected an HTTPS WeChat article URL")
        biz = parse_qs(parts.query).get("__biz", [""])[0]
    if not biz:
        raise ValueError("This URL has no __biz; obtain it from the original article page")
    try:
        decoded = base64.b64decode(biz, validate=True).decode("ascii")
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("Invalid __biz value") from None
    if not decoded.isdigit():
        raise ValueError("__biz must encode a numeric publisher identity")
    query = urlencode({"action": "home", "__biz": biz, "scene": "124"})
    return {"biz": biz,
            "home_url": "https://mp.weixin.qq.com/mp/profile_ext?" + query + "#wechat_redirect",
            "access_note": "Link generation only. Historical article requests may require a WeChat session; a home URL does not supply credentials."}


def stamp(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZONE).timestamp())


def clean_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or parts.netloc != "mp.weixin.qq.com":
        raise ValueError("Only public WeChat article URLs are accepted")
    if parts.path != "/s" and not parts.path.startswith("/s/"):
        raise ValueError("Expected a public article URL")
    # Signed search links may need their full query to remain accessible.
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    fields = dict(pairs)
    canonical = {"__biz", "mid", "idx", "sn"}
    query = urlencode([(k, v) for k, v in pairs if k in canonical]) if all(fields.get(k) for k in canonical) else parts.query
    return urlunsplit(("https", parts.hostname, parts.path, query, ""))


def cards(content, shared_at):
    if not isinstance(content, str):
        return
    start = content.find("<")
    if start < 0:
        return
    try:
        root = ET.fromstring(content[start:])
    except ET.ParseError:
        return
    nodes = list(root.iter("appmsg")) + list(root.iter("item"))
    for node in nodes:
        url = node.findtext("url") or node.findtext("digest_url")
        if not url:
            continue
        try:
            url = clean_url(url)
        except ValueError:
            continue
        published = node.findtext("pub_time")
        yield {
            "title": node.findtext("title") or "",
            "publisher": (node.findtext("sourcedisplayname") or
                          node.findtext("source_name") or root.findtext(".//nickname") or ""),
            "url": url,
            "shared_at": shared_at,
            "published_at": int(published) if published and published.isdigit() else None,
        }


def search(storage, capture, args):
    start, end = stamp(args["start_date"]), stamp(args["end_date"])
    if start >= end:
        raise ValueError("end_date must be after start_date, exclusive, Beijing time")
    chats = args["chat_ids"]
    if not isinstance(chats, list) or not 1 <= len(chats) <= 10 or not all(
            isinstance(c, str) and c.strip() for c in chats):
        raise ValueError("Provide 1 to 10 explicitly selected conversation IDs")
    publisher = args["publisher"].strip()
    if not publisher:
        raise ValueError("publisher is required")
    basis = args.get("date_basis", "shared")
    if basis not in ("shared", "published"):
        raise ValueError("date_basis must be shared or published")
    result, seen = [], set()
    excluded = 0
    for chat in dict.fromkeys(chats):
        # Published-date filtering requires scanning all locally retained cards.
        windows = [(start, end)] if basis == "shared" else [(0, int(time.time()) + 1)]
        while windows:
            low, high = windows.pop()
            rows = run(Namespace(storage=storage, capture=capture, chat=chat,
                                 scratch=None, limit=10000, since=low, until=high))
            if len(rows) == 10000:
                if high - low <= 1:
                    raise ValueError("Too many messages in one second; export would be incomplete")
                middle = (low + high) // 2
                windows.extend([(low, middle), (middle, high)])
                continue
            for row in rows:
                for card in cards(row.get("content"), row["timestamp"]):
                    if card["publisher"] != publisher:
                        continue
                    if args.get("title", "").casefold() not in card["title"].casefold():
                        continue
                    date = card["shared_at"] if basis == "shared" else card["published_at"]
                    if date is None:
                        excluded += 1
                        continue
                    if not low <= row["timestamp"] < high or not start <= date < end:
                        continue
                    parts = urlsplit(card["url"])
                    fields = dict(parse_qsl(parts.query))
                    identity = tuple(fields.get(k) for k in ("__biz", "mid", "idx"))
                    key = identity if all(identity) else card["url"]
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append(card)
    result.sort(key=lambda c: c["shared_at"] if basis == "shared" else c["published_at"])
    return {"articles": result, "date_basis": basis,
            "missing_publication_date": excluded,
            "coverage": "Only cards retained in the explicitly selected local chats; not a complete publisher archive"}


class BodyParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.active = False
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if not self.active and dict(attrs).get("id") == "js_content":
            self.active, self.depth = True, 1
        elif self.active and tag not in {"br", "img", "hr", "input", "meta", "link", "wbr"}:
            self.depth += 1
        if self.active and tag in {"script", "style"}:
            self.skip += 1
        if self.active and tag in {"p", "section", "div", "br", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"br", "img", "hr", "input", "meta", "link", "wbr"}:
            return
        if self.active:
            if tag in {"script", "style"}:
                self.skip = max(0, self.skip - 1)
            self.depth -= 1
            if self.depth <= 0:
                self.active = False

    def handle_data(self, data):
        if self.active and not self.skip:
            self.parts.append(data)


def fetch_batch(urls):
    if not isinstance(urls, list) or not 1 <= len(urls) <= 20:
        raise ValueError("Provide 1 to 20 public article URLs")
    results = []
    for raw in dict.fromkeys(urls):
        url = raw
        try:
            url = clean_url(raw)
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=20) as response:
                clean_url(response.url)
                data = response.read(8_000_001)
            if len(data) > 8_000_000:
                raise ValueError("Article exceeds 8 MB")
            document = data.decode("utf-8", errors="replace")
            parser = BodyParser()
            parser.feed(document)
            body = "\n".join(line.strip() for line in "".join(parser.parts).splitlines()
                             if line.strip())
            if not body:
                body = embedded_body(document)
            results.append({"url": url, "status": "extracted" if body else "blocked",
                            "text": body, "full_text_verified": False})
        except (OSError, ValueError) as exc:
            results.append({"url": url, "status": "error", "error": str(exc)})
    return results


def embedded_body(document):
    """Decode a literal string used by newer WeChat pages, without running JS."""
    found = re.search(
        r"""content_noencode\s*:\s*(?P<q>['"])(?P<body>(?:\\.|(?!(?P=q)).)*)(?P=q)""",
        document, re.S,
    )
    if not found:
        return ""
    escapes = {"n": "\n", "r": "\r", "t": "\t", "'": "'", '"': '"', "\\": "\\"}

    def decode(match):
        token = match[1]
        if token.startswith(("u", "x")):
            return chr(int(token[1:], 16))
        return escapes.get(token, "\\" + token)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)",
                  decode, found["body"]).strip()
