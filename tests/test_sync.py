import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

SKILL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SKILL/'scripts'))
from store import Error,Store
from sync import sync_once


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); return sock.getsockname()[1]


def start_server(root):
    port=free_port()
    process=subprocess.Popen([sys.executable,str(SKILL/'scripts/serve.py'),'--root',str(root),'--host','127.0.0.1','--port',str(port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    for _ in range(50):
        try:
            import urllib.request
            urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=.2); break
        except Exception: time.sleep(.05)
    else:
        process.terminate(); raise RuntimeError('server did not start')
    return process,f'http://127.0.0.1:{port}'


def test_schema_v1_migrates_and_client_writes_queue_operations(tmp_path):
    root=tmp_path/'library'; store=Store(root); asset=store.save({'title':'保留的数据'})['asset']
    with store.connect() as connection: connection.execute('PRAGMA user_version=1')
    migrated=Store(root)
    with migrated.connect() as connection: assert connection.execute('PRAGMA user_version').fetchone()[0]==2
    assert migrated.get(asset['id'])['title']=='保留的数据'
    migrated.configure_sync('client','http://127.0.0.1:9999','x'*32)
    migrated.save({'title':'离线新增','asset_type':'note'})
    assert migrated.sync_status()['pending']==1


def test_server_client_sync_offline_attachment_rules_and_last_arrival_wins(tmp_path):
    server=Store(tmp_path/'server')
    file=tmp_path/'server-image.png'; file.write_bytes(b'\x89PNG\r\n\x1a\nserver')
    original=server.save({'title':'服务端基线','asset_type':'image','file_path':str(file),'tags':[{'name':'图片','facet':'domain'}]})['asset']
    server.add_discovery_source({'source_type':'rss','name':'同步来源','url':'https://example.com/sync.xml'})
    server.save_interest_rule({'name':'同步规则','include_keywords':['agent']})
    source=server.discovery_sources()[0]
    server.ingest_discovery(source['id'],[{'external_id':'cache-only','item_type':'article','title':'不应同步','url':'https://example.com/cache'}])
    server.configure_sync('server'); process,base=start_server(server.root)
    try:
        first=Store(tmp_path/'first'); first.configure_sync('client',base,server.sync_config['token'])
        second=Store(tmp_path/'second'); second.configure_sync('client',base,server.sync_config['token'])
        assert sync_once(first)['applied']>0 and sync_once(second)['applied']>0
        assert first.attachment(original['id']).read_bytes().endswith(b'server')
        assert any(x['name']=='同步来源' for x in first.discovery_sources())
        assert any(x['name']=='同步规则' for x in first.interest_rules())
        assert first.discovery_items(state='all')['total']==0

        left=first.get(original['id']); first.update(original['id'],{'revision':left['revision'],'title':'第一台修改'})
        right=second.get(original['id']); second.update(original['id'],{'revision':right['revision'],'title':'第二台后到'})
        sync_once(first); sync_once(second); sync_once(first)
        assert Store(server.root).get(original['id'])['title']=='第二台后到'
        assert first.get(original['id'])['title']==second.get(original['id'])['title']=='第二台后到'

        local_file=tmp_path/'client.pdf'; local_file.write_bytes(b'%PDF-client-file')
        created=first.save({'title':'客户端附件','asset_type':'pdf','file_path':str(local_file)})['asset']
        sync_once(first); synced=Store(server.root)
        assert synced.attachment(created['id']).read_bytes()==local_file.read_bytes()

        wrong=Store(tmp_path/'wrong'); wrong.configure_sync('client',base,'z'*32)
        with pytest.raises(Error,match='同步令牌无效'): sync_once(wrong)
    finally:
        process.terminate(); process.wait(timeout=3)


def test_sync_disable_and_clear_prevent_automatic_repopulation(tmp_path):
    store=Store(tmp_path/'library'); store.configure_sync('client','http://127.0.0.1:9999','x'*32)
    store.save({'title':'清空前'})
    result=store.clear_data('清空全部数据')
    assert result['stats']['assets']==0
    assert store.sync_status()['enabled'] is False and store.sync_status()['mode']=='standalone'


def test_client_service_automatically_sends_offline_queue(tmp_path):
    server=Store(tmp_path/'server'); server.configure_sync('server'); server_process,base=start_server(server.root)
    client_process=None
    try:
        client=Store(tmp_path/'client'); client.configure_sync('client',base,server.sync_config['token'],interval_seconds=5)
        queued=client.save({'title':'服务未启动时保存','asset_type':'note'})['asset']
        assert client.sync_status()['pending']==1
        port=free_port()
        client_process=subprocess.Popen([sys.executable,str(SKILL/'scripts/serve.py'),'--root',str(client.root),'--port',str(port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for _ in range(60):
            try:
                if Store(server.root).get(queued['id'])['title']=='服务未启动时保存': break
            except Error: pass
            time.sleep(.05)
        else: pytest.fail('client service did not synchronize the offline queue')
        assert Store(client.root).sync_status()['pending']==0
    finally:
        if client_process: client_process.terminate(); client_process.wait(timeout=3)
        server_process.terminate(); server_process.wait(timeout=3)
