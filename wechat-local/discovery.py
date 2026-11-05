"""Headless public WeChat search. Search coverage is explicitly incomplete."""

import html
import json
import re
import subprocess
import time
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, quote, urlsplit


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def text(fragment):
    parser = PlainText()
    parser.feed(fragment)
    return "".join(parser.parts).strip()


def download(url):
    url = quote(url, safe=":/?&=%+;,@!$'()*-._~")
    response = subprocess.run(
        ["curl.exe", "--fail", "--location", "--max-time", "25",
         "--silent", "--show-error", "--max-filesize", "8000000", url],
        capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if response.returncode:
        raise OSError("Public search request failed, curl code " + str(response.returncode))
    return response.stdout.decode("utf-8", errors="replace")


def parse_page(page):
    if re.search(r"antispider|请输入验证码|访问过于频繁", page):
        return {"status": "verification_required", "candidates": []}
    candidates = []
    for item in re.findall(r"<li\b[^>]*>(.*?)</li>", page, re.S):
        heading = re.search(r"<h3\b[^>]*>(.*?)</h3>", item, re.S)
        if not heading:
            continue
        anchor = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                           heading[1], re.S)
        if not anchor:
            continue
        source = re.search(r'<span\b[^>]*class="all-time-y2"[^>]*>(.*?)</span>',
                           item, re.S)
        timestamp = re.search(r"timeConvert\(['\"](\d+)['\"]\)", item)
        candidates.append({
            "title": text(anchor[2]),
            "publisher": text(source[1]) if source else "",
            "published_at": int(timestamp[1]) if timestamp else None,
            "search_link": urljoin("https://weixin.sogou.com", html.unescape(anchor[1])),
            "status": "candidate",
        })
    if candidates:
        return {"status": "ok", "candidates": candidates}
    if "暂无" in page:
        return {"status": "no_results", "candidates": []}
    return {"status": "unrecognized_response", "candidates": []}


def resolve_candidate(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "weixin.sogou.com" or parts.path != "/link":
        raise ValueError("Expected a search result link from weixin.sogou.com/link")
    page = download(url)
    if re.search(r"antispider|请输入验证码|此验证码用于确认", page):
        return {"status": "verification_required", "original_url": None}
    matches = re.findall(r"https://mp\.weixin\.qq\.com/s[^\s<>\"']+", html.unescape(page))
    for candidate in matches:
        if urlsplit(candidate).hostname == "mp.weixin.qq.com":
            return {"status": "resolved", "original_url": candidate}
    return {"status": "unresolved", "original_url": None}


def discover(args):
    publisher = args["publisher"].strip()
    if not publisher:
        raise ValueError("publisher cannot be empty")
    zone = timezone(timedelta(hours=8))
    start = datetime.strptime(args["start_date"], "%Y-%m-%d").replace(tzinfo=zone)
    end = datetime.strptime(args["end_date"], "%Y-%m-%d").replace(tzinfo=zone)
    if start >= end:
        raise ValueError("end_date must be later than start_date (exclusive)")
    pages = args.get("pages", 2)
    if type(pages) is not int or not 1 <= pages <= 5:
        raise ValueError("pages must be 1 to 5")
    result, seen = [], set()
    status = "ok"
    for page in range(1, pages + 1):
        query = {"type": 2, "query": publisher + " " + args.get("title", ""),
                 "page": page, "tsn": 5, "ft": start.strftime("%Y-%m-%d"),
                 "et": (end - timedelta(days=1)).strftime("%Y-%m-%d")}
        parsed = parse_page(download("https://weixin.sogou.com/weixin?" + urlencode(query)))
        if parsed["status"] == "unrecognized_response":
            # Some deployments reject advanced date parameters. Filter the
            # returned publication timestamps locally instead.
            basic = {key: query[key] for key in ("type", "query", "page")}
            parsed = parse_page(download("https://weixin.sogou.com/weixin?" + urlencode(basic)))
        status = parsed["status"]
        if status != "ok":
            break
        for item in parsed["candidates"]:
            date = item["published_at"]
            if item["publisher"] != publisher or date is None:
                continue
            if not start.timestamp() <= date < end.timestamp():
                continue
            if args.get("title", "").casefold() not in item["title"].casefold():
                continue
            if item["search_link"] not in seen:
                result.append(item)
                seen.add(item["search_link"])
        if page < pages:
            time.sleep(2)
    return {"publisher": publisher, "status": status, "articles": result,
            "complete_archive": False,
            "coverage": "Publicly indexed WeChat originals only. Missing results do not imply no articles. Search links still require resolution and publisher/date/full-text verification."}


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("publisher")
    parser.add_argument("start_date")
    parser.add_argument("end_date")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = discover(vars(args))
    with open(args.output, "x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"status": result["status"], "count": len(result["articles"]),
                      "complete_archive": False}))
