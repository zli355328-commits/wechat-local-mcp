"""Read candidate metadata from a public Tonghuashun author index.

This optional index is not a WeChat history API. Dates and identity require
verification against the original article before analysis.
"""
import html
import re
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, urlopen


def download(url):
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as response:
        if urlsplit(response.url).hostname != "t.10jqka.com.cn":
            raise ValueError("Unexpected index redirect")
        data = response.read(4_000_001)
    if len(data) > 4_000_000:
        raise ValueError("Index page exceeds 4 MB")
    return data.decode("utf-8", "replace")


def plain(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def parse_items(document):
    articles = []
    for item in re.split(r'<li class="ucwlist-all', document)[1:]:
        def field(css):
            match = re.search(r'<div class="' + css + r'[^\"]*">(.*?)</div>', item, re.S)
            return plain(match[1]) if match else ""

        link = re.search(r'href="(https://t\.10jqka\.com\.cn/pid_\d+\.shtml)"', item)
        date = re.search(r'class="ucwlist-fd-time[^>]+>(.*?)</a>', item, re.S)
        title = field("ucwlist-fd-title")
        if not link or not title:
            continue
        label = plain(date[1]) if date else ""
        articles.append({
            "title": title,
            "source_url": link[1],
            "date_label": label,
            "published_at": None,
            "year_verified": False,
            "excerpt": field("ucwlist-fd-detail").replace("阅读全文", "").strip()[:180],
            "status": "candidate",
            "wechat_original_url": None,
            "wechat_read_count": None,
            "full_text_verified": False,
        })
    return articles


def collect_candidates(args):
    source = urlsplit(args["author_url"])
    if source.scheme != "https" or source.netloc != "t.10jqka.com.cn":
        raise ValueError("Provide an HTTPS t.10jqka.com.cn author URL")
    match = re.fullmatch(r"/([0-9]{1,20})/?", source.path)
    if not match:
        raise ValueError("Expected a numeric public author URL")
    publisher = args["publisher"].strip()
    pages = args.get("pages", 3)
    if not publisher or type(pages) is not int or not 1 <= pages <= 20:
        raise ValueError("Provide publisher and 1 to 20 pages")
    author = match[1]
    home = download(f"https://t.10jqka.com.cn/{author}/?posttype=post")
    heading = re.search(r"<title>(.*?)</title>", home, re.S)
    if not heading or plain(heading[1]) != publisher:
        raise ValueError("Public index title does not match the requested publisher")
    articles, seen, errors = [], set(), []
    examined = 0
    for number in range(1, pages + 1):
        query = urlencode({"page": number, "subtype": "viewPost", "userid": author})
        try:
            import json
            data = json.loads(download("https://t.10jqka.com.cn/newcircle/user/userPost/?" + query))
            if data.get("errorCode") != 0:
                raise ValueError("Public index returned an error")
            batch = parse_items(data.get("result", {}).get("posthtml", ""))
            examined += 1
            if not batch:
                break
            for article in batch:
                if article["source_url"] not in seen:
                    seen.add(article["source_url"])
                    articles.append(article)
        except (OSError, ValueError, TypeError, AttributeError) as error:
            errors.append({"page": number, "error": str(error)})
            break
    return {
        "publisher": publisher,
        "source_author_url": f"https://t.10jqka.com.cn/{author}/?posttype=post",
        "status": "partial" if errors else "ok",
        "articles": articles,
        "pages_examined": examined,
        "errors": errors,
        "complete_archive": False,
        "wechat_identity_verified": False,
        "coverage": "Public author-index candidates only. Same-name identity, publication year, WeChat original URL and full text must be verified. Missing index entries do not imply no articles. Excerpts are not full text; comment/like counts are not WeChat readership.",
    }
