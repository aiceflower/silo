"""Deterministic discovery fetchers for GitHub, Hacker News, RSS and Atom."""
import ipaddress
import json
import math
import os
import socket
import ssl
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen

from store import Error, canonical, norm, now

MAX_RESPONSE = 3 * 1024 * 1024
USER_AGENT = "Silo/0.2 local-discovery"


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]
    def handle_data(self,data): self.parts.append(data)


def clean_html(value,limit=800):
    parser=PlainText()
    try: parser.feed(value or '')
    except Exception: pass
    return ' '.join(unescape(' '.join(parser.parts)).split())[:limit]


def safe_remote_url(url):
    parsed=urlsplit(url)
    if parsed.scheme not in {'http','https'} or not parsed.hostname: raise Error('validation','发现来源必须是 HTTP(S) 地址')
    host=parsed.hostname.lower()
    if host in {'localhost','localhost.localdomain'}: raise Error('forbidden','发现来源不可指向本机或私有网络')
    try:
        addresses={info[4][0] for info in socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)}
    except socket.gaierror as exc: raise Error('network','无法解析发现来源地址') from exc
    for address in addresses:
        ip=ipaddress.ip_address(address)
        if not ip.is_global: raise Error('forbidden','发现来源不可指向本机或私有网络')
    return url


def fetch(url,headers=None):
    safe_remote_url(url)
    request=Request(url,headers={'User-Agent':USER_AGENT,'Accept':'application/json, application/atom+xml, application/rss+xml, application/xml, text/xml;q=.9',**(headers or {})})
    ca_file=os.environ.get('SSL_CERT_FILE')
    if not ca_file:
        try:
            import certifi
            ca_file=certifi.where()
        except ImportError:
            system_ca='/etc/ssl/cert.pem'
            ca_file=system_ca if os.path.isfile(system_ca) else None
    context=ssl.create_default_context(cafile=ca_file)
    try:
        with urlopen(request,timeout=12,context=context) as response:
            size=response.headers.get('Content-Length')
            if size and int(size)>MAX_RESPONSE: raise Error('network','发现来源响应过大')
            body=response.read(MAX_RESPONSE+1)
            if len(body)>MAX_RESPONSE: raise Error('network','发现来源响应过大')
            return body,response.headers
    except HTTPError as exc:
        if exc.code==304: return None,exc.headers
        raise Error('network',f'发现来源返回 HTTP {exc.code}') from exc
    except (URLError,TimeoutError,OSError) as exc: raise Error('network',f'无法读取发现来源：{exc}') from exc


def iso_date(value):
    if not value: return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if not parsed.tzinfo: parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        try: return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
        except Exception: return None


def github_items(config):
    days=max(1,min(int(config.get('days',30)),365)); since=(datetime.now(timezone.utc)-timedelta(days=days)).date().isoformat()
    parts=[f'created:>={since}',f'stars:>={max(0,int(config.get("min_stars",20)))}','archived:false']
    if config.get('language'): parts.append('language:'+str(config['language']).strip())
    if config.get('topic'): parts.append('topic:'+str(config['topic']).strip())
    url='https://api.github.com/search/repositories?q='+quote(' '.join(parts))+'&sort=stars&order=desc&per_page=50'
    headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    token=os.environ.get('GITHUB_TOKEN')
    if token: headers['Authorization']='Bearer '+token
    body,_=fetch(url,headers); payload=json.loads(body)
    if not isinstance(payload.get('items'),list): raise Error('network','GitHub 返回内容无效')
    results=[]
    for repo in payload['items']:
        stars=int(repo.get('stargazers_count') or 0); forks=int(repo.get('forks_count') or 0)
        age=max(1,(datetime.now(timezone.utc)-datetime.fromisoformat(repo['created_at'].replace('Z','+00:00'))).days)
        score=round(math.log10(stars+1)*35+math.log10(forks+1)*12+max(0,35-age*.35),2)
        results.append({'external_id':str(repo['id']),'item_type':'github','title':repo.get('full_name') or repo.get('name'),'url':repo['html_url'],'summary':repo.get('description') or 'GitHub 项目暂未提供描述。','author':(repo.get('owner') or {}).get('login',''),'published_at':iso_date(repo.get('created_at')),'metrics_json':{'stars':stars,'forks':forks,'issues':int(repo.get('open_issues_count') or 0),'language':repo.get('language'),'license':(repo.get('license') or {}).get('spdx_id'),'updated_at':repo.get('updated_at')},'topics_json':repo.get('topics') or [],'score':score})
    return results


def hackernews_items(config):
    limit=max(1,min(int(config.get('limit',40)),100))
    ids=json.loads(fetch('https://hacker-news.firebaseio.com/v0/topstories.json')[0])[:limit]
    def one(item_id):
        try: return json.loads(fetch(f'https://hacker-news.firebaseio.com/v0/item/{item_id}.json')[0])
        except Error: return None
    with ThreadPoolExecutor(max_workers=8) as pool: stories=list(pool.map(one,ids))
    results=[]
    for story in stories:
        if not story or story.get('type')!='story': continue
        target=story.get('url') or f'https://news.ycombinator.com/item?id={story["id"]}'
        score=int(story.get('score') or 0)+int(story.get('descendants') or 0)*.25
        results.append({'external_id':str(story['id']),'item_type':'article','title':story.get('title') or target,'url':target,'summary':f'Hacker News：{story.get("score",0)} 分，{story.get("descendants",0)} 条讨论。','author':story.get('by',''),'published_at':datetime.fromtimestamp(story.get('time',0),timezone.utc).isoformat(),'metrics_json':{'points':story.get('score',0),'comments':story.get('descendants',0),'discussion_url':f'https://news.ycombinator.com/item?id={story["id"]}'},'topics_json':['Hacker News'],'score':score})
    return results


def _text(node,names):
    for child in node.iter():
        if child.tag.rsplit('}',1)[-1] in names and child.text and child.text.strip(): return child.text.strip()
    return ''


def feed_items(body,feed_url):
    try: root=ET.fromstring(body)
    except ET.ParseError as exc: raise Error('network','RSS/Atom XML 格式无效') from exc
    nodes=[n for n in root.iter() if n.tag.rsplit('}',1)[-1] in {'item','entry'}]
    results=[]
    for node in nodes[:100]:
        title=clean_html(_text(node,{'title'}),300); link=''
        for child in node:
            if child.tag.rsplit('}',1)[-1]=='link':
                link=(child.attrib.get('href') or child.text or '').strip()
                if link and child.attrib.get('rel','alternate') in {'alternate',''}: break
        link=urljoin(feed_url,link); identifier=_text(node,{'id','guid'}) or link
        if not title or not link: continue
        summary=clean_html(_text(node,{'summary','description','content'}))
        published=iso_date(_text(node,{'published','updated','pubDate','date'}))
        author=_text(node,{'author','creator','name'})
        topics=[clean_html(x.text,80) for x in node.iter() if x.tag.rsplit('}',1)[-1]=='category' and x.text]
        results.append({'external_id':identifier,'item_type':'article','title':title,'url':link,'summary':summary,'author':author,'published_at':published,'metrics_json':{},'topics_json':list(dict.fromkeys(topics))[:12],'score':0})
    return results


def rule_score(item,rules):
    text=norm(' '.join([item.get('title',''),item.get('summary',''),item.get('author',''),' '.join(item.get('topics_json',[])),str(item.get('metrics_json',{}).get('language') or '')]))
    delta=0
    for rule in rules:
        if not rule.get('enabled'): continue
        excluded=[norm(x) for x in rule['exclude_keywords_json']]
        if any(x and x in text for x in excluded): delta-=1000; continue
        include=[norm(x) for x in rule['include_keywords_json']]
        delta+=sum(25 for x in include if x and x in text)
        delta+=sum(18 for x in rule['topics_json'] if norm(x) in text)
        delta+=sum(18 for x in rule['languages_json'] if norm(x)==norm(str(item.get('metrics_json',{}).get('language') or '')))
        if int(item.get('metrics_json',{}).get('stars') or 0)<rule.get('min_stars',0): delta-=100
    return delta


def refresh_source(store,source_id):
    source=next((x for x in store.discovery_sources() if x['id']==source_id),None)
    if not source: raise Error('not_found','发现来源不存在')
    try:
        if source['source_type']=='github': items=github_items(source['config_json'])
        elif source['source_type']=='hackernews': items=hackernews_items(source['config_json'])
        elif source['source_type'] in {'rss','atom'}:
            headers={}
            if source.get('etag'): headers['If-None-Match']=source['etag']
            if source.get('last_modified'): headers['If-Modified-Since']=source['last_modified']
            body,response_headers=fetch(source['url'],headers)
            if body is None: store.update_discovery_source(source_id); return {'added':0,'updated':0,'total':0,'not_modified':True}
            items=feed_items(body,source['url'])
            store.update_discovery_source(source_id,etag=response_headers.get('ETag'),last_modified=response_headers.get('Last-Modified'))
        else: raise Error('validation','不支持的发现来源')
        rules=store.interest_rules()
        for item in items: item['score']=float(item.get('score',0))+rule_score(item,rules)
        result=store.ingest_discovery(source_id,items); result['source']=source['name']; return result
    except Exception as exc:
        store.update_discovery_source(source_id,error=str(exc))
        if isinstance(exc,Error): raise
        raise Error('network',f'刷新失败：{exc}') from exc
