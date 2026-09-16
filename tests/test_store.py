import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
import pytest
from store import Error,Store,canonical,create_share_package,now

def report(typ='website'):
    return dict(schema_version=1,asset_type=typ,generated_at=now(),sources=[{'title':'说明','url':'https://example.com','accessed_at':now()}],unknowns=['尚未验证成本'],report_markdown='## 探索报告\n独特的光谱分析功能。',positioning='开发工具',core_features=['光谱分析'],use_cases=['研究'],strengths=[],limitations=[],alternatives=[],recommendation='try',personal_value='个人价值测试',problems_solved=[],tech_stack=[],usage_modes=[],learning_cost='unknown',maturity={'level':'unknown','reason':'尚未验证'})

def test_save_dedup_preserves_user_and_report(store):
    a=store.save({'title':'工具','source_url':'https://EXAMPLE.com:443/?utm_source=x','summary':'原摘要','user_note':'我的备注','tags':[{'name':'知识库','facet':'scenario'}]})['asset']
    a=store.update(a['id'],{'revision':a['revision'],'title':'人工标题','tags':[{'name':'人工标签','facet':'domain'}]})
    a=store.explore(a['id'],{'revision':a['revision'],'analysis_json':report()})
    r=store.save({'title':'覆盖','source_url':'https://example.com/','summary':'新摘要','user_note':'覆盖备注'})
    assert r['duplicate'] and r['asset']['id']==a['id']
    assert r['asset']['title']=='人工标题' and r['asset']['user_note']=='我的备注'
    assert r['asset']['analysis_json']==a['analysis_json'] and r['asset']['summary']=='原摘要'
    assert r['asset']['tags'][0]['source']=='user'

def test_url_policy():
    assert canonical('https://GitHub.com/Owner/Repo.git/')=='https://github.com/owner/repo'
    assert canonical('https://example.com/?a=1&utm_source=x#part')=='https://example.com/?a=1#part'
    assert canonical('https://github.com/o/r/issues/1')!=canonical('https://github.com/o/r')
    assert canonical('https://example.com/?a=1')!=canonical('https://example.com/?a=2')
    with pytest.raises(Error): canonical('javascript:alert(1)')

def test_failure_and_revision(store):
    a=store.save({'title':'网站','source_url':'https://example.com'})['asset']
    a=store.explore(a['id'],{'revision':a['revision'],'error':'无法访问'})
    assert a['explore_level']=='basic' and a['last_explored_at'] is None
    a=store.explore(a['id'],{'revision':a['revision'],'analysis_json':report()})
    old=a['last_explored_at'];a=store.explore(a['id'],{'revision':a['revision'],'error':'超时'})
    assert a['explore_level']=='deep' and a['last_explored_at']==old and a['analysis_json']
    with pytest.raises(Error,match='已更新'): store.update(a['id'],{'revision':1,'title':'过期'})
    with pytest.raises(Error,match='已更新'): store.explore(a['id'],{'revision':1,'analysis_json':report()})

def test_tag_alias_confidence_and_human_priority(store):
    a=store.save({'title':'标签','tags':[{'name':' Python ','facet':'technology'},{'name':'低置信','facet':'domain','confidence':.64}]})['asset']
    assert len(a['tags'])==1
    store.alias(a['tags'][0]['id'],'蟒蛇语言')
    b=store.save({'title':'复用','tags':[{'name':'蟒蛇语言','facet':'technology'}]})['asset']
    assert a['tags'][0]['id']==b['tags'][0]['id']
    b=store.update(b['id'],{'revision':b['revision'],'tags':[{'name':'PYTHON','facet':'technology'}]})
    b=store.explore(b['id'],{'revision':b['revision'],'analysis_json':report('idea'),'tags':[{'name':'python','facet':'technology','confidence':.7}]})
    assert b['tags'][0]['source']=='user' and b['tags'][0]['confidence']==1
    c=store.save({'title':'其他','tags':[{'name':'Java','facet':'technology'}]})['asset']
    with pytest.raises(Error): store.alias(c['tags'][0]['id'],'python')
    assert store.search('蟒蛇语言')['total']==2

def test_search_chinese_report_filters_and_literal_input(store):
    a=store.save({'title':'截图工具','summary':'网页抓取','tags':[{'name':'Python','facet':'technology'}]})['asset']
    a=store.explore(a['id'],{'revision':a['revision'],'analysis_json':report('idea')})
    for q in ['截图','抓取','python','光谱','截图 抓取']: assert store.search(q)['total']==1
    for q in ['" OR *','%','不存在','NEAR(foo)']: assert store.search(q)['total']==0
    assert store.search(tags=[a['tags'][0]['id']])['total']==1
    a=store.update(a['id'],{'revision':a['revision'],'title':'新标题','status':'archived'})
    assert store.search('新标题')['total']==0
    assert store.search('新标题',status='archived')['total']==1
    assert store.search('截图',status='all')['total']==0
    with store.connect() as c: assert c.execute('SELECT COUNT(*) FROM assets_fts WHERE assets_fts MATCH ?',('截图',)).fetchone()[0]==0

def test_transaction_rolls_back(store):
    with pytest.raises(Error): store.save({'title':'不应存在','tags':[{'name':'有效','facet':'domain'},{'name':'无效','facet':'bad'}]})
    assert store.search()['total']==0 and store.tags()==[]
    a=store.save({'title':'原始'})['asset']
    with pytest.raises(Error): store.update(a['id'],{'revision':1,'title':'不应更新','tags':[{'name':'坏','facet':'bad'}]})
    assert store.get(a['id'])['title']=='原始'

def test_bookmark_import(store):
    raw=b'<DL><DT><H3>AI</H3><DL><DT><H3>Tools</H3><DL><DT><A HREF="https://github.com/o/r">Repo</A></DL></DL><DT><H3>Work</H3><DL><DT><A HREF="https://github.com/o/r?utm_source=x">Again</A><DT><A HREF="javascript:bad">Bad</A><DT><A HREF="https://example.com"></A></DL></DL>'
    p=store.import_bookmarks(raw,True); assert p['added']==2 and p['duplicate']==1 and p['failed']==1 and store.search()['total']==0
    r=store.import_bookmarks(raw); assert r['added']==2 and r['duplicate']==1 and r['failed']==1
    a=store.search(asset_type='github')['items'][0]; assert a['metadata_json']['bookmark_folders']==['AI/Tools','Work']
    a=store.update(a['id'],{'revision':a['revision'],'title':'我的仓库','status':'archived'})
    r=store.import_bookmarks(raw); assert r['added']==0 and r['duplicate']==3
    assert store.get(a['id'])['status']=='archived' and store.get(a['id'])['title']=='我的仓库'

def test_import_limits(store):
    store.config['import_max_items']=1
    with pytest.raises(Error): store.import_bookmarks(b'<a href="https://a.com">a</a><a href="https://b.com">b</a>')
    store.config['import_max_bytes']=2
    with pytest.raises(Error): store.import_bookmarks(b'too long')

def test_attachment_backup_and_migration(store,tmp_path):
    file=tmp_path/'example.html';file.write_text('<p>saved</p>')
    a=store.save({'title':'HTML','asset_type':'html','file_path':str(file)})['asset']
    assert not Path(a['local_path']).is_absolute()
    assert store.save({'title':'再次','asset_type':'html','file_path':str(file)})['duplicate']
    dest=tmp_path/'backup';store.backup(dest)
    moved=tmp_path/'moved';shutil.move(str(dest),moved); other=Store(moved)
    assert other.attachment(a['id']).read_text()=='<p>saved</p>'
    assert other.search('HTML')['total']==1
    with store.connect() as c: c.execute('UPDATE assets SET local_path=? WHERE id=?',('../example.html',a['id']))
    with pytest.raises(Error): store.attachment(a['id'])

def test_export_restore_and_clear_data(store,tmp_path):
    file=tmp_path/'evidence.txt';file.write_text('private attachment')
    original=store.save({'title':'需要恢复的资产','asset_type':'other','file_path':str(file),'content_text':'private source'})['asset']
    store.configure_sync('client','http://127.0.0.1:9999','x'*32)
    archive=tmp_path/'silo-data.zip'; exported=store.export_archive(archive)
    assert exported['manifest']['assets']==1 and archive.is_file()
    with zipfile.ZipFile(archive) as z:
        assert {'manifest.json','data/assets.db','data/config.json','data/sync.json'}.issubset(z.namelist())
    store.save({'title':'备份后新增'})
    with pytest.raises(Error,match='恢复需要确认'): store.restore_archive(archive,'错误确认')
    restored=store.restore_archive(archive,'恢复备份')
    assert restored['stats']['assets']==1 and store.get(original['id'])['title']=='需要恢复的资产'
    assert store.sync_status()['enabled'] is False
    assert store.search('备份后新增',status='all')['total']==0 and store.attachment(original['id']).read_text()=='private attachment'
    with pytest.raises(Error,match='清空需要确认'): store.clear_data('错误确认')
    cleared=store.clear_data('清空全部数据')
    assert cleared['stats']['assets']==0 and store.search(status='all')['total']==0
    assert not any(p.is_file() for p in (store.root/'storage').rglob('*'))

def test_invalid_restore_preserves_current_data(store,tmp_path):
    asset=store.save({'title':'必须保留'})['asset']
    broken=tmp_path/'broken.zip';broken.write_bytes(b'not a zip')
    with pytest.raises(Error,match='有效的 Silo 备份包'): store.restore_archive(broken,'恢复备份')
    assert store.get(asset['id'])['title']=='必须保留'

def test_share_package_has_fresh_empty_database(store,tmp_path):
    store.save({'title':'绝不能进入分享包','content_text':'PRIVATE-MARKER'})
    store.configure_sync('client','http://127.0.0.1:9999','x'*32)
    archive=tmp_path/'silo-empty.zip';result=create_share_package(store.root,archive)
    assert result['contains_user_data'] is False
    unpacked=tmp_path/'unpacked'
    with zipfile.ZipFile(archive) as z:
        assert all('..' not in Path(name).parts for name in z.namelist())
        z.extractall(unpacked)
    shared=Store(unpacked/'silo')
    assert shared.stats()['assets']==0
    assert not (unpacked/'silo/data/sync.json').exists()
    assert b'PRIVATE-MARKER' not in (unpacked/'silo/data/assets.db').read_bytes()

def test_cli_is_cwd_independent(tmp_path):
    cli=Path(__file__).resolve().parents[1]/'scripts/asset_db.py'
    p=subprocess.run([sys.executable,str(cli),'--root',str(tmp_path/'cli'),'save'],input=json.dumps({'title':'独立 CLI'}),text=True,cwd=tmp_path,capture_output=True)
    assert p.returncode==0 and json.loads(p.stdout)['ok']
    p=subprocess.run([sys.executable,str(cli),'--root',str(tmp_path/'cli'),'save'],input='{}',text=True,capture_output=True)
    assert p.returncode==1 and json.loads(p.stdout)['error']['code']=='validation'

def test_invalid_report_does_not_overwrite(store):
    a=store.save({'title':'测试'})['asset']
    with pytest.raises(Error): store.explore(a['id'],{'revision':1,'analysis_json':{'schema_version':1}})
    assert store.get(a['id'])['revision']==1

def test_facet_names_distinct(store):
    a=store.save({'title':'知识','tags':[{'name':'知识库','facet':'scenario'},{'name':'知识管理','facet':'scenario'}]})['asset']
    assert len(a['tags'])==2

def test_idea_not_deduplicated(store):
    assert store.save({'title':'相同灵感'})['asset']['id']!=store.save({'title':'相同灵感'})['asset']['id']

def test_note_is_first_class_and_searchable(store):
    a=store.save({'title':'会议后的想法','asset_type':'note','content_text':'决定先验证图片探索流程','tags':[{'name':'工作记录','facet':'scenario'}]},source='user')['asset']
    assert a['asset_type']=='note' and a['explore_level']=='basic'
    assert store.search('图片探索',asset_type='note')['items'][0]['id']==a['id']

def test_attachment_size_limit(store,tmp_path):
    store.config['attachment_max_bytes']=4
    file=tmp_path/'large.bin';file.write_bytes(b'12345')
    with pytest.raises(Error,match='附件不可超过'): store.save({'title':'过大附件','asset_type':'other','file_path':str(file)})

def test_concurrent_writers(store):
    from concurrent.futures import ThreadPoolExecutor
    a=store.save({'title':'并发'})['asset']
    def update(title):
        try: store.update(a['id'],{'revision':a['revision'],'title':title}); return 'ok'
        except Error as e: return e.code
    with ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(update,['甲','乙']))
    assert sorted(outcomes)==['conflict','ok']

def test_rich_text_is_sanitized_searchable_and_keeps_managed_image(store):
    asset=store.save({'title':'富文本笔记','asset_type':'note','content_text':'旧内容'})['asset']
    image=store.add_embed(asset['id'],'示意图.png',b'\x89PNG\r\n\x1a\nimage')
    rich=f'''<h2>设计说明</h2><div><strong>关键内容</strong></div>
      <img src="{image['url']}" alt="示意图" onerror="bad()">
      <img src="https://tracker.invalid/a.png"><script>EVIL()</script>'''
    updated=store.update(asset['id'],{'revision':asset['revision'],'content_html':rich,'source_url':None,'canonical_url':None})
    assert '<h2>设计说明</h2>' in updated['content_html']
    assert image['url'] in updated['content_html'] and 'onerror' not in updated['content_html']
    assert 'tracker.invalid' not in updated['content_html'] and 'EVIL' not in updated['content_html']
    assert '关键内容' in updated['content_text'] and store.search(q='关键内容')['total']==1
    assert updated['embeds'][0]['id']==image['id'] and updated['embeds'][0]['local_path'].startswith('storage/embeds/')
    item,path=store.embed(asset['id'],image['id'])
    assert item['mime_type']=='image/png' and path.read_bytes().startswith(b'\x89PNG')

def test_local_markdown_image_path_is_copied_into_managed_storage(store):
    asset=store.save({'title':'本地图片引用','asset_type':'note'})['asset']
    source=store.root/'incoming image.png';source.write_bytes(b'\x89PNG\r\n\x1a\nlocal')
    image=store.add_embed_from_path(asset['id'],str(source).replace(' ','%20'))
    assert image['url'].startswith(f"/api/assets/{asset['id']}/embeds/")
    assert (store.root/image['local_path']).read_bytes()==source.read_bytes()
    with pytest.raises(Error,match='仅允许导入'):
        store.add_embed_from_path(asset['id'],'/etc/passwd')

def test_html_text_and_pdf_fallback(store,tmp_path):
    html=tmp_path/'text.html';html.write_text('<h1>正文可检索</h1><script>SECRET_SCRIPT</script>')
    a=store.save({'title':'文件','asset_type':'html','file_path':str(html)})['asset']
    assert '正文可检索' in a['content_text'] and 'SECRET_SCRIPT' not in a['content_text']
    assert store.search('正文可检索')['total']==1
    pdf=tmp_path/'broken.pdf';pdf.write_bytes(b'not a pdf')
    b=store.save({'title':'损坏 PDF','asset_type':'pdf','file_path':str(pdf)})['asset']
    assert b['metadata_json']['extraction_warning'] and store.attachment(b['id']).exists()

def test_reindex_and_pagination(store):
    for i in range(4): store.save({'title':f'分页 {i}'})
    assert store.search(page=2,page_size=3)['items'][0]['title']=='分页 0'
    with store.connect() as c: c.execute('DELETE FROM assets_fts')
    assert store.reindex()['indexed']==4
    with store.connect() as c: assert c.execute('SELECT count(*) FROM assets_fts').fetchone()[0]==4

def test_newer_schema_is_not_rebuilt(store):
    with store.connect() as c: c.execute('PRAGMA user_version=99')
    with pytest.raises(Error,match='版本不受支持'): Store(store.root)

def test_database_busy_error(store):
    # Lower connect timeout only for this test to avoid waiting five seconds.
    import sqlite3
    original=store.connect
    def short_connect():
        c=original(); c.execute('PRAGMA busy_timeout=10'); return c
    store.connect=short_connect
    with original() as lock:
        lock.execute('BEGIN IMMEDIATE')
        with pytest.raises(Error) as e: store.save({'title':'被锁'})
        assert e.value.code=='database_busy'

def test_enrich_import_with_file(store,tmp_path):
    store.import_bookmarks(b'<a href="https://example.com">Imported</a>')
    a=store.search()['items'][0]; assert a['explore_level']=='none'
    f=tmp_path/'saved.html';f.write_text('<p>后续附加文件</p>')
    r=store.save({'title':'不覆盖','source_url':'https://example.com','file_path':str(f),'summary':'补齐摘要'})
    assert r['duplicate'] and r['asset']['title']=='Imported'
    assert r['asset']['explore_level']=='basic' and store.attachment(a['id']).exists()

def test_import_classification(store):
    raw='<a href="https://example.com/linkis">Apache Linkis 使用</a><a href="https://example.com/english">英语学习</a><a href="https://example.com/z">陌生内容</a>'.encode()
    store.import_bookmarks(raw)
    rows=store.search()['items']
    bytitle={a['title']:{t['name'] for t in a['tags']} for a in rows}
    assert '大数据' in bytitle['Apache Linkis 使用']
    assert '英语学习' in bytitle['英语学习']
    assert '待分类' in bytitle['陌生内容']
    assert store.classify_bookmarks()['updated']==0
    store.import_bookmarks(raw)
    assert store.search()['total']==3

def test_backfill_preview_preserves_existing(store):
    a=store.save({'title':'Spark 教程','metadata_json':{'bookmark_folders':['工作/大数据']},'explore_level':'none'})['asset']
    b=store.save({'title':'英语','tags':[{'name':'人工分类','facet':'scenario'}],'metadata_json':{'bookmark_folders':['']}})['asset']
    before=store.get(a['id'])
    assert store.classify_bookmarks(True)['updated']==1 and store.get(a['id'])['tags']==[]
    assert store.classify_bookmarks()['updated']==1
    after=store.get(a['id'])
    assert after['updated_at']==before['updated_at'] and after['explore_level']=='none'
    assert after['revision']==before['revision']+1
    assert store.get(b['id'])['tags']==b['tags']
    assert store.classify_bookmarks()['updated']==0
    tag=next(t for t in store.tags(status='active') if t['name']=='大数据')
    assert store.search(tags=[tag['id']])['total']==1
    store.update(a['id'],{'revision':after['revision'],'status':'archived'})
    assert next(t for t in store.tags(status='active') if t['id']==tag['id'])['usage_count']==0

def test_classification_word_boundaries():
    from classification import classify
    assert '人工智能' not in [t['name'] for t in classify('email service','https://mail.example.com')]
    assert '人工智能' in [t['name'] for t in classify('AI tools','https://example.com')]

def test_folder_navigation_and_bulk_update(store):
    a=store.save({'title':'甲','metadata_json':{'bookmark_folders':['工作/工具']},'tags':[{'name':'开发工具','facet':'domain'}]})['asset']
    b=store.save({'title':'乙','metadata_json':{'bookmark_folders':['个人/工具']}})['asset']
    assert store.folders()==[{'name':'个人/工具','usage_count':1},{'name':'工作/工具','usage_count':1}]
    assert store.search(folders=['工作/工具'])['items'][0]['id']==a['id']
    result=store.bulk_update({'items':[{'id':a['id'],'revision':a['revision']},{'id':b['id'],'revision':b['revision']}],'add_tags':[{'name':'批量整理','facet':'scenario'}]})
    assert result['updated']==2
    assert all('批量整理' in {t['name'] for t in x['tags']} for x in result['items'])
    added=next(t for t in result['items'][0]['tags'] if t['name']=='批量整理')
    removed=store.bulk_update({'items':[{'id':x['id'],'revision':x['revision']} for x in result['items']],'remove_tag_ids':[added['id']]})
    assert all('批量整理' not in {t['name'] for t in x['tags']} for x in removed['items'])
    with pytest.raises(Error,match='已更新'):
        store.bulk_update({'items':[{'id':a['id'],'revision':a['revision']}],'status':'archived'})
    assert store.get(a['id'])['status']=='active'
