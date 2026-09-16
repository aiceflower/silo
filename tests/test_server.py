import json
import io
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def local_server(tmp_path):
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(SKILL / "scripts" / "serve.py"), "--port", str(port), "--root", str(tmp_path / "library")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=.2)
            break
        except Exception:
            time.sleep(.05)
    else:
        process.terminate()
        raise RuntimeError("Silo server did not start")
    yield base
    process.terminate()
    process.wait(timeout=3)


def request(base, path, method="GET", data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    all_headers = {"Content-Type": "application/json", "Origin": base, **(headers or {})}
    req = urllib.request.Request(base + path, body, all_headers, method=method)
    with urllib.request.urlopen(req, timeout=3) as response:
        return response.status, response.headers, response.read()


def multipart_payload(parts):
    boundary = "----silo-test-boundary"
    chunks = []
    for name, value, filename, content_type in parts:
        chunks.append(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append((disposition + "\r\n").encode())
        if content_type:
            chunks.append(f"Content-Type: {content_type}\r\n".encode())
        chunks.append(b"\r\n" + (value if isinstance(value, bytes) else str(value).encode()) + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def raw_request(base, path, body, content_type):
    req = urllib.request.Request(base + path, body, {"Content-Type": content_type, "Origin": base}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as response:
        return response.status, json.loads(response.read())


def test_static_interface_and_asset_workflow(local_server):
    health = json.loads(request(local_server, "/api/health")[2])
    assert health["app"] == "silo" and health["root"]
    status, headers, body = request(local_server, "/")
    assert status == 200 and b"Silo" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert request(local_server, "/ui/app.js")[0] == 200
    assert request(local_server, "/vendor/jodit/jodit.min.js")[0] == 200

    status, _, body = request(local_server, "/api/assets", "POST", {
        "title": "服务测试笔记", "asset_type": "note", "content_text": "原始信息",
        "summary": "测试摘要", "explore_level": "basic", "tags": []
    })
    asset = json.loads(body)["asset"]
    assert status == 200 and asset["asset_type"] == "note"
    result = json.loads(request(local_server, "/api/assets?q=" + urllib.parse.quote("原始信息"))[2])
    assert result["total"] == 1

    updated = json.loads(request(local_server, f"/api/assets/{asset['id']}", "PATCH", {
        "revision": asset["revision"], "user_note": "人工备注"
    })[2])
    assert updated["user_note"] == "人工备注"


def test_rich_text_image_upload_and_persistence(local_server):
    _,_,body=request(local_server,"/api/assets","POST",{"title":"图文笔记","asset_type":"note","content_text":"初始内容","explore_level":"basic","tags":[]})
    asset=json.loads(body)['asset']
    payload,boundary=multipart_payload([('file',b'\x89PNG\r\n\x1a\nrich','rich.png','image/png')])
    status,embed=raw_request(local_server,f"/api/assets/{asset['id']}/embeds",payload,f"multipart/form-data; boundary={boundary}")
    assert status==200 and embed['url'].endswith(embed['id'])
    rich=f'<h2>带格式标题</h2><div><strong>加粗正文</strong></div><img src="{embed["url"]}" alt="插图">'
    updated=json.loads(request(local_server,f"/api/assets/{asset['id']}","PATCH",{'revision':asset['revision'],'content_html':rich,'source_url':None,'canonical_url':None})[2])
    assert '<strong>加粗正文</strong>' in updated['content_html'] and '加粗正文' in updated['content_text']
    image=request(local_server,embed['url'])
    assert image[1]['Content-Type']=='image/png' and image[2].startswith(b'\x89PNG')


def test_local_image_path_import_endpoint(local_server):
    _,_,body=request(local_server,"/api/assets","POST",{"title":"本地引用","asset_type":"note","tags":[]})
    asset=json.loads(body)['asset']
    root=Path(json.loads(request(local_server,"/api/health")[2])['root'])
    source=root/'from editor.png';source.write_bytes(b'\x89PNG\r\n\x1a\npath')
    status,_,body=request(local_server,f"/api/assets/{asset['id']}/embeds/from-path","POST",{"path":str(source).replace(' ','%20')})
    image=json.loads(body)
    assert status==200 and request(local_server,image['url'])[2].endswith(b'path')


def test_write_origin_is_required(local_server):
    req = urllib.request.Request(local_server + "/api/assets", b"{}", {"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(req)
    assert error.value.code == 403


def test_html_preview_removes_active_content(local_server, tmp_path):
    # Exercise the sanitizer directly; multipart upload is covered by browser tests.
    sys.path.insert(0, str(SKILL / "scripts"))
    from serve import sanitize_html
    safe = sanitize_html('<h1 onclick="bad()">正文</h1><script>BAD()</script><img src="https://tracker.test/x">')
    assert "正文" in safe and "BAD" not in safe and "onclick" not in safe and "tracker" not in safe


def test_upload_and_bookmark_preview(local_server):
    body, boundary = multipart_payload([
        ("title", "截图资料", None, None), ("asset_type", "image", None, None),
        ("description", "识别目标项目", None, None), ("tags_json", "[]", None, None),
        ("file", b"\x89PNG\r\n\x1a\nmock", "shot.png", "image/png"),
    ])
    status, saved = raw_request(local_server, "/api/assets/upload", body, f"multipart/form-data; boundary={boundary}")
    assert status == 200 and saved["asset"]["explore_level"] == "none"
    assert request(local_server, f"/api/assets/{saved['asset']['id']}/preview")[1]["Content-Type"] == "image/png"

    bookmark = b'<DL><DT><A HREF="https://github.com/example/project">Example</A></DL>'
    body, boundary = multipart_payload([("file", bookmark, "bookmarks.html", "text/html")])
    status, preview = raw_request(local_server, "/api/imports/bookmarks/preview", body, f"multipart/form-data; boundary={boundary}")
    assert status == 200 and preview["preview"] and preview["added"] == 1


def test_data_export_restore_clear_and_empty_share(local_server, tmp_path):
    _,_,body=request(local_server,"/api/assets","POST",{"title":"备份资产","asset_type":"note","content_text":"PRIVATE-HTTP","explore_level":"basic","tags":[]})
    original=json.loads(body)["asset"]
    stats=json.loads(request(local_server,"/api/data/stats")[2])
    assert stats["assets"]==1

    export_body=request(local_server,"/api/data/export")[2]
    with zipfile.ZipFile(io.BytesIO(export_body)) as archive:
        assert json.loads(archive.read("manifest.json"))["assets"]==1

    share_body=request(local_server,"/api/data/share-package")[2]
    share_file=tmp_path/"share.zip";share_file.write_bytes(share_body)
    with zipfile.ZipFile(share_file) as archive: archive.extractall(tmp_path/"share")
    shared_db=sqlite3.connect(tmp_path/"share/silo/data/assets.db")
    assert shared_db.execute("select count(*) from assets").fetchone()[0]==0

    request(local_server,"/api/assets","POST",{"title":"恢复时应消失","asset_type":"idea","content_text":"temporary","explore_level":"basic","tags":[]})
    body,boundary=multipart_payload([("confirmation","恢复备份",None,None),("file",export_body,"silo-data.zip","application/zip")])
    _,restored=raw_request(local_server,"/api/data/restore",body,f"multipart/form-data; boundary={boundary}")
    assert restored["stats"]["assets"]==1
    assert json.loads(request(local_server,f"/api/assets/{original['id']}")[2])["title"]=="备份资产"

    _,_,cleared_body=request(local_server,"/api/data/clear","POST",{"confirmation":"清空全部数据"})
    assert json.loads(cleared_body)["stats"]["assets"]==0
