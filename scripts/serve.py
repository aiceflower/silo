#!/usr/bin/env python3
"""Dependency-free local HTTP interface for the Silo Skill."""
import argparse
import hmac
import html
import ipaddress
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from store import Error, Store, create_share_package  # noqa: E402
from discovery import refresh_source  # noqa: E402
from sync import sync_once  # noqa: E402

UI = ROOT / "assets" / "ui"
VENDOR = ROOT / "assets" / "vendor"
PAGE_CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
PREVIEW_CSP = "default-src 'none'; script-src 'none'; style-src 'none'; img-src 'none'; font-src 'none'; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; sandbox"


class StaticHTMLSanitizer(HTMLParser):
    allowed = {"p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "b", "strong", "i", "em", "u", "s", "ul", "ol", "li", "br", "hr", "blockquote", "pre", "code", "table", "thead", "tbody", "tr", "td", "th", "caption", "section", "article", "header", "footer"}
    blocked = {"script", "style", "template", "iframe", "object", "embed", "form", "svg", "math"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.blocked:
            self.depth += 1
            return
        if self.depth or tag not in self.allowed:
            return
        safe = []
        if tag in {"td", "th"}:
            for key, value in attrs:
                if key in {"colspan", "rowspan"} and value and value.isdigit():
                    safe.append(f' {key}="{html.escape(value, quote=True)}"')
        self.out.append(f"<{tag}{''.join(safe)}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in self.allowed and tag.lower() not in {"br", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.blocked:
            if self.depth:
                self.depth -= 1
            return
        if not self.depth and tag in self.allowed and tag not in {"br", "hr"}:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.depth:
            self.out.append(html.escape(data))


def sanitize_html(text):
    parser = StaticHTMLSanitizer()
    parser.feed(text)
    parser.close()
    return "<!doctype html><meta charset=utf-8><title>安全预览</title>" + "".join(parser.out)


def multipart(body, content_type):
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.is_multipart():
        raise Error("validation", "上传格式无效")
    fields, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = {"filename": Path(filename).name, "content": payload, "content_type": part.get_content_type()}
        else:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    server_version = "Silo/0.1"

    @property
    def store(self):
        return self.server.store

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_bytes(self, body, status=200, content_type="application/octet-stream", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), status, "application/json; charset=utf-8")

    def fail(self, exc):
        code = getattr(exc, "code", "internal")
        status = {"validation": 422, "not_found": 404, "conflict": 409, "database_busy": 503, "file": 400, "forbidden": 403, "network": 502}.get(code, 500)
        self.send_json({"error": {"code": code, "message": str(exc)}}, status)

    def request_body(self, extra=1024 * 1024, maximum=None):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise Error("validation", "Content-Length 无效") from exc
        maximum = maximum or max(self.store.config["attachment_max_bytes"], self.store.config["import_max_bytes"]) + extra
        if length <= 0 or length > maximum:
            raise Error("file", "请求内容为空或超过限制")
        return self.rfile.read(length)

    def check_host(self):
        try: loopback=ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError: loopback=False
        if not loopback: raise Error("forbidden", "普通页面和资产接口只接受本机请求")
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise Error("forbidden", "只接受本机请求")

    def check_write_origin(self):
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            raise Error("forbidden", "拒绝跨站写入")
        expected = f"http://{self.headers.get('Host')}"
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        if origin and origin.rstrip("/") != expected.rstrip("/"):
            raise Error("forbidden", "请求来源不匹配")
        if not origin and referer:
            parsed = urlsplit(referer)
            if f"{parsed.scheme}://{parsed.netloc}".rstrip("/") != expected.rstrip("/"):
                raise Error("forbidden", "请求来源不匹配")
        if not origin and not referer:
            raise Error("forbidden", "写入需要同源 Origin 或 Referer")

    def check_sync_auth(self):
        if self.store.sync_config.get('mode')!='server' or not self.store.sync_config.get('enabled'):
            raise Error('not_found','同步服务未启用')
        expected='Bearer '+str(self.store.sync_config.get('token') or '')
        if not hmac.compare_digest(self.headers.get('Authorization',''),expected): raise Error('forbidden','同步令牌无效')

    def dispatch(self):
        parsed = urlsplit(self.path)
        path, query = parsed.path, parse_qs(parsed.query, keep_blank_values=True)
        is_sync=path.startswith('/api/sync/')
        if is_sync: self.check_sync_auth()
        else: self.check_host()
        if self.command not in {"GET", "HEAD"} and not is_sync:
            self.check_write_origin()

        if is_sync:
            if path=='/api/sync/status' and self.command in {'GET','HEAD'}: return self.send_json(self.store.sync_status())
            blob_match=re.fullmatch(r'/api/sync/blobs/([0-9a-f]{64})',path)
            if blob_match and self.command in {'GET','HEAD'}:
                blob=self.store.blob_path(blob_match.group(1)); return self.send_bytes(blob.read_bytes(),headers={'Cache-Control':'private, max-age=31536000, immutable'})
            if blob_match and self.command=='PUT': return self.send_json(self.store.put_sync_blob(blob_match.group(1),self.request_body(maximum=self.store.config['attachment_max_bytes'])))
            if path=='/api/sync/pull' and self.command in {'GET','HEAD'}:
                return self.send_json(self.store.sync_events(int(query.get('after',['0'])[0]),int(query.get('limit',['100'])[0])))
            if path=='/api/sync/push' and self.command=='POST':
                body=json.loads(self.request_body().decode()); return self.send_json(self.store.server_push(body.get('events')))
            raise Error('not_found','同步接口不存在')

        if self.command in {"GET", "HEAD"}:
            if path == "/api/health":
                return self.send_json({"ok": True, "app": "silo", "schema_version": 2, "root": str(self.store.root),"mode":self.store.sync_config.get('mode','standalone')})
            if path == "/api/assets":
                one = lambda key, default=None: query.get(key, [default])[0]
                return self.send_json(self.store.search(q=one("q", ""), asset_type=one("asset_type"), status=one("status", "active"), explore_level=one("explore_level"), tags=query.get("tags", []), folders=query.get("folders", []), page=int(one("page", 1)), page_size=int(one("page_size", 30))))
            if path == "/api/tags":
                return self.send_json(self.store.tags(query.get("q", [""])[0], query.get("status", ["all"])[0]))
            if path == "/api/folders":
                return self.send_json(self.store.folders(query.get("status", ["active"])[0]))
            if path == "/api/data/stats":
                return self.send_json(self.store.stats())
            if path == '/api/data/sync/status':
                return self.send_json(self.store.sync_status())
            if path == "/api/discovery/sources":
                return self.send_json(self.store.discovery_sources())
            if path == "/api/discovery/items":
                one=lambda key,default=None: query.get(key,[default])[0]
                return self.send_json(self.store.discovery_items(item_type=one('item_type'),state=one('state','new'),source_id=one('source_id'),q=one('q',''),language=one('language'),topic=one('topic'),page=int(one('page',1)),page_size=int(one('page_size',30))))
            if path == "/api/discovery/facets":
                return self.send_json(self.store.discovery_facets(query.get('state',['new'])[0]))
            if path == "/api/discovery/rules":
                return self.send_json(self.store.interest_rules())
            if path in {"/api/data/export", "/api/data/share-package"}:
                stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
                with tempfile.TemporaryDirectory(prefix='silo-download-') as temp:
                    filename=f"silo-data-{stamp}.zip" if path.endswith('/export') else f"silo-skill-empty-{stamp}.zip"
                    output=Path(temp)/filename
                    if path.endswith('/export'): self.store.export_archive(output)
                    else: create_share_package(ROOT,output)
                    return self.send_bytes(output.read_bytes(),content_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{filename}"','Cache-Control':'no-store'})
            embed_match=re.fullmatch(r"/api/assets/([^/]+)/embeds/([0-9a-f-]{36})",path)
            if embed_match:
                item,file_path=self.store.embed(*embed_match.groups())
                return self.send_bytes(file_path.read_bytes(),content_type=item['mime_type'],headers={'Cache-Control':'private, max-age=31536000, immutable','Content-Security-Policy':"default-src 'none'"})
            match = re.fullmatch(r"/api/assets/([^/]+)(?:/(attachment|preview))?", path)
            if match:
                asset_id, action = match.groups()
                if not action:
                    return self.send_json(self.store.get(asset_id))
                asset = self.store.get(asset_id)
                file_path = self.store.attachment(asset_id)
                if action == "attachment":
                    filename = Path((asset.get("metadata_json") or {}).get("filename", file_path.name)).name
                    return self.send_bytes(file_path.read_bytes(), content_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}", "Content-Security-Policy": "default-src 'none'; sandbox"})
                if asset["asset_type"] == "html":
                    safe = sanitize_html(file_path.read_text(encoding="utf-8", errors="replace")).encode()
                    return self.send_bytes(safe, content_type="text/html; charset=utf-8", headers={"Content-Security-Policy": PREVIEW_CSP})
                if asset["asset_type"] in {"image", "capture"}:
                    raw = file_path.read_bytes()
                    media = "image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/gif" if raw.startswith((b"GIF87a", b"GIF89a")) else "image/webp" if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" else None
                    if media:
                        return self.send_bytes(raw, content_type=media, headers={"Content-Security-Policy": PREVIEW_CSP})
                raise Error("validation", "该附件仅支持下载")
            if path.startswith("/ui/"):
                name = path.removeprefix("/ui/")
                if name not in {"app.js", "style.css"}:
                    raise Error("not_found", "静态文件不存在")
                file_path = UI / name
                return self.send_bytes(file_path.read_bytes(), content_type=mimetypes.guess_type(name)[0] or "application/octet-stream", headers={"Content-Security-Policy": PAGE_CSP})
            if path.startswith('/vendor/jodit/'):
                name=path.removeprefix('/vendor/jodit/')
                if name not in {'jodit.min.js','jodit.min.css','LICENSE.txt'}: raise Error('not_found','静态文件不存在')
                file_path=VENDOR/'jodit'/name
                return self.send_bytes(file_path.read_bytes(),content_type=mimetypes.guess_type(name)[0] or 'text/plain',headers={'Content-Security-Policy':PAGE_CSP,'Cache-Control':'public, max-age=31536000, immutable'})
            if path.startswith("/api/"):
                raise Error("not_found", "接口不存在")
            return self.send_bytes((UI / "index.html").read_bytes(), content_type="text/html; charset=utf-8", headers={"Content-Security-Policy": PAGE_CSP})

        if path == "/api/assets" and self.command == "POST":
            body = json.loads(self.request_body().decode())
            if "file_path" in body:
                raise Error("validation", "网页创建不可读取本机路径")
            return self.send_json(self.store.save(body, "user"))
        if path == '/api/data/sync/run' and self.command=='POST':
            return self.send_json(sync_once(self.store))
        if path == "/api/discovery/sources" and self.command == "POST":
            return self.send_json(self.store.add_discovery_source(json.loads(self.request_body().decode())))
        if path == "/api/discovery/rules" and self.command == "POST":
            return self.send_json(self.store.save_interest_rule(json.loads(self.request_body().decode())))
        if path == "/api/discovery/refresh" and self.command == "POST":
            body=json.loads(self.request_body().decode()); source_id=body.get('source_id')
            if source_id: return self.send_json(refresh_source(self.store,source_id))
            results=[]
            sources=self.store.discovery_sources()
            if body.get('stale_only'):
                cutoff=datetime.now(timezone.utc)-timedelta(minutes=self.store.config['discovery_cache_minutes'])
                def stale(source):
                    if not source.get('last_checked_at'): return True
                    try: return datetime.fromisoformat(source['last_checked_at'].replace('Z','+00:00'))<cutoff
                    except ValueError: return True
                sources=[source for source in sources if stale(source)]
            for source in sources:
                if source['enabled']:
                    try: results.append(refresh_source(self.store,source['id']))
                    except Error as exc: results.append({'source':source['name'],'error':str(exc)})
            return self.send_json({'results':results,'stale_only':bool(body.get('stale_only'))})
        match = re.fullmatch(r"/api/discovery/items/([^/]+)/(state|save)",path)
        if match and self.command == "POST":
            item_id,action=match.groups()
            if action=='save': return self.send_json(self.store.save_discovery_item(item_id))
            body=json.loads(self.request_body().decode()); return self.send_json(self.store.set_discovery_state(item_id,body.get('state')))
        if path == "/api/assets" and self.command == "PATCH":
            return self.send_json(self.store.bulk_update(json.loads(self.request_body().decode())))
        match = re.fullmatch(r"/api/assets/([^/]+)", path)
        if match and self.command == "PATCH":
            return self.send_json(self.store.update(match.group(1), json.loads(self.request_body().decode())))
        local_embed_match=re.fullmatch(r"/api/assets/([^/]+)/embeds/from-path",path)
        if local_embed_match and self.command=='POST':
            body=json.loads(self.request_body().decode())
            return self.send_json(self.store.add_embed_from_path(local_embed_match.group(1),body.get('path')))
        embed_match=re.fullmatch(r"/api/assets/([^/]+)/embeds",path)
        if embed_match and self.command=='POST':
            _,files=multipart(self.request_body(),self.headers.get('Content-Type',''))
            upload=files.get('file')
            if not upload: raise Error('file','请选择图片')
            return self.send_json(self.store.add_embed(embed_match.group(1),upload['filename'],upload['content'],upload['content_type']))
        if path == "/api/assets/upload" and self.command == "POST":
            fields, files = multipart(self.request_body(), self.headers.get("Content-Type", ""))
            upload = files.get("file")
            if not upload or not upload["content"]:
                raise Error("file", "附件不能为空")
            asset_type = fields.get("asset_type", "")
            if asset_type not in {"image", "capture", "pdf", "html", "other"}:
                raise Error("validation", "该类型不支持附件上传")
            suffix = Path(upload["filename"]).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
                suffix = ".bin"
            temp_name = None
            try:
                with tempfile.NamedTemporaryFile(prefix="silo-upload-", suffix=suffix, delete=False) as temp:
                    temp.write(upload["content"])
                    temp_name = temp.name
                tags = json.loads(fields.get("tags_json", "[]"))
                filename = Path(upload["filename"] or "附件").name
                title = fields.get("title", "").strip()
                if asset_type == "capture" and not title:
                    title = Path(filename).stem or "待处理素材"
                data = {"title": title, "asset_type": asset_type, "file_path": temp_name, "summary": fields.get("summary") or fields.get("description", "")[:180], "core_value": fields.get("core_value", ""), "content_text": fields.get("description", ""), "tags": tags, "explore_level": "none" if asset_type in {"image", "capture"} else "basic", "metadata_json": {"filename": filename}}
                if fields.get("source_url", "").strip():
                    data["source_url"] = fields["source_url"].strip()
                return self.send_json(self.store.save(data, "user"))
            finally:
                if temp_name:
                    Path(temp_name).unlink(missing_ok=True)
        if path in {"/api/imports/bookmarks/preview", "/api/imports/bookmarks"} and self.command == "POST":
            _, files = multipart(self.request_body(), self.headers.get("Content-Type", ""))
            upload = files.get("file")
            if not upload:
                raise Error("file", "请选择收藏夹文件")
            return self.send_json(self.store.import_bookmarks(upload["content"], path.endswith("/preview")))
        if path == "/api/data/restore" and self.command == "POST":
            fields,files=multipart(self.request_body(maximum=self.store.config['backup_max_bytes']+1024*1024),self.headers.get('Content-Type',''))
            upload=files.get('file')
            if not upload: raise Error('file','请选择 Silo 数据备份包')
            temp_name=None
            try:
                with tempfile.NamedTemporaryFile(prefix='silo-restore-upload-',suffix='.zip',delete=False) as temp:
                    temp.write(upload['content']); temp_name=temp.name
                return self.send_json(self.store.restore_archive(temp_name,fields.get('confirmation')))
            finally:
                if temp_name: Path(temp_name).unlink(missing_ok=True)
        if path == "/api/data/clear" and self.command == "POST":
            data=json.loads(self.request_body().decode())
            return self.send_json(self.store.clear_data(data.get('confirmation')))
        raise Error("not_found", "接口不存在")

    def do_GET(self):
        try:
            self.dispatch()
        except (Error, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.fail(exc)

    do_HEAD = do_GET

    def do_POST(self):
        try:
            self.dispatch()
        except (Error, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.fail(exc)

    do_PATCH = do_POST
    do_PUT = do_POST


def main():
    parser = argparse.ArgumentParser(description="Silo local interface")
    parser.add_argument("--host", choices=["127.0.0.1", "localhost", "0.0.0.0"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", help="alternate Silo data root")
    parser.add_argument('--mode',choices=['standalone','server','client'])
    parser.add_argument('--sync-url')
    parser.add_argument('--sync-interval',type=int,default=30)
    args = parser.parse_args()
    store = Store(args.root)
    if args.mode:
        store.configure_sync(args.mode,args.sync_url,os.environ.get('SILO_SYNC_TOKEN'),args.sync_interval)
        if args.mode=='server': print('Silo sync token: '+store.sync_config['token'],file=sys.stderr)
    mode=store.sync_config.get('mode','standalone'); host=args.host or ('0.0.0.0' if mode=='server' else '127.0.0.1')
    server = ThreadingHTTPServer((host, args.port), Handler)
    server.store = store
    stop_sync=threading.Event(); sync_thread=None
    if mode=='client' and store.sync_config.get('enabled'):
        def sync_loop():
            delay=0; backoff=5
            while not stop_sync.wait(delay):
                try: sync_once(store); backoff=5; delay=store.sync_config.get('interval_seconds',30)
                except Exception as exc: print(f'Silo sync: {exc}',file=sys.stderr); delay=backoff; backoff=min(backoff*2,300)
        sync_thread=threading.Thread(target=sync_loop,name='silo-sync',daemon=True); sync_thread.start()
    print(f"Silo running at http://{host}:{args.port} ({mode})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_sync.set()
        if sync_thread: sync_thread.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
