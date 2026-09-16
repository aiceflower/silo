#!/usr/bin/env python3
"""JSON CLI; resolves the database independently of cwd."""
import argparse
import json
import os
import sys
from pathlib import Path
from store import Store, Error, create_share_package
from discovery import refresh_source
from sync import sync_once

def main():
    p=argparse.ArgumentParser(); p.add_argument('--root',help='Asset data root (default: Skill root)')
    p.add_argument('command',choices=['init','save','get','update','search','explore-save','import-bookmarks','backup','export','restore','clear','package','stats','tags','alias','reindex','classify-bookmarks','discovery-sources','discovery-list','discovery-refresh','discovery-add-source','discovery-add-rule','discovery-save','discovery-state','sync-status','sync-now','sync-config','sync-disable'])
    p.add_argument('--input',default='-',help='UTF-8 JSON file or - for stdin'); p.add_argument('--id'); p.add_argument('--file'); p.add_argument('--destination'); p.add_argument('--confirm'); p.add_argument('--preview',action='store_true'); p.add_argument('--q',default=''); p.add_argument('--status',default='active'); p.add_argument('--state',default='new'); p.add_argument('--asset-type'); p.add_argument('--page',type=int,default=1); p.add_argument('--page-size',type=int,default=30); p.add_argument('--tag',action='append',default=[]); p.add_argument('--explore-level')
    a=p.parse_args()
    try:
        s=Store(a.root)
        if a.command in ('save','update','explore-save','alias','discovery-add-source','discovery-add-rule','discovery-state','sync-config'):
            data=json.load(sys.stdin) if a.input=='-' else json.loads(Path(a.input).read_text(encoding='utf-8'))
        if a.command=='init': result={'root':str(s.root),'schema_version':1}
        elif a.command=='save': result=s.save(data)
        elif a.command=='get': result=s.get(a.id)
        elif a.command=='update': result=s.update(a.id,data)
        elif a.command=='explore-save': result=s.explore(a.id,data)
        elif a.command=='search': result=s.search(q=a.q,status=a.status,asset_type=a.asset_type,page=a.page,page_size=a.page_size,tags=a.tag,explore_level=a.explore_level)
        elif a.command=='classify-bookmarks': result=s.classify_bookmarks(a.preview)
        elif a.command=='reindex': result=s.reindex()
        elif a.command=='stats': result=s.stats()
        elif a.command=='sync-status': result=s.sync_status()
        elif a.command=='sync-now': result=sync_once(s)
        elif a.command=='sync-config': result=s.configure_sync(data.get('mode'),data.get('server_url'),data.get('token') or os.environ.get('SILO_SYNC_TOKEN'),data.get('interval_seconds',30))
        elif a.command=='sync-disable': result=s.disable_sync()
        elif a.command=='tags': result=s.tags(a.q)
        elif a.command=='alias': result=s.alias(data['tag_id'],data['alias'])
        elif a.command=='discovery-sources': result=s.discovery_sources()
        elif a.command=='discovery-list': result=s.discovery_items(item_type=a.asset_type,state=a.state,q=a.q,page=a.page,page_size=a.page_size)
        elif a.command=='discovery-refresh':
            if a.id: result=refresh_source(s,a.id)
            else:
                result={'results':[]}
                for source in s.discovery_sources():
                    if source['enabled']:
                        try: result['results'].append(refresh_source(s,source['id']))
                        except Error as e: result['results'].append({'source':source['name'],'error':str(e)})
        elif a.command=='discovery-add-source': result=s.add_discovery_source(data)
        elif a.command=='discovery-add-rule': result=s.save_interest_rule(data)
        elif a.command=='discovery-save': result=s.save_discovery_item(a.id)
        elif a.command=='discovery-state': result=s.set_discovery_state(a.id,data.get('state'))
        elif a.command=='import-bookmarks':
            if not a.file: raise Error('validation','需要 --file')
            result=s.import_bookmarks(Path(a.file).read_bytes(),a.preview)
        elif a.command=='backup':
            if not a.destination: raise Error('validation','需要 --destination')
            result=s.backup(a.destination)
        elif a.command=='export':
            if not a.destination: raise Error('validation','需要 --destination')
            result=s.export_archive(a.destination)
        elif a.command=='restore':
            if not a.file: raise Error('validation','需要 --file')
            result=s.restore_archive(a.file,a.confirm)
        elif a.command=='clear': result=s.clear_data(a.confirm)
        elif a.command=='package':
            if not a.destination: raise Error('validation','需要 --destination')
            result=create_share_package(Path(__file__).resolve().parent.parent,a.destination)
        print(json.dumps({'ok':True,'data':result},ensure_ascii=False))
    except (Error,OSError,ValueError,KeyError,TypeError) as e:
        print(json.dumps({'ok':False,'error':{'code':getattr(e,'code','validation'),'message':str(e)}},ensure_ascii=False)); return 1
    return 0

if __name__=='__main__': sys.exit(main())
