"""Local stdio MCP adapter for the WeChat reader."""

import ctypes
import json
import os
import sys
from argparse import Namespace
from pathlib import Path

from wechat_reader import run
from public_articles import search, fetch_batch, account_home
from discovery import discover, resolve_candidate
from public_index import collect_candidates

TOOLS = [{'name': 'find_conversation',
  'description': 'Find matching local WeChat contacts or groups by name or ID. Returns up to 20 '
                 'matches.',
  'inputSchema': {'type': 'object',
                  'properties': {'query': {'type': 'string', 'minLength': 1}},
                  'required': ['query'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
 {'name': 'read_conversation',
  'description': 'Read one explicitly requested local WeChat conversation, newest first. Use '
                 'filehelper for 文件传输助手.',
  'inputSchema': {'type': 'object',
                  'properties': {'chat_id': {'type': 'string', 'minLength': 1},
                                 'limit': {'type': 'integer',
                                           'minimum': 1,
                                           'maximum': 100,
                                           'default': 20},
                                 'since': {'type': 'integer',
                                           'description': 'Inclusive Unix timestamp'},
                                 'until': {'type': 'integer',
                                           'description': 'Exclusive Unix timestamp'}},
                  'required': ['chat_id'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
 {'name': 'search_public_articles',
  'description': 'Search retained WeChat article cards by exact publisher, optional title and date '
                 'range in explicitly selected chats. Shared dates are not publication dates. '
                 'Published mode excludes cards without publication dates. Local coverage may be '
                 'incomplete.',
  'inputSchema': {'type': 'object',
                  'properties': {'publisher': {'type': 'string'},
                                 'chat_ids': {'type': 'array',
                                              'items': {'type': 'string'},
                                              'minItems': 1,
                                              'maxItems': 10},
                                 'start_date': {'type': 'string',
                                                'description': 'YYYY-MM-DD Beijing time, '
                                                               'inclusive'},
                                 'end_date': {'type': 'string',
                                              'description': 'YYYY-MM-DD Beijing time, exclusive'},
                                 'title': {'type': 'string'},
                                 'date_basis': {'type': 'string',
                                                'enum': ['shared', 'published'],
                                                'default': 'shared'}},
                  'required': ['publisher', 'chat_ids', 'start_date', 'end_date'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
 {'name': 'fetch_public_articles',
  'description': 'Fetch 1-20 public WeChat article bodies. Returns extracted text or individual '
                 'errors. Verify the extracted text is complete before summarizing; captcha or '
                 'unavailable pages are not full text.',
  'inputSchema': {'type': 'object',
                  'properties': {'urls': {'type': 'array',
                                          'items': {'type': 'string'},
                                          'minItems': 1,
                                          'maxItems': 20}},
                  'required': ['urls'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True, 'openWorldHint': True}},
 {'name': 'discover_public_articles',
  'description': 'Search publicly indexed WeChat ORIGINAL article candidates by publisher and '
                 'optional title, without desktop control or local chat history. Filters '
                 'publication dates. Results may be incomplete and require original-link/full-text '
                 'verification.',
  'inputSchema': {'type': 'object',
                  'properties': {'publisher': {'type': 'string'},
                                 'title': {'type': 'string'},
                                 'start_date': {'type': 'string'},
                                 'end_date': {'type': 'string',
                                              'description': 'YYYY-MM-DD exclusive, Beijing time'},
                                 'pages': {'type': 'integer',
                                           'minimum': 1,
                                           'maximum': 5,
                                           'default': 2}},
                  'required': ['publisher', 'start_date', 'end_date'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True, 'openWorldHint': True}},
 {'name': 'resolve_public_article',
  'description': 'Resolve a Sogou WeChat search candidate to the original mp.weixin.qq.com article '
                 'URL; reports verification_required without pretending the body is available.',
  'inputSchema': {'type': 'object',
                  'properties': {'search_link': {'type': 'string'}},
                  'required': ['search_link'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True, 'openWorldHint': True}},
 {'name': 'get_public_account_home',
  'description': 'Generate the original WeChat publisher home link from __biz or an original '
                 'article URL containing __biz. Does not grant access to protected history APIs.',
  'inputSchema': {'type': 'object',
                  'properties': {'biz': {'type': 'string'}, 'article_url': {'type': 'string'}},
                  'anyOf': [{'required': ['biz']}, {'required': ['article_url']}],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
 {'name': 'list_public_article_candidates',
  'description': 'Collect title/excerpt candidates from a specified public Tonghuashun author '
                 'index, without desktop control. Not a full WeChat history or full-text tool; '
                 'dates and publisher identity still require original verification.',
  'inputSchema': {'type': 'object',
                  'properties': {'publisher': {'type': 'string', 'minLength': 1},
                                 'author_url': {'type': 'string',
                                                'description': 'HTTPS numeric author URL on '
                                                               't.10jqka.com.cn'},
                                 'pages': {'type': 'integer',
                                           'minimum': 1,
                                           'maximum': 20,
                                           'default': 3}},
                  'required': ['publisher', 'author_url'],
                  'additionalProperties': False},
  'annotations': {'readOnlyHint': True, 'openWorldHint': True}}]


def storage():
    configured = os.environ.get("WECHAT_DB_STORAGE")
    if configured:
        path = Path(configured).expanduser()
        if not (path / "contact/contact.db").is_file():
            raise ValueError("WECHAT_DB_STORAGE 必须指向微信的 db_storage 文件夹。")
        return path
    documents = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, documents):
        raise ValueError("无法定位 Windows 文档文件夹。")
    root = Path(documents.value) / "xwechat_files"
    paths = [p for p in root.glob("*/db_storage") if (p / "contact/contact.db").is_file()]
    if len(paths) != 1:
        raise ValueError("需要配置唯一的微信账号数据库。")
    return paths[0]


def call_tool(name, args):
    if name == "get_public_account_home":
        return account_home(args)
    if name == "resolve_public_article":
        return resolve_candidate(args["search_link"])
    if name == "discover_public_articles":
        return discover(args)
    if name == "fetch_public_articles":
        return fetch_batch(args["urls"])
    if name == "list_public_article_candidates":
        return collect_candidates(args)
    if name not in {"find_conversation", "read_conversation", "search_public_articles"}:
        raise ValueError("未知工具。")
    capture_path = os.environ.get("WECHAT_CAPTURE_FILE")
    capture = Path(capture_path).expanduser() if capture_path else None
    if capture is None or not capture.is_file():
        raise ValueError("找不到本机微信密钥文件，需要重新配置读取器。")
    if name == "search_public_articles":
        return search(storage(), capture, args)
    chat = args.get("chat_id") if name == "read_conversation" else None
    limit, since, until = args.get("limit", 20), args.get("since", 0), args.get("until", 253402300799)
    query = args.get("query", "")
    if name == "read_conversation" and (not isinstance(chat, str) or not chat.strip()):
        raise ValueError("请指定一个会话 ID。")
    if name == "find_conversation" and (not isinstance(query, str) or not query.strip()):
        raise ValueError("请输入联系人或群名。")
    if any(type(v) is not int for v in (limit, since, until)) or not (1 <= limit <= 100 and 0 <= since < until <= 253402300799):
        raise ValueError("消息条数须为 1 到 100，且时间范围必须有效。")
    rows = run(Namespace(storage=storage(), chat=chat, capture=capture, scratch=None,
                         limit=limit, since=since, until=until))
    if chat is None:
        term = query.strip().casefold()
        rows = [row for row in rows if any(term in str(row.get(k, "")).casefold()
                                           for k in ("id", "name", "nickname"))][:20]
    return rows


def dispatch(method, params):
    if method == "initialize":
        version = params.get("protocolVersion", "")
        return {
            "protocolVersion": version if version in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"} else "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "wechat-local", "version": "0.3.3"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            rows = call_tool(params.get("name"), params.get("arguments", {}))
        except (ValueError, OSError) as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        return {"content": [{"type": "text", "text": json.dumps(rows, ensure_ascii=False)}]}
    raise LookupError(method)


if __name__ == "__main__":
    for line in sys.stdin.buffer:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or "id" not in request:
                continue
            response = {"result": dispatch(request.get("method"), request.get("params", {}))}
        except LookupError:
            response = {"error": {"code": -32601, "message": "Method not found"}}
        except Exception:
            response = {"error": {"code": -32603, "message": "本地读取失败。"}}
        response.update(jsonrpc="2.0", id=request.get("id") if isinstance(request, dict) else None)
        print(json.dumps(response, ensure_ascii=True), flush=True)
