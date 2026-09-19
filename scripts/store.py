"""Shared local asset storage. No model or network access."""
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, unquote
from classification import classify

TYPES = {'github','website','article','image','capture','idea','note','html','pdf','bookmark','other'}
FACETS = {'domain','capability','scenario','technology'}
LEVELS = {'none','basic','deep'}
DEFAULT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DISCOVERY_SOURCES = [
    ('github','GitHub 近期热门','https://api.github.com/search/repositories',{'days':30,'language':'','topic':'','min_stars':20}),
    ('hackernews','Hacker News · Top','https://hacker-news.firebaseio.com/v0/topstories.json',{'limit':40}),
    ('rss','OpenAI News','https://openai.com/news/rss.xml',{'category':'AI'}),
    ('rss','Google DeepMind','https://deepmind.google/blog/rss.xml',{'category':'AI'}),
    ('rss','Google Research','https://research.google/blog/rss/',{'category':'AI research'}),
    ('rss','Hugging Face Blog','https://huggingface.co/blog/feed.xml',{'category':'open-source AI'}),
    ('rss','LangChain Blog','https://www.langchain.com/blog/rss.xml',{'category':'AI agents'}),
    ('rss','AWS Machine Learning','https://aws.amazon.com/blogs/machine-learning/feed/',{'category':'AI engineering'}),
    ('rss','Microsoft Research','https://www.microsoft.com/en-us/research/feed/',{'category':'research'}),
    ('rss','MIT News · Artificial Intelligence','https://news.mit.edu/rss/topic/artificial-intelligence2',{'category':'AI research'}),
    ('rss','GitHub Engineering','https://github.blog/engineering/feed/',{'category':'software engineering'}),
    ('atom','Simon Willison','https://simonwillison.net/atom/everything/',{'category':'AI engineering'}),
]

def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return str(uuid.uuid4())
def norm(s): return ' '.join(unicodedata.normalize('NFKC', s).casefold().split())

class Error(Exception):
    def __init__(self, code, message): self.code, self.message = code, message; super().__init__(message)

def require(test, message):
    if not test: raise Error('validation', message)

class Connection(sqlite3.Connection):
    _silo_lock = None
    _silo_released = False
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()
    def close(self):
        if self._silo_released: return
        try: super().close()
        finally:
            self._silo_released=True
            if self._silo_lock: self._silo_lock.release()

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.blocked=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'): self.blocked+=1
        elif tag=='br' and not self.blocked: self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.blocked: self.blocked-=1
        if tag in ('p','div','li','h1','h2','h3','h4'): self.parts.append('\n')
    def handle_data(self,data):
        if not self.blocked: self.parts.append(data)

class RichTextSanitizer(HTMLParser):
    """Keep document formatting while rejecting active or remote content."""
    allowed={'div','p','span','br','strong','b','em','i','u','s','del','sub','sup','h1','h2','h3','h4','blockquote','pre','code','ul','ol','li','a','figure','figcaption','img','hr','table','thead','tbody','tfoot','tr','th','td'}
    void={'br','hr','img'}; blocked={'script','style','template','iframe','object','embed','form','svg','math'}
    def __init__(self,asset_id): super().__init__(convert_charrefs=True); self.asset_id=asset_id; self.out=[]; self.blocked_depth=0
    def _attrs(self,tag,attrs):
        safe=[]
        for key,value in attrs:
            key=key.lower(); value=value or ''
            embed_pattern=rf'/api/assets/{re.escape(self.asset_id)}/embeds/[0-9a-f-]{{36}}'
            if key=='href' and tag=='a' and (value.startswith(('http://','https://')) or re.fullmatch(embed_pattern,value)):
                safe.extend([('href',value),('target','_blank'),('rel','noreferrer noopener')])
            elif key=='src' and tag=='img' and re.fullmatch(embed_pattern,value): safe.append(('src',value))
            elif key=='alt' and tag=='img': safe.append(('alt',value[:300]))
            elif key in {'width','height'} and tag=='img' and value.isdigit() and 0<int(value)<=10000: safe.append((key,value))
            elif key in {'colspan','rowspan'} and tag in {'td','th'} and value.isdigit() and 0<int(value)<=100: safe.append((key,value))
            elif key=='style' and tag in {'div','p','h1','h2','h3','h4','td','th'}:
                match=re.fullmatch(r'\s*text-align\s*:\s*(left|center|right|justify)\s*;?\s*',value,re.I)
                if match: safe.append(('style',f'text-align:{match.group(1).lower()}'))
            elif key=='data-trix-content-type' and tag=='figure' and value in {'image/png','image/jpeg','image/gif','image/webp'}: safe.append((key,value))
            elif key=='data-trix-attachment' and tag=='figure':
                try: attachment=json.loads(value)
                except (json.JSONDecodeError,TypeError): continue
                url=attachment.get('url')
                if not isinstance(url,str) or not re.fullmatch(embed_pattern,url): continue
                clean={'url':url,'href':url,'contentType':attachment.get('contentType') if attachment.get('contentType') in {'image/png','image/jpeg','image/gif','image/webp'} else 'image/png','filename':str(attachment.get('filename','image'))[:300]}
                for field in ('filesize','width','height'):
                    if isinstance(attachment.get(field),(int,float)) and attachment[field]>=0: clean[field]=attachment[field]
                safe.append((key,json.dumps(clean,ensure_ascii=False,separators=(',',':'))))
            elif key=='class' and tag in {'figure','figcaption'}:
                classes=' '.join(x for x in value.split() if re.fullmatch(r'attachment(?:__[a-z-]+|--[a-z0-9-]+)?',x))
                if classes: safe.append(('class',classes))
        return ''.join(f' {k}="{html.escape(v,quote=True)}"' for k,v in safe)
    def handle_starttag(self,tag,attrs):
        tag=tag.lower()
        if tag in self.blocked: self.blocked_depth+=1; return
        if not self.blocked_depth and tag in self.allowed: self.out.append(f'<{tag}{self._attrs(tag,attrs)}>')
    def handle_startendtag(self,tag,attrs): self.handle_starttag(tag,attrs)
    def handle_endtag(self,tag):
        tag=tag.lower()
        if tag in self.blocked:
            if self.blocked_depth: self.blocked_depth-=1
        elif not self.blocked_depth and tag in self.allowed and tag not in self.void: self.out.append(f'</{tag}>')
    def handle_data(self,data):
        if not self.blocked_depth: self.out.append(html.escape(data))

def sanitize_rich_html(value,asset_id):
    require(isinstance(value,str),'content_html 必须为文字')
    parser=RichTextSanitizer(asset_id); parser.feed(value); parser.close(); return ''.join(parser.out).strip()

def rich_text_plain(value):
    parser=TextExtractor(); parser.feed(value); parser.close()
    return re.sub(r'\n{3,}','\n\n',''.join(parser.parts)).strip()

def canonical(url):
    if not url: return None
    require(isinstance(url,str), 'URL 必须为文字')
    try:
        p = urlsplit(url.strip()); port = p.port
        require(p.scheme.lower() in ('http','https') and p.hostname and not p.username and not p.password, '仅支持不含凭据的 HTTP(S) URL')
        host = p.hostname.lower().encode('idna').decode()
        if ':' in host: host = '[' + host + ']'
        if port and (p.scheme.lower(),port) not in [('http',80),('https',443)]: host += ':'+str(port)
        path = p.path or '/'
        if host == 'github.com' and re.fullmatch(r'/[^/]+/[^/]+/?',path):
            path = re.sub(r'\.git$','',path.rstrip('/')).lower()
        query = '&'.join(x for x in p.query.split('&') if x and not (unquote(x.split('=')[0]).lower().startswith('utm_') or unquote(x.split('=')[0]).lower() in {'fbclid','gclid','msclkid'}))
        return urlunsplit((p.scheme.lower(),host,path,query,p.fragment))
    except (ValueError,UnicodeError): raise Error('validation','URL 格式无效')

def infer(url):
    p=urlsplit(url or '')
    return 'github' if p.hostname=='github.com' and re.fullmatch(r'/[^/]+/[^/]+/?',p.path) else 'website'

class BookmarkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.stack=[]; self.pending=None; self.capture=None; self.buf=[]; self.href=None; self.rows=[]
    def handle_starttag(self,tag,attrs):
        if tag=='h3': self.capture='folder'; self.buf=[]
        elif tag=='dl': self.stack.append(self.pending); self.pending=None
        elif tag=='a': self.capture='link'; self.buf=[]; self.href=dict(attrs).get('href','')
    def handle_data(self,data):
        if self.capture: self.buf.append(data)
    def handle_endtag(self,tag):
        if tag=='h3' and self.capture=='folder': self.pending=''.join(self.buf).strip(); self.capture=None
        elif tag=='a' and self.capture=='link':
            self.rows.append({'title': ''.join(self.buf).strip() or self.href, 'source_url':self.href, 'folder':'/'.join(x for x in self.stack if x)}); self.capture=None
        elif tag=='dl' and self.stack: self.stack.pop()

class Store:
    def __init__(self, root=None):
        self._io_lock=threading.RLock()
        self.root=Path(root or DEFAULT_ROOT).resolve(); (self.root/'data').mkdir(parents=True,exist_ok=True)
        self.db=self.root/'data/assets.db'
        config=self.root/'data/config.json'
        self.defaults={'import_max_items':10000,'import_max_bytes':20*1024*1024,'attachment_max_bytes':20*1024*1024,'backup_max_bytes':512*1024*1024,'page_size':30,'discovery_cache_minutes':30,'discovery_max_items':100}
        self.config=dict(self.defaults)
        if config.exists():
            saved_config=json.loads(config.read_text()); self.config.update(saved_config)
            if any(key not in saved_config for key in self.defaults): config.write_text(json.dumps(self.config,indent=2,ensure_ascii=False))
        else: config.write_text(json.dumps(self.config,indent=2,ensure_ascii=False))
        self.sync_file=self.root/'data/sync.json'
        self.sync_config=self._load_sync_config()
        self._applying_remote=False
        with self.connect() as c:
            version=c.execute('PRAGMA user_version').fetchone()[0]
            require(version in (0,1,2),'数据库版本不受支持；请使用对应版本程序，不会自动重建')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY, asset_type TEXT NOT NULL, title TEXT NOT NULL,
              source_url TEXT, canonical_url TEXT UNIQUE, local_path TEXT, content_hash TEXT UNIQUE,
              summary TEXT NOT NULL DEFAULT '', core_value TEXT NOT NULL DEFAULT '', content_text TEXT NOT NULL DEFAULT '', content_html TEXT NOT NULL DEFAULT '', user_note TEXT NOT NULL DEFAULT '',
              analysis_json TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', explore_level TEXT NOT NULL DEFAULT 'none', status TEXT NOT NULL DEFAULT 'active',
              revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              last_explored_at TEXT, last_explore_attempt_at TEXT, last_explore_error TEXT);
            CREATE TABLE IF NOT EXISTS tags(id TEXT PRIMARY KEY,name TEXT NOT NULL,normalized_name TEXT NOT NULL,facet TEXT NOT NULL,parent_id TEXT REFERENCES tags(id),description TEXT DEFAULT '',created_at TEXT,updated_at TEXT,UNIQUE(facet,normalized_name));
            CREATE TABLE IF NOT EXISTS tag_aliases(id TEXT PRIMARY KEY,tag_id TEXT NOT NULL REFERENCES tags(id),alias TEXT NOT NULL,normalized_alias TEXT NOT NULL,created_at TEXT,UNIQUE(tag_id,normalized_alias));
            CREATE TABLE IF NOT EXISTS asset_tags(asset_id TEXT REFERENCES assets(id),tag_id TEXT REFERENCES tags(id),confidence REAL NOT NULL,source TEXT NOT NULL,created_at TEXT,PRIMARY KEY(asset_id,tag_id));
            CREATE TABLE IF NOT EXISTS asset_embeds(id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,local_path TEXT NOT NULL UNIQUE,content_hash TEXT NOT NULL,mime_type TEXT NOT NULL,filename TEXT NOT NULL,size_bytes INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(asset_id,content_hash));
            CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(id UNINDEXED,title,summary,core_value,content_text,user_note,report);
            CREATE TABLE IF NOT EXISTS discovery_sources (
              id TEXT PRIMARY KEY, source_type TEXT NOT NULL, name TEXT NOT NULL, url TEXT,
              config_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
              last_checked_at TEXT, last_error TEXT, etag TEXT, last_modified TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(source_type,url));
            CREATE TABLE IF NOT EXISTS discovery_items (
              id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES discovery_sources(id) ON DELETE CASCADE,
              external_id TEXT NOT NULL, item_type TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', published_at TEXT,
              metrics_json TEXT NOT NULL DEFAULT '{}', topics_json TEXT NOT NULL DEFAULT '[]', score REAL NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'new', asset_id TEXT REFERENCES assets(id), discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(source_id,external_id));
            CREATE TABLE IF NOT EXISTS interest_rules (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, include_keywords_json TEXT NOT NULL DEFAULT '[]',
              exclude_keywords_json TEXT NOT NULL DEFAULT '[]', languages_json TEXT NOT NULL DEFAULT '[]',
              topics_json TEXT NOT NULL DEFAULT '[]', min_stars INTEGER NOT NULL DEFAULT 0,
              max_age_days INTEGER, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS discovery_items_state_idx ON discovery_items(state,item_type,score DESC,published_at DESC);
            ''')
            self._ensure_rich_content_schema(c)
            self._ensure_sync_schema(c)
            if version != 2:
                c.execute('PRAGMA user_version=2')
            stamp=now()
            c.executemany('INSERT OR IGNORE INTO discovery_sources(id,source_type,name,url,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',[
              (uid(),source_type,name,url,json.dumps(config,ensure_ascii=False),stamp,stamp)
              for source_type,name,url,config in DEFAULT_DISCOVERY_SOURCES])

    def _load_sync_config(self):
        defaults={'mode':'standalone','device_id':None,'server_url':None,'token':None,'interval_seconds':30,'enabled':False}
        if not self.sync_file.exists(): return defaults
        try: saved=json.loads(self.sync_file.read_text(encoding='utf-8'))
        except (json.JSONDecodeError,OSError): return defaults
        return {**defaults,**saved} if isinstance(saved,dict) else defaults

    def _save_sync_config(self):
        temp=self.sync_file.with_suffix('.tmp')
        temp.write_text(json.dumps(self.sync_config,ensure_ascii=False,indent=2),encoding='utf-8')
        os.chmod(temp,0o600); os.replace(temp,self.sync_file)

    def _ensure_sync_schema(self,c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS sync_operations(
          op_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,device_seq INTEGER NOT NULL,
          entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,action TEXT NOT NULL,
          payload_json TEXT NOT NULL,blob_hashes_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,pushed_at TEXT,
          UNIQUE(device_id,device_seq));
        CREATE TABLE IF NOT EXISTS sync_server_events(
          server_seq INTEGER PRIMARY KEY AUTOINCREMENT,op_id TEXT NOT NULL UNIQUE,device_id TEXT NOT NULL,device_seq INTEGER NOT NULL,
          entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,action TEXT NOT NULL,
          payload_json TEXT NOT NULL,blob_hashes_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sync_applied(op_id TEXT PRIMARY KEY,server_seq INTEGER NOT NULL,applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sync_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sync_blobs(content_hash TEXT PRIMARY KEY,size_bytes INTEGER NOT NULL,local_path TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS sync_operations_pending_idx ON sync_operations(pushed_at,device_seq);
        ''')
    def _ensure_rich_content_schema(self,c):
        columns={row[1] for row in c.execute('PRAGMA table_info(assets)')}
        if 'content_html' not in columns: c.execute("ALTER TABLE assets ADD COLUMN content_html TEXT NOT NULL DEFAULT ''")
        c.execute('CREATE TABLE IF NOT EXISTS asset_embeds(id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,local_path TEXT NOT NULL UNIQUE,content_hash TEXT NOT NULL,mime_type TEXT NOT NULL,filename TEXT NOT NULL,size_bytes INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(asset_id,content_hash))')
    def connect(self):
        self._io_lock.acquire()
        try:
            c=sqlite3.connect(self.db,timeout=5,factory=Connection); c._silo_lock=self._io_lock; c.row_factory=sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA journal_mode=WAL'); c.create_function('norm',1,lambda s:norm(s or '')); return c
        except Exception:
            self._io_lock.release(); raise
    @contextmanager
    def tx(self):
        c=self.connect()
        try: c.execute('BEGIN IMMEDIATE'); yield c; c.commit()
        except sqlite3.OperationalError as e:
            c.rollback(); raise Error('database_busy' if 'locked' in str(e) else 'database',str(e)) from e
        except Exception: c.rollback(); raise
        finally: c.close()
    def _get(self,c,id):
        r=c.execute('SELECT * FROM assets WHERE id=?',(id,)).fetchone()
        if not r: raise Error('not_found','资产不存在')
        a=dict(r)
        for k in ('analysis_json','metadata_json'): a[k]=json.loads(a[k]) if a[k] else None
        a['tags']=[dict(x) for x in c.execute('SELECT t.*,a.confidence,a.source FROM tags t JOIN asset_tags a ON t.id=a.tag_id WHERE a.asset_id=? ORDER BY t.facet,t.name',(id,))]
        a['embeds']=[dict(x) for x in c.execute('SELECT id,local_path,mime_type,filename,size_bytes,created_at FROM asset_embeds WHERE asset_id=? ORDER BY created_at',(id,))]
        return a
    def get(self,id):
        with self.connect() as c: return self._get(c,id)
    def _index(self,c,id):
        a=self._get(c,id); c.execute('DELETE FROM assets_fts WHERE id=?',(id,))
        c.execute('INSERT INTO assets_fts VALUES(?,?,?,?,?,?,?)',(id,a['title'],a['summary'],a['core_value'],a['content_text'],a['user_note'],(a['analysis_json'] or {}).get('report_markdown','')))

    def _state(self,c,key,default='0'):
        row=c.execute('SELECT value FROM sync_state WHERE key=?',(key,)).fetchone(); return row[0] if row else default

    def _set_state(self,c,key,value):
        c.execute('INSERT INTO sync_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

    def _asset_sync_payload(self,c,asset_id):
        asset=dict(c.execute('SELECT * FROM assets WHERE id=?',(asset_id,)).fetchone() or {})
        if not asset: raise Error('not_found','资产不存在')
        tags=[]
        for row in c.execute('SELECT t.*,at.confidence,at.source,at.created_at binding_created_at FROM tags t JOIN asset_tags at ON at.tag_id=t.id WHERE at.asset_id=? ORDER BY t.id',(asset_id,)):
            tag=dict(row); tag['aliases']=[dict(x) for x in c.execute('SELECT * FROM tag_aliases WHERE tag_id=? ORDER BY id',(tag['id'],))]; tags.append(tag)
        embeds=[dict(x) for x in c.execute('SELECT * FROM asset_embeds WHERE asset_id=? ORDER BY id',(asset_id,))]
        blobs=[]
        if asset.get('content_hash') and asset.get('local_path'):
            path=self.root/asset['local_path']
            if path.is_file(): blobs.append({'content_hash':asset['content_hash'],'local_path':asset['local_path'],'size_bytes':path.stat().st_size})
        for embed in embeds:
            blobs.append({'content_hash':embed['content_hash'],'local_path':embed['local_path'],'size_bytes':embed['size_bytes']})
        return {'asset':asset,'tags':tags,'embeds':embeds,'blobs':blobs}

    def _entity_payload(self,c,entity_type,entity_id):
        if entity_type=='asset': return self._asset_sync_payload(c,entity_id)
        if entity_type=='source':
            row=c.execute('SELECT id,source_type,name,url,config_json,enabled,created_at,updated_at FROM discovery_sources WHERE id=?',(entity_id,)).fetchone()
            if not row: raise Error('not_found','发现来源不存在')
            return {'source':dict(row)}
        if entity_type=='rule':
            row=c.execute('SELECT * FROM interest_rules WHERE id=?',(entity_id,)).fetchone()
            if not row: raise Error('not_found','兴趣规则不存在')
            return {'rule':dict(row)}
        if entity_type=='tag_catalog':
            tags=[]
            for row in c.execute('SELECT * FROM tags ORDER BY id'):
                tag=dict(row); tag['aliases']=[dict(x) for x in c.execute('SELECT * FROM tag_aliases WHERE tag_id=? ORDER BY id',(tag['id'],))]; tags.append(tag)
            return {'tags':tags}
        raise Error('validation','同步实体类型无效')

    def _record_sync(self,c,entity_type,entity_id):
        if self._applying_remote or not self.sync_config.get('enabled') or self.sync_config.get('mode')=='standalone': return
        payload=self._entity_payload(c,entity_type,entity_id)
        blobs=payload.get('blobs',[]); device_id=self.sync_config.get('device_id')
        require(bool(device_id),'同步设备 ID 缺失')
        seq=int(self._state(c,'local_device_seq'))+1; self._set_state(c,'local_device_seq',seq)
        event={'op_id':uid(),'device_id':device_id,'device_seq':seq,'entity_type':entity_type,'entity_id':entity_id,'action':'upsert','payload_json':json.dumps(payload,ensure_ascii=False,separators=(',',':')),'blob_hashes_json':json.dumps([x['content_hash'] for x in blobs]),'created_at':now()}
        if self.sync_config['mode']=='server':
            c.execute('INSERT INTO sync_server_events(op_id,device_id,device_seq,entity_type,entity_id,action,payload_json,blob_hashes_json,created_at,received_at) VALUES(:op_id,:device_id,:device_seq,:entity_type,:entity_id,:action,:payload_json,:blob_hashes_json,:created_at,:received_at)',{**event,'received_at':now()})
        else:
            c.execute('INSERT INTO sync_operations(op_id,device_id,device_seq,entity_type,entity_id,action,payload_json,blob_hashes_json,created_at) VALUES(:op_id,:device_id,:device_seq,:entity_type,:entity_id,:action,:payload_json,:blob_hashes_json,:created_at)',event)

    def configure_sync(self,mode,server_url=None,token=None,interval_seconds=30):
        require(mode in {'standalone','server','client'},'同步模式无效')
        require(type(interval_seconds) is int and 5<=interval_seconds<=3600,'同步间隔必须为 5～3600 秒')
        previous=self.sync_config.get('mode')
        if mode=='client':
            checked=canonical(server_url or self.sync_config.get('server_url'))
            require(bool(checked),'客户端需要有效的服务端 URL')
            require(isinstance(token or self.sync_config.get('token'),str) and len(token or self.sync_config.get('token'))>=20,'同步令牌无效')
            server_url=(server_url or self.sync_config.get('server_url')).rstrip('/')
        if mode=='server': token=token or self.sync_config.get('token') or secrets.token_urlsafe(32)
        if mode=='standalone': server_url=None; token=None
        device_id=self.sync_config.get('device_id') if previous==mode else None
        self.sync_config.update({'mode':mode,'device_id':device_id or uid(),'server_url':server_url,'token':token,'interval_seconds':interval_seconds,'enabled':mode!='standalone'})
        self._save_sync_config()
        if mode=='server': self.seed_sync_baseline()
        return self.sync_status()

    def disable_sync(self): return self.configure_sync('standalone')

    def seed_sync_baseline(self):
        if self.sync_config.get('mode')!='server': return {'seeded':0}
        with self.tx() as c:
            if c.execute('SELECT 1 FROM sync_server_events LIMIT 1').fetchone(): return {'seeded':0}
            entities=[('asset',r[0]) for r in c.execute('SELECT id FROM assets')]
            entities += [('source',r[0]) for r in c.execute('SELECT id FROM discovery_sources')]
            entities += [('rule',r[0]) for r in c.execute('SELECT id FROM interest_rules')]
            entities.append(('tag_catalog','all'))
            for kind,entity_id in entities: self._record_sync(c,kind,entity_id)
            return {'seeded':len(entities)}

    def sync_status(self):
        with self.connect() as c:
            pending=c.execute('SELECT count(*) FROM sync_operations WHERE pushed_at IS NULL').fetchone()[0]
            latest=c.execute('SELECT COALESCE(max(server_seq),0) FROM sync_server_events').fetchone()[0]
            values={r['key']:r['value'] for r in c.execute('SELECT * FROM sync_state')}
        return {'mode':self.sync_config.get('mode','standalone'),'enabled':bool(self.sync_config.get('enabled')),'device_id':self.sync_config.get('device_id'),'server_url':self.sync_config.get('server_url'),'interval_seconds':self.sync_config.get('interval_seconds',30),'pending':pending,'cursor':int(values.get('pull_cursor','0')),'server_seq':latest,'last_success_at':values.get('last_success_at'),'last_error':values.get('last_error')}

    def pending_sync_operations(self,limit=100):
        require(type(limit) is int and 1<=limit<=100,'同步批次无效')
        with self.connect() as c: rows=c.execute('SELECT * FROM sync_operations WHERE pushed_at IS NULL ORDER BY device_seq LIMIT ?',(limit,)).fetchall()
        return [self._event_dict(row) for row in rows]

    def _event_dict(self,row):
        item=dict(row); item['payload']=json.loads(item.pop('payload_json')); item['blob_hashes']=json.loads(item.pop('blob_hashes_json')); item.pop('pushed_at',None); item.pop('received_at',None); return item

    def mark_sync_pushed(self,op_ids):
        if not op_ids: return
        with self.tx() as c: c.executemany('UPDATE sync_operations SET pushed_at=? WHERE op_id=?',[(now(),x) for x in op_ids])

    def set_sync_result(self,success,error=None,cursor=None):
        with self.tx() as c:
            self._set_state(c,'last_success_at' if success else 'last_error',now() if success else str(error or '同步失败'))
            if success: self._set_state(c,'last_error','')
            if cursor is not None: self._set_state(c,'pull_cursor',int(cursor))

    def sync_events(self,after=0,limit=100):
        require(type(after) is int and after>=0 and type(limit) is int and 1<=limit<=100,'同步游标无效')
        with self.connect() as c: rows=c.execute('SELECT * FROM sync_server_events WHERE server_seq>? ORDER BY server_seq LIMIT ?',(after,limit)).fetchall()
        return {'events':[self._event_dict(row) for row in rows],'cursor':rows[-1]['server_seq'] if rows else after,'has_more':len(rows)==limit}

    def _safe_blob_target(self,relative):
        require(isinstance(relative,str) and relative.startswith('storage/'),'同步附件路径无效')
        target=(self.root/relative).resolve(); require(target.is_relative_to((self.root/'storage').resolve()),'同步附件路径无效'); return target

    def blob_path(self,digest):
        require(bool(re.fullmatch(r'[0-9a-f]{64}',digest or '')),'附件哈希无效')
        with self.connect() as c:
            row=c.execute('SELECT local_path FROM sync_blobs WHERE content_hash=?',(digest,)).fetchone()
            if not row: row=c.execute('SELECT local_path FROM assets WHERE content_hash=? UNION SELECT local_path FROM asset_embeds WHERE content_hash=? LIMIT 1',(digest,digest)).fetchone()
        if not row: raise Error('not_found','同步附件不存在')
        path=self._safe_blob_target(row[0]); require(path.is_file(),'同步附件文件不存在'); return path

    def put_sync_blob(self,digest,content):
        require(isinstance(content,(bytes,bytearray)) and content,'同步附件不能为空')
        require(len(content)<=self.config['attachment_max_bytes'],f"附件不可超过 {self.config['attachment_max_bytes']//1024//1024} MiB")
        require(hashlib.sha256(content).hexdigest()==digest,'附件哈希校验失败')
        relative=f'storage/.sync/{digest}'; target=self._safe_blob_target(relative); target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_suffix('.tmp'); temp.write_bytes(content); os.replace(temp,target)
        with self.tx() as c: c.execute('INSERT INTO sync_blobs VALUES(?,?,?,?) ON CONFLICT(content_hash) DO UPDATE SET size_bytes=excluded.size_bytes,local_path=excluded.local_path',(digest,len(content),relative,now()))
        return {'content_hash':digest,'size_bytes':len(content)}

    def _validate_sync_event(self,event):
        require(isinstance(event,dict),'同步操作必须为对象')
        require(bool(re.fullmatch(r'[0-9a-f-]{36}',str(event.get('op_id','')))),'同步操作 ID 无效')
        require(bool(re.fullmatch(r'[0-9a-f-]{36}',str(event.get('device_id','')))),'同步设备 ID 无效')
        require(type(event.get('device_seq')) is int and event['device_seq']>0,'同步设备序号无效')
        require(event.get('entity_type') in {'asset','source','rule','tag_catalog'} and isinstance(event.get('entity_id'),str),'同步实体无效')
        require(event.get('action')=='upsert' and isinstance(event.get('payload'),dict),'同步操作无效')
        require(isinstance(event.get('blob_hashes',[]),list) and all(re.fullmatch(r'[0-9a-f]{64}',x or '') for x in event.get('blob_hashes',[])),'附件哈希列表无效')
        require(isinstance(event.get('created_at'),str),'同步时间无效')

    def _materialize_payload_blobs(self,payload):
        for blob in payload.get('blobs',[]):
            require(isinstance(blob,dict) and re.fullmatch(r'[0-9a-f]{64}',blob.get('content_hash','')),'同步附件元数据无效')
            target=self._safe_blob_target(blob.get('local_path'))
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==blob['content_hash']: continue
            source=self.blob_path(blob['content_hash']); target.parent.mkdir(parents=True,exist_ok=True)
            temp=target.with_suffix(target.suffix+'.sync-tmp'); shutil.copyfile(source,temp)
            require(hashlib.sha256(temp.read_bytes()).hexdigest()==blob['content_hash'],'同步附件写入校验失败'); os.replace(temp,target)

    def _apply_event(self,c,event,server_seq):
        if c.execute('SELECT 1 FROM sync_applied WHERE op_id=?',(event['op_id'],)).fetchone(): return False
        payload=event['payload']; kind=event['entity_type']
        if kind=='asset':
            asset=payload.get('asset'); require(isinstance(asset,dict) and asset.get('id')==event['entity_id'],'资产同步内容无效')
            columns=['id','asset_type','title','source_url','canonical_url','local_path','content_hash','summary','core_value','content_text','content_html','user_note','analysis_json','metadata_json','explore_level','status','revision','created_at','updated_at','last_explored_at','last_explore_attempt_at','last_explore_error']
            require(all(column in asset for column in columns),'资产同步字段不完整')
            existing_url=c.execute('SELECT id FROM assets WHERE canonical_url=? AND id<>?',(asset.get('canonical_url'),asset['id'])).fetchone() if asset.get('canonical_url') else None
            existing_hash=c.execute('SELECT id FROM assets WHERE content_hash=? AND id<>?',(asset.get('content_hash'),asset['id'])).fetchone() if asset.get('content_hash') else None
            if existing_url: c.execute('UPDATE assets SET canonical_url=NULL WHERE id=?',(existing_url[0],))
            if existing_hash: c.execute('UPDATE assets SET content_hash=NULL,local_path=NULL WHERE id=?',(existing_hash[0],))
            marks=','.join('?' for _ in columns); updates=','.join(f'{x}=excluded.{x}' for x in columns if x!='id')
            c.execute(f'INSERT INTO assets({",".join(columns)}) VALUES({marks}) ON CONFLICT(id) DO UPDATE SET {updates}',[asset[x] for x in columns])
            c.execute('DELETE FROM asset_tags WHERE asset_id=?',(asset['id'],))
            for tag in payload.get('tags',[]):
                tag_columns=['id','name','normalized_name','facet','parent_id','description','created_at','updated_at']
                c.execute(f'INSERT INTO tags({",".join(tag_columns)}) VALUES({",".join("?" for _ in tag_columns)}) ON CONFLICT(id) DO UPDATE SET name=excluded.name,normalized_name=excluded.normalized_name,facet=excluded.facet,parent_id=excluded.parent_id,description=excluded.description,updated_at=excluded.updated_at',[tag.get(x) for x in tag_columns])
                for alias in tag.get('aliases',[]):
                    c.execute('INSERT INTO tag_aliases(id,tag_id,alias,normalized_alias,created_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET tag_id=excluded.tag_id,alias=excluded.alias,normalized_alias=excluded.normalized_alias',(alias['id'],tag['id'],alias['alias'],alias['normalized_alias'],alias['created_at']))
                c.execute('INSERT INTO asset_tags(asset_id,tag_id,confidence,source,created_at) VALUES(?,?,?,?,?)',(asset['id'],tag['id'],tag['confidence'],tag['source'],tag.get('binding_created_at') or now()))
            c.execute('DELETE FROM asset_embeds WHERE asset_id=?',(asset['id'],))
            for embed in payload.get('embeds',[]):
                columns=['id','asset_id','local_path','content_hash','mime_type','filename','size_bytes','created_at']
                c.execute(f'INSERT INTO asset_embeds({",".join(columns)}) VALUES({",".join("?" for _ in columns)})',[embed[x] for x in columns])
            self._index(c,asset['id'])
        elif kind=='source':
            source=payload.get('source'); require(isinstance(source,dict) and source.get('id')==event['entity_id'],'来源同步内容无效')
            conflict=c.execute('SELECT id FROM discovery_sources WHERE source_type=? AND url=? AND id<>?',(source['source_type'],source['url'],source['id'])).fetchone()
            if conflict: c.execute('DELETE FROM discovery_sources WHERE id=?',(conflict[0],))
            columns=['id','source_type','name','url','config_json','enabled','created_at','updated_at']
            c.execute(f'INSERT INTO discovery_sources({",".join(columns)}) VALUES({",".join("?" for _ in columns)}) ON CONFLICT(id) DO UPDATE SET source_type=excluded.source_type,name=excluded.name,url=excluded.url,config_json=excluded.config_json,enabled=excluded.enabled,updated_at=excluded.updated_at',[source[x] for x in columns])
        elif kind=='rule':
            rule=payload.get('rule'); require(isinstance(rule,dict) and rule.get('id')==event['entity_id'],'规则同步内容无效')
            columns=['id','name','include_keywords_json','exclude_keywords_json','languages_json','topics_json','min_stars','max_age_days','enabled','created_at','updated_at']
            c.execute(f'INSERT INTO interest_rules({",".join(columns)}) VALUES({",".join("?" for _ in columns)}) ON CONFLICT(id) DO UPDATE SET name=excluded.name,include_keywords_json=excluded.include_keywords_json,exclude_keywords_json=excluded.exclude_keywords_json,languages_json=excluded.languages_json,topics_json=excluded.topics_json,min_stars=excluded.min_stars,max_age_days=excluded.max_age_days,enabled=excluded.enabled,updated_at=excluded.updated_at',[rule[x] for x in columns])
        else:
            for tag in payload.get('tags',[]):
                columns=['id','name','normalized_name','facet','parent_id','description','created_at','updated_at']
                c.execute(f'INSERT INTO tags({",".join(columns)}) VALUES({",".join("?" for _ in columns)}) ON CONFLICT(id) DO UPDATE SET name=excluded.name,normalized_name=excluded.normalized_name,facet=excluded.facet,parent_id=excluded.parent_id,description=excluded.description,updated_at=excluded.updated_at',[tag.get(x) for x in columns])
                for alias in tag.get('aliases',[]): c.execute('INSERT OR REPLACE INTO tag_aliases(id,tag_id,alias,normalized_alias,created_at) VALUES(?,?,?,?,?)',(alias['id'],tag['id'],alias['alias'],alias['normalized_alias'],alias['created_at']))
        c.execute('INSERT INTO sync_applied VALUES(?,?,?)',(event['op_id'],server_seq,now())); return True

    def server_push(self,events):
        require(self.sync_config.get('mode')=='server','当前实例不是同步服务端')
        require(isinstance(events,list) and len(events)<=100,'每批最多 100 条同步操作')
        accepted=[]
        for event in events:
            self._validate_sync_event(event)
            with self.connect() as check:
                existing=check.execute('SELECT server_seq FROM sync_server_events WHERE op_id=?',(event['op_id'],)).fetchone()
            if existing: accepted.append({'op_id':event['op_id'],'server_seq':existing[0]}); continue
            self._materialize_payload_blobs(event['payload'])
            with self.tx() as c:
                c.execute('INSERT INTO sync_server_events(op_id,device_id,device_seq,entity_type,entity_id,action,payload_json,blob_hashes_json,created_at,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(event['op_id'],event['device_id'],event['device_seq'],event['entity_type'],event['entity_id'],event['action'],json.dumps(event['payload'],ensure_ascii=False,separators=(',',':')),json.dumps(event.get('blob_hashes',[])),event['created_at'],now()))
                server_seq=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                self._applying_remote=True
                try: self._apply_event(c,event,server_seq)
                finally: self._applying_remote=False
                accepted.append({'op_id':event['op_id'],'server_seq':server_seq})
        return {'accepted':accepted,'server_seq':self.sync_status()['server_seq']}

    def apply_pulled_events(self,events):
        require(isinstance(events,list) and len(events)<=100,'同步事件批次无效')
        applied=0; cursor=int(self.sync_status()['cursor'])
        for event in events:
            self._validate_sync_event(event); server_seq=event.get('server_seq')
            require(type(server_seq) is int and server_seq>cursor,'服务端事件序号无效')
            self._materialize_payload_blobs(event['payload'])
            with self.tx() as c:
                self._applying_remote=True
                try: applied+=int(self._apply_event(c,event,server_seq))
                finally: self._applying_remote=False
                self._set_state(c,'pull_cursor',server_seq); cursor=server_seq
        return {'applied':applied,'cursor':cursor}

    def client_bootstrap_state(self):
        with self.connect() as c:
            initialized=self._state(c,'client_initialized','0')=='1'
            entities={(kind,row[0]) for kind,table in [('asset','assets'),('source','discovery_sources'),('rule','interest_rules')] for row in c.execute(f'SELECT id FROM {table}')}
        return {'initialized':initialized,'entities':entities}

    def finish_client_bootstrap(self,original_entities,remote_entities):
        seeded=0
        with self.tx() as c:
            for kind,entity_id in sorted(set(original_entities)-set(remote_entities)):
                table={'asset':'assets','source':'discovery_sources','rule':'interest_rules'}[kind]
                if c.execute(f'SELECT 1 FROM {table} WHERE id=?',(entity_id,)).fetchone(): self._record_sync(c,kind,entity_id); seeded+=1
            self._set_state(c,'client_initialized','1')
        return seeded
    def _resolve(self,c,name,facet):
        return c.execute('SELECT * FROM tags WHERE facet=? AND normalized_name=? UNION SELECT t.* FROM tags t JOIN tag_aliases a ON a.tag_id=t.id WHERE t.facet=? AND a.normalized_alias=?',(facet,norm(name),facet,norm(name))).fetchone()
    def _tags(self,c,id,tags,source='ai',replace=False):
        require(isinstance(tags,list),'tags 必须是数组')
        if replace: c.execute('DELETE FROM asset_tags WHERE asset_id=?',(id,))
        for t in tags:
            require(isinstance(t,dict),'标签必须为对象')
            name=t.get('name'); facet=t.get('facet'); confidence=t.get('confidence',1)
            require(isinstance(name,str) and norm(name) and facet in FACETS,'标签需要有效 name 和 facet')
            require(isinstance(confidence,(int,float)) and 0<=confidence<=1,'标签置信度必须为 0～1')
            if source=='ai' and confidence<.65: continue
            row=self._resolve(c,name,facet)
            if row: tid=row['id']
            else:
                tid=uid(); c.execute('INSERT INTO tags(id,name,normalized_name,facet,created_at,updated_at) VALUES(?,?,?,?,?,?)',(tid,name.strip(),norm(name),facet,now(),now()))
            c.execute("INSERT INTO asset_tags VALUES(?,?,?,?,?) ON CONFLICT(asset_id,tag_id) DO UPDATE SET source=CASE WHEN asset_tags.source='user' THEN 'user' ELSE excluded.source END,confidence=CASE WHEN asset_tags.source='user' THEN asset_tags.confidence ELSE excluded.confidence END",(id,tid,confidence,source,now()))
    def alias(self,tag_id,alias):
        require(isinstance(alias,str) and norm(alias),'别名不能为空')
        with self.tx() as c:
            row=c.execute('SELECT * FROM tags WHERE id=?',(tag_id,)).fetchone()
            if not row: raise Error('not_found','标签不存在')
            old=self._resolve(c,alias,row['facet'])
            require(not old or old['id']==tag_id,'该别名已指向另一标签')
            c.execute('INSERT OR IGNORE INTO tag_aliases VALUES(?,?,?,?,?)',(uid(),tag_id,alias,norm(alias),now()))
            self._record_sync(c,'tag_catalog','all')
        return {'tag_id':tag_id,'alias':alias}
    def tags(self,q='',status='all'):
        require(status in ('active','archived','all'),'状态无效')
        with self.connect() as c:
            rows=[dict(r) for r in c.execute("SELECT t.*, (SELECT COUNT(*) FROM asset_tags x JOIN assets a ON a.id=x.asset_id WHERE x.tag_id=t.id AND (?='all' OR a.status=?)) usage_count FROM tags t ORDER BY t.facet,t.name",(status,status))]
            for r in rows: r['aliases']=[x[0] for x in c.execute('SELECT alias FROM tag_aliases WHERE tag_id=?',(r['id'],))]
            return [r for r in rows if norm(q) in norm(r['name']+' '+' '.join(r['aliases']))]

    def folders(self,status='active'):
        """Return imported bookmark folders as navigation metadata, separate from tags."""
        require(status in ('active','archived','all'),'状态无效')
        with self.connect() as c:
            where='' if status=='all' else 'WHERE a.status=?'
            params=[] if status=='all' else [status]
            rows=c.execute(f'''SELECT a.metadata_json FROM assets a {where}''',params).fetchall()
        counts={}
        for row in rows:
            for folder in (json.loads(row[0] or '{}').get('bookmark_folders') or []):
                if isinstance(folder,str) and folder.strip(): counts[folder.strip()]=counts.get(folder.strip(),0)+1
        return [{'name':name,'usage_count':count} for name,count in sorted(counts.items(),key=lambda x:(-x[1],norm(x[0])))]

    def discovery_sources(self):
        with self.connect() as c:
            rows=[]
            for row in c.execute('SELECT * FROM discovery_sources ORDER BY source_type,name'):
                item=dict(row); item['config_json']=json.loads(item['config_json'] or '{}'); item['enabled']=bool(item['enabled']); rows.append(item)
            return rows

    def add_discovery_source(self,data):
        require(isinstance(data,dict),'输入必须为对象')
        typ=data.get('source_type'); require(typ in {'rss','atom'},'只支持 RSS 或 Atom 来源')
        name=data.get('name',''); url=canonical(data.get('url'))
        require(isinstance(name,str) and name.strip() and url,'来源名称和 URL 不能为空')
        with self.tx() as c:
            stamp=now(); existing=c.execute('SELECT id FROM discovery_sources WHERE source_type=? AND url=?',(typ,url)).fetchone()
            if existing: return next(x for x in self.discovery_sources() if x['id']==existing['id'])
            source_id=uid(); c.execute('INSERT INTO discovery_sources(id,source_type,name,url,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(source_id,typ,name.strip(),url,'{}',stamp,stamp))
            self._record_sync(c,'source',source_id)
            return dict(c.execute('SELECT * FROM discovery_sources WHERE id=?',(source_id,)).fetchone())

    def update_discovery_source(self,source_id,*,checked_at=None,error=None,etag=None,last_modified=None):
        with self.tx() as c:
            if not c.execute('SELECT 1 FROM discovery_sources WHERE id=?',(source_id,)).fetchone(): raise Error('not_found','发现来源不存在')
            c.execute('UPDATE discovery_sources SET last_checked_at=?,last_error=?,etag=COALESCE(?,etag),last_modified=COALESCE(?,last_modified),updated_at=? WHERE id=?',(checked_at or now(),error,etag,last_modified,now(),source_id))

    def ingest_discovery(self,source_id,items):
        require(isinstance(items,list) and len(items)<=self.config['discovery_max_items'],'发现条目数量超出限制')
        added=updated=0
        with self.tx() as c:
            require(bool(c.execute('SELECT 1 FROM discovery_sources WHERE id=?',(source_id,)).fetchone()),'发现来源不存在')
            for item in items:
                require(isinstance(item,dict),'发现条目必须为对象')
                external=str(item.get('external_id','')).strip(); title=str(item.get('title','')).strip(); url=canonical(item.get('url'))
                typ=item.get('item_type'); require(external and title and url and typ in {'github','article'},'发现条目字段无效')
                metrics=item.get('metrics_json',{}); topics=item.get('topics_json',[])
                require(isinstance(metrics,dict) and isinstance(topics,list),'发现条目的指标或主题无效')
                stamp=now(); old=c.execute('SELECT id,state,asset_id FROM discovery_items WHERE source_id=? AND external_id=?',(source_id,external)).fetchone()
                values=(title,item.get('url'),item.get('summary',''),item.get('author',''),item.get('published_at'),json.dumps(metrics,ensure_ascii=False),json.dumps(topics,ensure_ascii=False),float(item.get('score',0)),stamp)
                if old:
                    c.execute('UPDATE discovery_items SET title=?,url=?,summary=?,author=?,published_at=?,metrics_json=?,topics_json=?,score=?,updated_at=? WHERE id=?',(*values,old['id'])); updated+=1
                else:
                    c.execute('INSERT INTO discovery_items(id,source_id,external_id,item_type,title,url,summary,author,published_at,metrics_json,topics_json,score,state,discovered_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(uid(),source_id,external,typ,*values[:-1],'new',stamp,stamp)); added+=1
            c.execute('UPDATE discovery_sources SET last_checked_at=?,last_error=NULL,updated_at=? WHERE id=?',(now(),now(),source_id))
        return {'added':added,'updated':updated,'total':len(items)}

    def discovery_items(self,item_type=None,state='new',source_id=None,q='',language=None,topic=None,page=1,page_size=30):
        require(item_type in (None,'github','article'),'发现类型无效'); require(state in {'new','interested','dismissed','saved','all'},'发现状态无效')
        require(type(page) is int and page>0 and type(page_size) is int and 1<=page_size<=100,'分页参数无效')
        where=[]; params=[]
        if item_type: where.append('d.item_type=?'); params.append(item_type)
        if state!='all': where.append('d.state=?'); params.append(state)
        if source_id: where.append('d.source_id=?'); params.append(source_id)
        if q.strip(): where.append("norm(d.title||' '||d.summary||' '||d.author||' '||d.topics_json) LIKE ?"); params.append('%'+norm(q)+'%')
        if language: where.append("norm(COALESCE(json_extract(d.metrics_json,'$.language'),''))=?"); params.append(norm(language))
        if topic: where.append("EXISTS (SELECT 1 FROM json_each(d.topics_json) j WHERE norm(j.value)=?)"); params.append(norm(topic))
        clause=' WHERE '+' AND '.join(where) if where else ''
        with self.connect() as c:
            total=c.execute('SELECT count(*) FROM discovery_items d'+clause,params).fetchone()[0]
            rows=c.execute('SELECT d.*,s.name source_name FROM discovery_items d JOIN discovery_sources s ON s.id=d.source_id'+clause+' ORDER BY d.score DESC,COALESCE(d.published_at,d.discovered_at) DESC LIMIT ? OFFSET ?',[*params,page_size,(page-1)*page_size]).fetchall()
            items=[]
            for row in rows:
                item=dict(row); item['metrics_json']=json.loads(item['metrics_json'] or '{}'); item['topics_json']=json.loads(item['topics_json'] or '[]'); items.append(item)
            return {'items':items,'total':total,'page':page,'page_size':page_size}

    def discovery_facets(self,state='new'):
        require(state in {'new','interested','dismissed','saved','all'},'发现状态无效')
        clause='' if state=='all' else ' WHERE state=?'; params=[] if state=='all' else [state]
        with self.connect() as c:
            languages=[{'name':r['name'],'count':r['count']} for r in c.execute("SELECT json_extract(metrics_json,'$.language') name,count(*) count FROM discovery_items"+clause+" GROUP BY name HAVING name IS NOT NULL AND name!='' ORDER BY count DESC,name LIMIT 30",params)]
            topic_clause='' if state=='all' else ' WHERE d.state=?'
            topics=[{'name':r['name'],'count':r['count']} for r in c.execute("SELECT j.value name,count(*) count FROM discovery_items d,json_each(d.topics_json) j"+topic_clause+" GROUP BY j.value ORDER BY count DESC,j.value LIMIT 40",params)]
            return {'languages':languages,'topics':topics}

    def set_discovery_state(self,item_id,state):
        require(state in {'new','interested','dismissed'},'发现状态无效')
        with self.tx() as c:
            if not c.execute('SELECT 1 FROM discovery_items WHERE id=?',(item_id,)).fetchone(): raise Error('not_found','发现条目不存在')
            c.execute('UPDATE discovery_items SET state=?,updated_at=? WHERE id=?',(state,now(),item_id))
        return self.discovery_item(item_id)

    def discovery_item(self,item_id):
        with self.connect() as c:
            row=c.execute('SELECT d.*,s.name source_name FROM discovery_items d JOIN discovery_sources s ON s.id=d.source_id WHERE d.id=?',(item_id,)).fetchone()
            if not row: raise Error('not_found','发现条目不存在')
            item=dict(row); item['metrics_json']=json.loads(item['metrics_json'] or '{}'); item['topics_json']=json.loads(item['topics_json'] or '[]'); return item

    def save_discovery_item(self,item_id):
        item=self.discovery_item(item_id)
        if item['asset_id']:
            return {'asset':self.get(item['asset_id']),'duplicate':True,'discovery_item':item}
        metadata={'discovery_source':item['source_name'],'discovery_metrics':item['metrics_json'],'discovery_topics':item['topics_json'],'published_at':item['published_at'],'author':item['author']}
        tags=[]
        for topic in item['topics_json'][:8]:
            if isinstance(topic,str) and topic.strip(): tags.append({'name':topic,'facet':'technology' if item['item_type']=='github' else 'domain','confidence':1})
        result=self.save({'title':item['title'],'asset_type':item['item_type'],'source_url':item['url'],'summary':item['summary'],'core_value':'来自发现页，等待进一步判断。','metadata_json':metadata,'tags':tags,'explore_level':'basic'},'user')
        with self.tx() as c: c.execute("UPDATE discovery_items SET state='saved',asset_id=?,updated_at=? WHERE id=?",(result['asset']['id'],now(),item_id))
        result['discovery_item']=self.discovery_item(item_id); return result

    def interest_rules(self):
        with self.connect() as c:
            rows=[]
            for row in c.execute('SELECT * FROM interest_rules ORDER BY created_at'):
                item=dict(row)
                for key in ('include_keywords_json','exclude_keywords_json','languages_json','topics_json'): item[key]=json.loads(item[key] or '[]')
                item['enabled']=bool(item['enabled']); rows.append(item)
            return rows

    def save_interest_rule(self,data):
        require(isinstance(data,dict),'输入必须为对象'); name=str(data.get('name','')).strip(); require(name,'规则名称不能为空')
        fields=[]
        for key in ('include_keywords','exclude_keywords','languages','topics'):
            value=data.get(key,[]); require(isinstance(value,list) and all(isinstance(x,str) for x in value),key+' 必须是文字数组'); fields.append(json.dumps([x.strip() for x in value if x.strip()],ensure_ascii=False))
        min_stars=data.get('min_stars',0); max_age=data.get('max_age_days')
        require(type(min_stars) is int and min_stars>=0 and (max_age is None or type(max_age) is int and max_age>0),'规则数值无效')
        with self.tx() as c:
            rule_id=uid(); stamp=now(); c.execute('INSERT INTO interest_rules VALUES(?,?,?,?,?,?,?,?,?,?,?)',(rule_id,name,*fields,min_stars,max_age,1,stamp,stamp))
            self._record_sync(c,'rule',rule_id)
        return next(x for x in self.interest_rules() if x['id']==rule_id)
    def save(self,data,source='ai'):
        require(isinstance(data,dict),'输入必须为对象'); require(source in ('ai','user','import'),'无效来源')
        allowed={'title','asset_type','source_url','file_path','summary','core_value','content_text','content_html','user_note','metadata_json','tags','explore_level'}
        require(not set(data)-allowed,'未知或只读字段: '+','.join(set(data)-allowed))
        url=canonical(data.get('source_url')); typ=data.get('asset_type',infer(url) if url else 'idea'); require(typ in TYPES,'无效资产类型')
        title=data.get('title')
        if typ=='capture' and (not isinstance(title,str) or not title.strip()) and data.get('file_path'):
            original=data.get('metadata_json',{}).get('filename') if isinstance(data.get('metadata_json',{}),dict) else None
            title=Path(original or data['file_path']).stem or '待处理素材'
        require(isinstance(title,str) and title.strip(),'标题不能为空')
        level=data.get('explore_level','none' if typ=='capture' else 'basic'); require(level in ('none','basic'),'SAVE 不可设置 deep')
        for k in ('summary','core_value','content_text','content_html','user_note'): require(isinstance(data.get(k,''),str),k+' 必须为文字')
        require(isinstance(data.get('metadata_json',{}),dict),'metadata_json 必须是对象')
        folders=data.get('metadata_json',{}).get('bookmark_folders',[])
        require(isinstance(folders,list) and all(isinstance(x,str) for x in folders),'bookmark_folders 必须为字符串数组')
        path=None; digest=None; file=None
        if data.get('file_path'):
            file=Path(data['file_path']).resolve()
            if not file.is_file(): raise Error('file','附件文件不存在')
            if file.stat().st_size>self.config['attachment_max_bytes']: raise Error('file',f"附件不可超过 {self.config['attachment_max_bytes']//1024//1024} MiB")
            digest=hashlib.sha256(file.read_bytes()).hexdigest()
            suffix=file.suffix.lower() if re.fullmatch(r'\.[a-zA-Z0-9]{1,8}',file.suffix) else '.bin'
            folder='images' if typ=='image' else 'captures' if typ=='capture' else 'html' if typ=='html' else 'documents'
            path=f'storage/{folder}/{digest}{suffix}'
            data=dict(data)
            data['metadata_json']=dict(data.get('metadata_json',{}))
            data['metadata_json'].setdefault('filename',file.name)
            if not data.get('content_text') and typ=='html':
                parser=TextExtractor(); parser.feed(file.read_text(encoding='utf-8',errors='replace'))
                data['content_text']=''.join(parser.parts).strip()
            if not data.get('content_text') and typ=='pdf':
                try:
                    from pypdf import PdfReader
                    data['content_text']='\n'.join(p.extract_text() or '' for p in PdfReader(file).pages).strip()
                    if not data['content_text']: data['metadata_json']['extraction_warning']='未提取到文本，已保留原文件'
                except Exception:
                    data['metadata_json']['extraction_warning']='无法提取 PDF 正文，已保留原文件'
        with self.tx() as c:
            rows=c.execute('SELECT id FROM assets WHERE canonical_url=? OR content_hash=?',(url,digest)).fetchall()
            require(len(rows)<2,'URL 和附件分别对应不同资产，需人工合并')
            duplicate=bool(rows); id=rows[0]['id'] if rows else uid()
            content_html=sanitize_rich_html(data.get('content_html',''),id) if data.get('content_html') else ''
            if content_html and not data.get('content_text'): data['content_text']=rich_text_plain(content_html)
            if duplicate:
                old=self._get(c,id)
                if not old['canonical_url'] and url:
                    c.execute('UPDATE assets SET source_url=?,canonical_url=? WHERE id=?',(data.get('source_url'),url,id))
                if not old['local_path'] and path:
                    dest=self.root/path; dest.parent.mkdir(parents=True,exist_ok=True)
                    if not dest.exists(): shutil.copyfile(file,dest)
                    c.execute('UPDATE assets SET local_path=?,content_hash=? WHERE id=?',(path,digest,id))
                if old['explore_level']=='none' and level=='basic':
                    c.execute("UPDATE assets SET explore_level='basic' WHERE id=?",(id,))
                for k in ('summary','core_value','content_text'):
                    if not old[k] and data.get(k): c.execute(f'UPDATE assets SET {k}=? WHERE id=?',(data[k],id))
                if not old.get('content_html') and content_html: c.execute('UPDATE assets SET content_html=? WHERE id=?',(content_html,id))
                meta=old['metadata_json']
                incoming=data.get('metadata_json',{})
                for k,v in incoming.items():
                    if k=='bookmark_folders': meta[k]=sorted(set(meta.get(k,[])+v))
                    elif k not in meta: meta[k]=v
                c.execute('UPDATE assets SET metadata_json=?,revision=revision+1,updated_at=? WHERE id=?',(json.dumps(meta,ensure_ascii=False),now(),id))
            else:
                if file:
                    dest=self.root/path; dest.parent.mkdir(parents=True,exist_ok=True)
                    if not dest.exists(): shutil.copyfile(file,dest)
                c.execute('INSERT INTO assets(id,asset_type,title,source_url,canonical_url,local_path,content_hash,summary,core_value,content_text,content_html,user_note,metadata_json,explore_level,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(id,typ,title.strip(),data.get('source_url'),url,path,digest,data.get('summary',''),data.get('core_value',''),data.get('content_text',''),content_html,data.get('user_note',''),json.dumps(data.get('metadata_json',{}),ensure_ascii=False),level,now(),now()))
            self._tags(c,id,data.get('tags',[]),source); self._index(c,id); self._record_sync(c,'asset',id)
            return {'asset':self._get(c,id),'duplicate':duplicate}
    def update(self,id,data):
        require(isinstance(data,dict),'输入必须为对象')
        allowed_fields={'revision','title','summary','core_value','content_text','content_html','user_note','metadata_json','analysis_json','explore_level','status','asset_type','source_url','canonical_url','tags'}
        require(not set(data)-allowed_fields,'包含不可修改的字段')
        require(type(data.get('revision')) is int,'必须携带 revision')
        with self.tx() as c:
            a=self._get(c,id)
            if a['revision']!=data['revision']: raise Error('conflict','资产已更新，请重新加载后编辑')
            for k in ('title','summary','core_value','content_text','user_note','status','explore_level','asset_type'):
                if k in data:
                    require(isinstance(data[k],str),k+' 必须为文字')
                    if k=='title': require(data[k].strip(),'标题不能为空')
                    if k=='status': require(data[k] in ('active','archived'),'状态无效')
                    if k=='explore_level': require(data[k] in ('none','basic','deep'),'探索级别无效')
                    if k=='asset_type': require(data[k] in TYPES,'资产类型无效')
                    c.execute(f'UPDATE assets SET {k}=? WHERE id=?',(data[k],id))
            for k in ('source_url','canonical_url'):
                if k in data:
                    require(data[k] is None or isinstance(data[k],str),k+' 必须为 URL 或空')
                    value=(data[k] or '').strip() or None
                    if value:
                        checked=canonical(value)
                        value=checked if k=='canonical_url' else value
                    c.execute(f'UPDATE assets SET {k}=? WHERE id=?',(value,id))
            if 'content_html' in data:
                cleaned=sanitize_rich_html(data['content_html'],id)
                c.execute('UPDATE assets SET content_html=?,content_text=? WHERE id=?',(cleaned,rich_text_plain(cleaned),id))
            for k in ('metadata_json','analysis_json'):
                if k in data:
                    require(isinstance(data[k],(dict,type(None))),k+' 必须为对象或空')
                    c.execute(f'UPDATE assets SET {k}=? WHERE id=?',(json.dumps(data[k],ensure_ascii=False) if data[k] is not None else None,id))
            if 'tags' in data: self._tags(c,id,data['tags'],'user',True)
            c.execute('UPDATE assets SET revision=revision+1,updated_at=? WHERE id=?',(now(),id)); self._index(c,id); self._record_sync(c,'asset',id)
            return self._get(c,id)

    def add_embed(self,asset_id,filename,content,mime_type=''):
        require(isinstance(content,(bytes,bytearray)) and content,'图片不能为空')
        require(len(content)<=self.config['attachment_max_bytes'],f"图片不可超过 {self.config['attachment_max_bytes']//1024//1024} MiB")
        signatures=[('image/png',b'\x89PNG\r\n\x1a\n','.png'),('image/jpeg',b'\xff\xd8\xff','.jpg'),('image/gif',b'GIF87a','.gif'),('image/gif',b'GIF89a','.gif')]
        detected=next(((kind,suffix) for kind,sig,suffix in signatures if content.startswith(sig)),None)
        if not detected and content.startswith(b'RIFF') and len(content)>=12 and content[8:12]==b'WEBP': detected=('image/webp','.webp')
        require(detected is not None,'仅支持 PNG、JPEG、GIF 或 WebP 图片')
        kind,suffix=detected; digest=hashlib.sha256(content).hexdigest(); embed_id=uid(); rel=f'storage/embeds/{asset_id}/{digest}{suffix}'; dest=self.root/rel
        with self.tx() as c:
            self._get(c,asset_id)
            old=c.execute('SELECT * FROM asset_embeds WHERE asset_id=? AND content_hash=?',(asset_id,digest)).fetchone()
            if old: row=dict(old)
            else:
                dest.parent.mkdir(parents=True,exist_ok=True)
                if not dest.exists(): dest.write_bytes(content)
                c.execute('INSERT INTO asset_embeds VALUES(?,?,?,?,?,?,?,?)',(embed_id,asset_id,rel,digest,kind,Path(filename or 'image'+suffix).name,len(content),now()))
                row=dict(c.execute('SELECT * FROM asset_embeds WHERE id=?',(embed_id,)).fetchone())
                self._record_sync(c,'asset',asset_id)
        row['url']=f'/api/assets/{asset_id}/embeds/{row["id"]}'; return row

    def add_embed_from_path(self,asset_id,value):
        """Copy a local Markdown image reference into managed Silo storage."""
        require(isinstance(value,str) and value.strip(),'本地图片路径不能为空')
        raw=unquote(value.strip())
        require(raw.startswith(('/', '~/')),'本地图片必须使用绝对路径')
        source=Path(os.path.expanduser(raw)).resolve()
        allowed_roots=(Path.home().resolve(),self.root.resolve())
        require(any(source.is_relative_to(root) for root in allowed_roots),'仅允许导入用户主目录或 Silo 目录中的图片')
        require(source.is_file(),'本地图片不存在或无法读取')
        require(source.stat().st_size<=self.config['attachment_max_bytes'],f"图片不可超过 {self.config['attachment_max_bytes']//1024//1024} MiB")
        return self.add_embed(asset_id,source.name,source.read_bytes())

    def embed(self,asset_id,embed_id):
        with self.connect() as c: row=c.execute('SELECT * FROM asset_embeds WHERE id=? AND asset_id=?',(embed_id,asset_id)).fetchone()
        if not row: raise Error('not_found','富文本图片不存在')
        item=dict(row); path=(self.root/item['local_path']).resolve()
        require(path.is_relative_to((self.root/'storage').resolve()) and path.is_file(),'富文本图片文件不存在')
        return item,path

    def bulk_update(self,data):
        """Atomically apply an explicit user action to a bounded set of assets."""
        require(isinstance(data,dict),'输入必须为对象')
        require(not set(data)-{'items','status','add_tags','remove_tag_ids'},'批量操作包含未知字段')
        items=data.get('items'); require(isinstance(items,list) and 1<=len(items)<=100,'每次请选择 1～100 项资产')
        require(all(isinstance(x,dict) and isinstance(x.get('id'),str) and type(x.get('revision')) is int for x in items),'资产需要 id 和 revision')
        require(len({x['id'] for x in items})==len(items),'资产 ID 不可重复')
        status=data.get('status'); require(status is None or status in ('active','archived'),'状态无效')
        add_tags=data.get('add_tags',[]); remove_ids=data.get('remove_tag_ids',[])
        require(isinstance(add_tags,list) and isinstance(remove_ids,list) and all(isinstance(x,str) for x in remove_ids),'标签参数无效')
        require(status is not None or add_tags or remove_ids,'没有可执行的批量操作')
        with self.tx() as c:
            assets=[]
            for item in items:
                a=self._get(c,item['id'])
                if a['revision']!=item['revision']: raise Error('conflict',f"{a['title']} 已更新，请重新加载后再批量操作")
                assets.append(a)
            for a in assets:
                if status is not None: c.execute('UPDATE assets SET status=? WHERE id=?',(status,a['id']))
                if add_tags: self._tags(c,a['id'],add_tags,'user')
                if remove_ids:
                    c.execute('DELETE FROM asset_tags WHERE asset_id=? AND tag_id IN ('+','.join('?' for _ in remove_ids)+')',[a['id'],*remove_ids])
                c.execute('UPDATE assets SET revision=revision+1,updated_at=? WHERE id=?',(now(),a['id']))
                self._index(c,a['id'])
                self._record_sync(c,'asset',a['id'])
            return {'updated':len(assets),'items':[self._get(c,a['id']) for a in assets]}
    def explore(self,id,data):
        require(isinstance(data,dict),'输入必须为对象')
        with self.tx() as c:
            a=self._get(c,id)
            if data.get('revision')!=a['revision']: raise Error('conflict','资产已更新，请重新读取后提交探索')
            if data.get('error'):
                require(isinstance(data['error'],str),'error 必须为文字')
                c.execute('UPDATE assets SET last_explore_error=?,last_explore_attempt_at=?,updated_at=?,revision=revision+1 WHERE id=?',(data['error'],now(),now(),id))
            else:
                r=data.get('analysis_json'); require(isinstance(r,dict),'需要 analysis_json')
                require(r.get('schema_version')==1 and r.get('asset_type')==a['asset_type'],'报告版本或类型无效')
                require(isinstance(r.get('report_markdown'),str) and r['report_markdown'].strip(),'需要完整报告正文')
                require(isinstance(r.get('sources'),list) and isinstance(r.get('unknowns'),list),'sources 和 unknowns 必须为数组')
                require(isinstance(r.get('generated_at'),str),'需要 generated_at')
                try: require(datetime.fromisoformat(r['generated_at'].replace('Z','+00:00')).tzinfo is not None,'报告时间必须包含时区')
                except ValueError: raise Error('validation','报告时间无效')
                require(r.get('recommendation') in {'use_now','try','keep','watch','archive'},'推荐动作无效')
                require(isinstance(r.get('positioning'),str),'需要 positioning')
                require(isinstance(r.get('personal_value'),str),'需要 personal_value')
                require(all(isinstance(x,str) for x in r['unknowns']),'unknowns 必须为字符串数组')
                for k in ('core_features','use_cases','strengths','limitations','alternatives'): require(isinstance(r.get(k),list),k+' 必须为数组')
                if a['asset_type']=='github':
                    for k in ('problems_solved','tech_stack','usage_modes'): require(isinstance(r.get(k),list),k+' 必须为数组')
                    require(r.get('learning_cost') in ('low','medium','high','unknown'),'上手成本无效')
                    require(isinstance(r.get('maturity'),dict),'需要 maturity')
                for s in r['sources']:
                    require(isinstance(s,dict) and isinstance(s.get('title'),str) and isinstance(s.get('accessed_at'),str),'来源需要标题和访问时间'); require(bool(canonical(s.get('url'))),'来源 URL 不能为空')
                    try: require(datetime.fromisoformat(s['accessed_at'].replace('Z','+00:00')).tzinfo is not None,'来源时间必须包含时区')
                    except ValueError: raise Error('validation','来源时间无效')
                c.execute("UPDATE assets SET analysis_json=?,explore_level='deep',last_explored_at=?,last_explore_attempt_at=?,last_explore_error=NULL,updated_at=?,revision=revision+1 WHERE id=?",(json.dumps(r,ensure_ascii=False),now(),now(),now(),id))
                for k in ('summary','core_value'):
                    if k in data:
                        require(isinstance(data[k],str),k+' 必须为文字'); c.execute(f'UPDATE assets SET {k}=? WHERE id=?',(data[k],id))
                self._tags(c,id,data.get('tags',[]),'ai')
            self._index(c,id); self._record_sync(c,'asset',id); return self._get(c,id)
    def search(self,q='',asset_type=None,status='active',explore_level=None,tags=None,folders=None,page=1,page_size=30):
        require(isinstance(q,str) and len(q)<=1000,'查询过长'); require(type(page) is int and page>=1 and type(page_size) is int and 1<=page_size<=100,'分页无效')
        require(status in ('active','archived','all'),'状态无效')
        require(asset_type is None or asset_type in TYPES,'类型无效'); require(explore_level is None or explore_level in LEVELS,'探索级别无效')
        tokens=norm(q).split(); conditions=[]; params=[]
        for k,v in [('asset_type',asset_type),('status',None if status=='all' else status),('explore_level',explore_level)]:
            if v: conditions.append('a.'+k+'=?'); params.append(v)
        if tags:
            conditions.append('EXISTS(SELECT 1 FROM asset_tags at WHERE at.asset_id=a.id AND at.tag_id IN ('+','.join('?' for _ in tags)+'))'); params.extend(tags)
        if folders:
            require(isinstance(folders,list) and all(isinstance(x,str) for x in folders),'收藏目录筛选无效')
            conditions.append('EXISTS(SELECT 1 FROM json_each(a.metadata_json,\'$.bookmark_folders\') f WHERE f.value IN ('+','.join('?' for _ in folders)+'))'); params.extend(folders)
        tagtext="COALESCE((SELECT group_concat(t.name||' '||COALESCE((SELECT group_concat(alias,' ') FROM tag_aliases WHERE tag_id=t.id),''),' ') FROM tags t JOIN asset_tags x ON x.tag_id=t.id WHERE x.asset_id=a.id),'')"
        report="COALESCE(json_extract(a.analysis_json,'$.report_markdown'),'')"
        text_expr="norm(a.title||' '||a.summary||' '||a.core_value||' '||a.content_text||' '||a.user_note||' '||"+report+"||' '||"+tagtext+")"
        for t in tokens:
            conditions.append(f'(instr({text_expr},?)>0 OR a.id IN (SELECT id FROM assets_fts WHERE assets_fts MATCH ?))'); params.extend([t,'"'+t.replace('"','""')+'"'])
        where=' AND '.join(conditions) or '1'
        with self.connect() as c:
            total=c.execute('SELECT count(*) FROM assets a WHERE '+where,params).fetchone()[0]
            rank=f"CASE WHEN norm(a.title)=? THEN 0 WHEN instr(norm(a.title),?)>0 THEN 1 WHEN instr(norm({tagtext}),?)>0 THEN 2 WHEN instr(norm(a.summary||' '||a.core_value),?)>0 THEN 3 ELSE 4 END"
            ids=c.execute(f'SELECT a.id FROM assets a WHERE {where} ORDER BY {rank},a.updated_at DESC,a.id LIMIT ? OFFSET ?',params+[norm(q)]*4+[page_size,(page-1)*page_size]).fetchall()
            items=[self._get(c,r[0]) for r in ids]
            for a in items:
                a['match_excerpt']=a['summary']
                for field in [a['title'],' '.join(t['name'] for t in a['tags']),a['summary'],a['core_value'],a['content_text'],a['user_note'],(a['analysis_json'] or {}).get('report_markdown','')]:
                    if tokens and any(t in norm(field) for t in tokens):
                        pos=next((norm(field).find(t) for t in tokens if t in norm(field)),0); a['match_excerpt']=field[max(0,pos-30):pos+180]; break
            return {'items':items,'total':total,'page':page,'page_size':page_size}
    def import_bookmarks(self,content,preview=False):
        require(len(content)<=self.config['import_max_bytes'],'收藏夹超过文件大小限制')
        try: text=content.decode('utf-8-sig')
        except UnicodeDecodeError: raise Error('validation','收藏夹必须为 UTF-8 编码')
        p=BookmarkParser(); p.feed(text); require(len(p.rows)<=self.config['import_max_items'],'收藏夹超过条数限制')
        require(bool(p.rows),'未找到收藏链接')
        result={'total':len(p.rows),'added':0,'duplicate':0,'failed':0,'errors':[],'preview':preview}; seen=set()
        with self.connect() as c: seen.update(r[0] for r in c.execute('SELECT canonical_url FROM assets WHERE canonical_url IS NOT NULL'))
        for i,row in enumerate(p.rows,1):
            try:
                url=canonical(row['source_url']); require(bool(url),'缺少 URL')
                if preview: duplicate=url in seen
                else:
                    duplicate=self.save({'title':row['title'],'source_url':row['source_url'],'asset_type':infer(url),'explore_level':'none','tags':classify(row['title'],url,[row['folder']]),'metadata_json':{'original_title':row['title'],'bookmark_folders':[row['folder']]}},'import')['duplicate']
                seen.add(url); result['duplicate' if duplicate else 'added']+=1
            except Error as e: result['failed']+=1; result['errors'].append({'row':i,'url':row['source_url'],'error':e.message})
        return result
    def classify_bookmarks(self,preview=False):
        """Add basic tags only to imported assets with no tags; preserve all existing work."""
        result={'preview':preview,'updated':0,'categories':{}}
        with self.tx() as c:
            rows=c.execute("SELECT a.* FROM assets a WHERE NOT EXISTS(SELECT 1 FROM asset_tags x WHERE x.asset_id=a.id)").fetchall()
            for row in rows:
                meta=json.loads(row['metadata_json'])
                if 'bookmark_folders' not in meta: continue
                tags=classify(row['title'],row['source_url'],meta.get('bookmark_folders',[]))
                result['updated']+=1
                for tag in tags: result['categories'][tag['name']]=result['categories'].get(tag['name'],0)+1
                if not preview:
                    self._tags(c,row['id'],tags,'import')
                    c.execute('UPDATE assets SET revision=revision+1 WHERE id=?',(row['id'],))
                    self._record_sync(c,'asset',row['id'])
        return result
    def attachment(self,id):
        a=self.get(id)
        if not a['local_path']: raise Error('not_found','没有附件')
        path=(self.root/a['local_path']).resolve()
        if not path.is_relative_to((self.root/'storage').resolve()) or not path.is_file(): raise Error('file','附件路径无效或文件不存在')
        return path
    def reindex(self):
        with self.tx() as c:
            ids=[r[0] for r in c.execute('SELECT id FROM assets')]
            c.execute('DELETE FROM assets_fts')
            for id in ids: self._index(c,id)
        return {'indexed':len(ids)}
    def backup(self,destination):
        dest=Path(destination).resolve(); require(not dest.exists(),'备份目标必须尚不存在'); require(not dest.is_relative_to(self.root),'备份目标必须位于数据目录之外')
        dest.mkdir(parents=True); (dest/'data').mkdir()
        with self.tx() as source:
            # Hold writer lock while backing up via a separate reader and copying attachments.
            with self.connect() as reader, sqlite3.connect(dest/'data/assets.db') as target: reader.backup(target)
            if (self.root/'storage').exists(): shutil.copytree(self.root/'storage',dest/'storage')
            shutil.copyfile(self.root/'data/config.json',dest/'data/config.json')
            if self.sync_file.exists(): shutil.copyfile(self.sync_file,dest/'data/sync.json')
        return {'path':str(dest)}

    def stats(self):
        with self.connect() as c:
            assets=c.execute('SELECT count(*) FROM assets').fetchone()[0]
            archived=c.execute("SELECT count(*) FROM assets WHERE status='archived'").fetchone()[0]
            attachments=c.execute('SELECT count(*) FROM assets WHERE local_path IS NOT NULL').fetchone()[0]+c.execute('SELECT count(*) FROM asset_embeds').fetchone()[0]
        storage=sum(p.stat().st_size for p in (self.root/'storage').rglob('*') if p.is_file()) if (self.root/'storage').exists() else 0
        return {'assets':assets,'active':assets-archived,'archived':archived,'attachments':attachments,'storage_bytes':storage}

    def export_archive(self,destination):
        """Create a portable data-only ZIP using SQLite's consistent backup API."""
        dest=Path(destination).resolve(); require(not dest.exists(),'导出目标必须尚不存在'); require(not dest.is_relative_to(self.root),'导出目标必须位于 Silo 目录之外')
        dest.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='silo-export-') as temp:
            stage=Path(temp)/'snapshot'; self.backup(stage)
            stats=self.stats()
            manifest={'format':'silo-data-backup','format_version':1,'schema_version':2,'created_at':now(),**stats}
            (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            with zipfile.ZipFile(dest,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
                for path in sorted(stage.rglob('*')):
                    if path.is_file(): archive.write(path,path.relative_to(stage).as_posix())
        return {'path':str(dest),'manifest':manifest}

    def _validated_archive(self,source,temp_root):
        source=Path(source).resolve(); require(source.is_file(),'备份文件不存在')
        require(source.stat().st_size<=self.config['backup_max_bytes'],'备份文件超过大小限制')
        try: archive=zipfile.ZipFile(source)
        except (zipfile.BadZipFile,OSError) as e: raise Error('file','不是有效的 Silo 备份包') from e
        with archive:
            infos=archive.infolist(); require(len(infos)<=100000,'备份文件数量异常')
            require(sum(x.file_size for x in infos)<=self.config['backup_max_bytes'],'备份解压后超过大小限制')
            for info in infos:
                path=Path(info.filename)
                require(not path.is_absolute() and '..' not in path.parts,'备份包含不安全路径')
                require(info.filename=='manifest.json' or info.filename.startswith('data/') or info.filename.startswith('storage/'),'备份包含未知内容')
                target=(temp_root/path).resolve(); require(target.is_relative_to(temp_root.resolve()),'备份路径无效')
                if info.is_dir(): target.mkdir(parents=True,exist_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with archive.open(info) as reader,target.open('wb') as writer: shutil.copyfileobj(reader,writer)
        manifest_path=temp_root/'manifest.json'; db=temp_root/'data/assets.db'; config=temp_root/'data/config.json'
        require(manifest_path.is_file() and db.is_file() and config.is_file(),'备份缺少必要文件')
        try: manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError,UnicodeDecodeError) as e: raise Error('validation','备份清单无效') from e
        require(manifest.get('format')=='silo-data-backup' and manifest.get('format_version')==1,'备份格式不受支持')
        try:
            restored_config=json.loads(config.read_text(encoding='utf-8'))
            require(isinstance(restored_config,dict),'备份配置无效')
            with sqlite3.connect(db) as c:
                require(c.execute('PRAGMA integrity_check').fetchone()[0]=='ok','备份数据库完整性检查失败')
                require(c.execute('PRAGMA user_version').fetchone()[0] in (1,2),'备份数据库版本不受支持')
                tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
                require({'assets','tags','tag_aliases','asset_tags','assets_fts'}.issubset(tables),'备份数据库结构不完整')
                count=c.execute('SELECT count(*) FROM assets').fetchone()[0]
                require(count==manifest.get('assets'),'备份资产数量与清单不一致')
                paths=[r[0] for r in c.execute('SELECT local_path FROM assets WHERE local_path IS NOT NULL')]
                if 'asset_embeds' in tables: paths += [r[0] for r in c.execute('SELECT local_path FROM asset_embeds')]
            for value in paths:
                path=(temp_root/value).resolve()
                require(path.is_relative_to((temp_root/'storage').resolve()) and path.is_file(),'备份缺少附件或附件路径无效')
        except sqlite3.DatabaseError as e: raise Error('validation','备份数据库无法读取') from e
        return manifest,restored_config

    def _replace_snapshot(self,stage,restored_config):
        """Replace data while excluding all concurrent Store readers and writers."""
        with self._io_lock, tempfile.TemporaryDirectory(prefix='silo-rollback-') as rollback_name:
            rollback=Path(rollback_name); self.backup(rollback/'snapshot')
            try:
                incoming=self.root/'data/.assets.restore.db'; shutil.copyfile(stage/'data/assets.db',incoming)
                for suffix in ('-wal','-shm'):
                    (Path(str(self.db)+suffix)).unlink(missing_ok=True)
                os.replace(incoming,self.db)
                shutil.copyfile(stage/'data/config.json',self.root/'data/config.json')
                restored_sync=stage/'data/sync.json'
                if restored_sync.exists():
                    sync_data=json.loads(restored_sync.read_text(encoding='utf-8')); sync_data['enabled']=False
                    self.sync_file.write_text(json.dumps(sync_data,ensure_ascii=False,indent=2),encoding='utf-8'); os.chmod(self.sync_file,0o600)
                else: self.sync_file.unlink(missing_ok=True)
                storage=self.root/'storage'
                if storage.exists(): shutil.rmtree(storage)
                if (stage/'storage').exists(): shutil.copytree(stage/'storage',storage)
                else: storage.mkdir(parents=True)
                self.config=dict(self.defaults); self.config.update(restored_config)
                self.sync_config=self._load_sync_config()
                with self.connect() as c:
                    self._ensure_rich_content_schema(c)
                    self._ensure_sync_schema(c); c.execute('PRAGMA user_version=2')
                    require(c.execute('PRAGMA integrity_check').fetchone()[0]=='ok','恢复后的数据库校验失败')
            except Exception:
                snapshot=rollback/'snapshot'
                shutil.copyfile(snapshot/'data/assets.db',self.db)
                shutil.copyfile(snapshot/'data/config.json',self.root/'data/config.json')
                if (snapshot/'data/sync.json').exists(): shutil.copyfile(snapshot/'data/sync.json',self.sync_file)
                else: self.sync_file.unlink(missing_ok=True)
                storage=self.root/'storage'
                if storage.exists(): shutil.rmtree(storage)
                if (snapshot/'storage').exists(): shutil.copytree(snapshot/'storage',storage)
                self.config=dict(self.defaults); self.config.update(json.loads((self.root/'data/config.json').read_text()))
                self.sync_config=self._load_sync_config()
                raise

    def restore_archive(self,source,confirmation):
        require(confirmation=='恢复备份','恢复需要确认文字：恢复备份')
        with tempfile.TemporaryDirectory(prefix='silo-restore-') as temp:
            stage=Path(temp); manifest,config=self._validated_archive(source,stage)
            self._replace_snapshot(stage,config)
        return {'restored':True,'manifest':manifest,'stats':self.stats()}

    def clear_data(self,confirmation):
        require(confirmation=='清空全部数据','清空需要确认文字：清空全部数据')
        with tempfile.TemporaryDirectory(prefix='silo-empty-') as temp:
            stage=Path(temp); empty=Store(stage)
            shutil.copyfile(self.root/'data/config.json',stage/'data/config.json')
            (stage/'storage').mkdir(exist_ok=True)
            config=json.loads((stage/'data/config.json').read_text())
            self._replace_snapshot(stage,config)
            self.sync_config={'mode':'standalone','device_id':uid(),'server_url':None,'token':None,'interval_seconds':30,'enabled':False}; self._save_sync_config()
        return {'cleared':True,'stats':self.stats()}


def create_share_package(skill_root,destination):
    """Package executable Skill code with a newly created empty database."""
    skill_root=Path(skill_root).resolve(); dest=Path(destination).resolve()
    require(not dest.exists(),'分享包目标必须尚不存在'); require(not dest.is_relative_to(skill_root),'分享包必须位于 Silo 目录之外')
    dest.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='silo-share-') as temp:
        stage=Path(temp)/'silo'; stage.mkdir()
        for name in ('SKILL.md','scripts','references','assets'):
            source=skill_root/name; target=stage/name
            if source.is_dir(): shutil.copytree(source,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            elif source.is_file(): shutil.copyfile(source,target)
        source_config=skill_root/'data/config.json'
        (stage/'data').mkdir(); (stage/'storage').mkdir()
        if source_config.exists(): shutil.copyfile(source_config,stage/'data/config.json')
        Store(stage)
        with zipfile.ZipFile(dest,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for path in sorted(stage.rglob('*')):
                if path.is_file(): archive.write(path,(Path('silo')/path.relative_to(stage)).as_posix())
            archive.writestr('silo/storage/','')
    return {'path':str(dest),'contains_user_data':False}
