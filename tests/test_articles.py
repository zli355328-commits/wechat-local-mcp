import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wechat-local"))
import public_articles as articles
import public_index as index
import discovery
import server


def listing(title="Example", pid=123):
    return f'''<li class="ucwlist-all post-single"><div class="ucwlist-fd-title fwb"><a href="https://t.10jqka.com.cn/pid_{pid}.shtml">{title}</a></div><div class="ucwlist-fd-detail">Opening only...<a>阅读全文</a></div><a class="ucwlist-fd-time fr">07-02</a></li>'''


class Links(unittest.TestCase):
    def test_signed_query_survives(self):
        url = "https://mp.weixin.qq.com/s?src=11&signature=a*b&timestamp=123&new=1"
        self.assertEqual(articles.clean_url(url), url)

    def test_canonical_tracking_removed(self):
        result = articles.clean_url("https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1&sn=def&scene=123#rd")
        self.assertNotIn("scene", result)
        self.assertIn("sn=def", result)

    def test_short_article(self):
        self.assertEqual(articles.clean_url("https://mp.weixin.qq.com/s/token#rd"), "https://mp.weixin.qq.com/s/token")

    def test_untrusted_hosts(self):
        for url in ["https://example.com/s/a", "https://mp.weixin.qq.com.evil/s/a", "https://x@mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com:123/s/a", "https://mp.weixin.qq.com/mp/profile_ext"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                articles.clean_url(url)

    def test_home_numeric_biz(self):
        result = articles.account_home({"biz": "MTIzNDU="})
        self.assertIn("profile_ext", result["home_url"])
        self.assertIn("does not supply", result["access_note"])

    def test_home_rejects_invalid(self):
        for value in ["bad", "YWJj"]:
            with self.assertRaises(ValueError):
                articles.account_home({"biz": value})


class Bodies(unittest.TestCase):
    def test_nested_and_void_elements(self):
        p = articles.BodyParser()
        p.feed('<div id="js_content"><section><p>First<img src="a"/><br></br>Second</p><script>secret()</script><p>Last</p></section></div><p>Outside</p>')
        text = "".join(p.parts)
        for word in ["First", "Second", "Last"]:
            self.assertIn(word, text)
        self.assertNotIn("secret", text)
        self.assertNotIn("Outside", text)

    def test_literal_decoder_no_evaluation(self):
        self.assertEqual(articles.embedded_body(r'''var info={content_noencode:'a\n\u4e2d\x21'}'''), "a\n中!")
        self.assertEqual(articles.embedded_body("content_noencode: do_something()"), "")

    def test_empty_body_blocked(self):
        class Response(io.BytesIO):
            url = "https://mp.weixin.qq.com/s/demo"
        with patch.object(articles, "urlopen", return_value=Response(b'<html>Verification required</html>')):
            result = articles.fetch_batch([Response.url])[0]
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["full_text_verified"])

    def test_batch_failure_does_not_abort(self):
        class Response(io.BytesIO):
            url = "https://mp.weixin.qq.com/s/ok"
        with patch.object(articles, "urlopen", return_value=Response(b'<div id="js_content">Synthetic body</div>')):
            results = articles.fetch_batch(["https://example.com/s/no", Response.url])
        self.assertEqual([a["status"] for a in results], ["error", "extracted"])
        self.assertFalse(results[1]["full_text_verified"])

    def test_batch_limit(self):
        for values in [[], ["x"] * 21, "not a list"]:
            with self.assertRaises(ValueError):
                articles.fetch_batch(values)


class LocalCards(unittest.TestCase):
    def test_xml_card_publisher_and_date(self):
        xml = '<msg><appmsg><title>Test &amp; example</title><url>https://mp.weixin.qq.com/s/demo</url><sourcedisplayname>Example</sourcedisplayname><pub_time>123</pub_time></appmsg></msg>'
        card = list(articles.cards(xml, 456))[0]
        self.assertEqual((card["publisher"], card["published_at"], card["shared_at"]), ("Example", 123, 456))

    def test_published_missing_excluded_and_duplicate_removed(self):
        day = articles.stamp("2026-07-02")
        def card(pub):
            return f'<msg><appmsg><title>Test</title><url>https://mp.weixin.qq.com/s/demo</url><sourcedisplayname>Example</sourcedisplayname>{pub}</appmsg></msg>'
        rows = [{"timestamp": day, "content": card(f'<pub_time>{day}</pub_time>')}, {"timestamp": day, "content": card('')}]
        with patch.object(articles, "run", return_value=rows):
            result = articles.search(None, None, {"publisher":"Example", "chat_ids":["chat-a", "chat-b"], "start_date":"2026-07-01", "end_date":"2026-07-03", "date_basis":"published"})
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["missing_publication_date"], 2)


class PublicIndex(unittest.TestCase):
    def test_metadata_never_claims_full_text_or_year(self):
        item = index.parse_items(listing())[0]
        self.assertIsNone(item["published_at"])
        self.assertFalse(item["year_verified"])
        self.assertFalse(item["full_text_verified"])
        self.assertIsNone(item["wechat_read_count"])
        self.assertNotIn("阅读全文", item["excerpt"])

    def test_dedup_and_empty_last_page(self):
        page = json.dumps({"errorCode":0, "result":{"posthtml":listing() + listing()}})
        empty = json.dumps({"errorCode":0, "result":{"posthtml":""}})
        with patch.object(index, "download", side_effect=['<title>Example</title>', page, empty]):
            result = index.collect_candidates({"publisher":"Example", "author_url":"https://t.10jqka.com.cn/123/", "pages":3})
        self.assertEqual(len(result["articles"]), 1)
        self.assertFalse(result["complete_archive"])
        self.assertFalse(result["wechat_identity_verified"])

    def test_partial_failure_preserves_candidates(self):
        page = json.dumps({"errorCode":0, "result":{"posthtml":listing()}})
        with patch.object(index, "download", side_effect=['<title>Example</title>', page, OSError('unavailable')]):
            result = index.collect_candidates({"publisher":"Example", "author_url":"https://t.10jqka.com.cn/123/"})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["articles"]), 1)

    def test_publisher_mismatch(self):
        with patch.object(index, "download", return_value='<title>Another author</title>'), self.assertRaises(ValueError):
            index.collect_candidates({"publisher":"Example", "author_url":"https://t.10jqka.com.cn/123/"})

    def test_rejects_arbitrary_endpoints(self):
        for url in ['https://example.com/123/', 'http://t.10jqka.com.cn/123/', 'https://t.10jqka.com.cn/newcircle/user/', 'https://x@t.10jqka.com.cn/123/']:
            with self.assertRaises(ValueError):
                index.collect_candidates({"publisher":"Example", "author_url":url})


class Discovery(unittest.TestCase):
    def test_verification_reported(self):
        self.assertEqual(discovery.parse_page('<html>请输入验证码</html>')["status"], "verification_required")

    def test_resolver_validates_host_before_request(self):
        with self.assertRaises(ValueError):
            discovery.resolve_candidate('https://example.com/link')

    def test_discovery_empty_does_not_claim_complete(self):
        with patch.object(discovery, "download", return_value='暂无结果'):
            result = discovery.discover({"publisher":"Example", "start_date":"2026-07-01", "end_date":"2026-08-01", "pages":1})
        self.assertFalse(result["complete_archive"])


class Server(unittest.TestCase):
    def test_public_tool_without_capture(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("home_url", server.call_tool('get_public_account_home', {"biz":"MTIzNDU="}))

    def test_local_card_route_uses_configured_capture(self):
        capture = Path('synthetic-capture.json')
        with patch.dict(os.environ, {"WECHAT_CAPTURE_FILE":str(capture)}), patch.object(Path, 'is_file', return_value=True), patch.object(server, 'storage', return_value=Path('synthetic-storage')), patch.object(server, 'search', return_value={"articles":[]}) as search:
            result = server.call_tool('search_public_articles', {})
            self.assertEqual(result, {"articles":[]})
            self.assertEqual(search.call_args.args[1], capture)

    def test_stdio_startup_and_tools(self):
        requests = [{"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{"protocolVersion":"2025-06-18"}}, {"jsonrpc":"2.0", "id":2, "method":"tools/list"}, {"jsonrpc":"2.0", "id":3, "method":"tools/call", "params":{"name":"get_public_account_home", "arguments":{"biz":"MTIzNDU="}}}]
        result = subprocess.run([sys.executable, '-B', '-u', str(ROOT/'wechat-local/server.py')], input=''.join(json.dumps(q)+'\n' for q in requests), text=True, capture_output=True, timeout=10, check=True)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(responses[0]['result']['serverInfo']['version'], '0.3.3')
        self.assertEqual(len(responses[1]['result']['tools']), 8)
        self.assertNotIn('isError', responses[2]['result'])


if __name__ == '__main__':
    unittest.main()
