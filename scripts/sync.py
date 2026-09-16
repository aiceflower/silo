#!/usr/bin/env python3
"""Dependency-free Silo client synchronization over the private-network API."""
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from store import Error


def _request(store,path,method='GET',data=None,raw=None):
    base=(store.sync_config.get('server_url') or '').rstrip('/')
    token=store.sync_config.get('token') or ''
    headers={'Authorization':'Bearer '+token,'User-Agent':'Silo-Sync/0.1'}
    body=raw
    if data is not None:
        body=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode(); headers['Content-Type']='application/json'
    request=urllib.request.Request(base+path,body,headers,method=method)
    try:
        with urllib.request.urlopen(request,timeout=15) as response:
            content=response.read()
            return json.loads(content) if content and 'json' in response.headers.get('Content-Type','') else content
    except urllib.error.HTTPError as exc:
        try: message=json.loads(exc.read()).get('error',{}).get('message')
        except Exception: message=None
        raise Error('network',message or f'同步服务返回 HTTP {exc.code}') from exc
    except (urllib.error.URLError,TimeoutError,OSError) as exc: raise Error('network',f'无法连接同步服务：{exc}') from exc


def _ensure_remote_blob(store,blob):
    digest=blob['content_hash']
    try: _request(store,f'/api/sync/blobs/{digest}',method='HEAD'); return
    except Error as exc:
        if '不存在' not in exc.message and '404' not in exc.message: raise
    path=store.blob_path(digest); content=path.read_bytes()
    if hashlib.sha256(content).hexdigest()!=digest: raise Error('file','本地附件哈希不匹配')
    _request(store,f'/api/sync/blobs/{digest}',method='PUT',raw=content)


def _ensure_local_blobs(store,event):
    for blob in event.get('payload',{}).get('blobs',[]):
        target=store._safe_blob_target(blob['local_path'])
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==blob['content_hash']: continue
        content=_request(store,f"/api/sync/blobs/{blob['content_hash']}")
        store.put_sync_blob(blob['content_hash'],content)


def _pull_all(store,remote_entities=None):
    cursor=store.sync_status()['cursor']; applied=0
    while True:
        result=_request(store,f'/api/sync/pull?after={cursor}&limit=100')
        events=result.get('events',[])
        for event in events:
            _ensure_local_blobs(store,event)
            if remote_entities is not None: remote_entities.add((event['entity_type'],event['entity_id']))
        outcome=store.apply_pulled_events(events); applied+=outcome['applied']; cursor=outcome['cursor']
        if not result.get('has_more'): break
    return applied,cursor


def sync_once(store):
    if not store.sync_config.get('enabled') or store.sync_config.get('mode')!='client': raise Error('validation','当前实例未启用客户端同步')
    try:
        bootstrap=store.client_bootstrap_state()
        if not bootstrap['initialized']:
            remote_entities=set(); applied,cursor=_pull_all(store,remote_entities)
            store.finish_client_bootstrap(bootstrap['entities'],remote_entities)
        pushed=0
        while True:
            events=store.pending_sync_operations(100)
            if not events: break
            for event in events:
                for blob in event.get('payload',{}).get('blobs',[]): _ensure_remote_blob(store,blob)
            result=_request(store,'/api/sync/push',method='POST',data={'events':events})
            accepted={item['op_id'] for item in result.get('accepted',[])}
            expected={item['op_id'] for item in events}
            if accepted!=expected: raise Error('network','服务端未确认完整同步批次')
            store.mark_sync_pushed(list(accepted)); pushed+=len(accepted)
        pulled,cursor=_pull_all(store); applied=locals().get('applied',0)+pulled
        store.set_sync_result(True,cursor=cursor)
        return {'ok':True,'pushed':pushed,'applied':applied,'cursor':cursor,'status':store.sync_status()}
    except Exception as exc:
        store.set_sync_result(False,error=str(exc))
        if isinstance(exc,Error): raise
        raise Error('network',str(exc)) from exc
