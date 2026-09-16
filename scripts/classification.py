"""Offline bookmark categorization, deliberately bounded and transparent."""
import re
import unicodedata
from urllib.parse import urlsplit,unquote

RULES = [
 ('大数据',r'大数据|数据湖|数据仓库|数据平台|\b(hadoop|spark|hive|flink|linkis|dolphinscheduler|kafka|hudi|iceberg|presto|trino|clickhouse|doris|datax|seatunnel|sqoop)\b'),
 ('人工智能',r'人工智能|机器学习|深度学习|大模型|智能体|提示词|\b(ai|aigc|llm|gpt\w*|chatgpt|claude|deepseek|agent|agents|rag|ollama|midjourney|comfyui|pytorch|tensorflow|langchain)\b'),
 ('前端开发',r'前端|网页模板|网站模板|\b(react|vue|angular|javascript|typescript|css|html|webpack|vite|frontend|tailwind)\b'),
 ('后端开发',r'后端|微服务|\b(java|spring\w*|django|fastapi|flask|golang|backend|grpc|mybatis|dubbo)\b'),
 ('数据库',r'数据库|\b(mysql|postgres\w*|sqlite|redis|mongodb|sql|oracle|database|数据库)\b'),
 ('运维部署',r'运维|部署|监控|容器|反向代理|内网穿透|\b(docker|kubernetes|k8s|nginx|linux|devops|jenkins|prometheus|grafana|frp|ansible)\b'),
 ('文档协作',r'文档|笔记|知识库|周会|会议纪要|需求模板|任务管理|\b(markdown|wiki|notion|obsidian|office|onlyoffice|siyuan|latex|feishu)\b'),
 ('开源社区',r'开源社区|开源项目|社区开发|发版|版本发布|毕业.*report|\b(apache|asf|committer|pmc|jira|opensource)\b'),
 ('编程学习',r'编程|算法|数据结构|设计模式|源码|教程|\b(leetcode|algorithm|tutorial|build-your-own|50projects|codecrafters)\b'),
 ('英语学习',r'英语|英文学习|词汇|雅思|托福|\b(english|ielts|toefl|duolingo)\b'),
 ('设计素材',r'设计素材|界面设计|配色|图标|字体|壁纸|\b(figma|dribbble|behance|icon|icons|font|fonts|unsplash)\b'),
 ('开发工具',r'在线工具|格式化|正则|编码转换|压缩工具|\b(json|formatter|regex|postman|insomnia|toolbox)\b'),
 ('阅读学习',r'读书|书籍|电子书|阅读|图书|课程|\b(ebook|books|coursera|edx)\b'),
 ('生活娱乐',r'睡前故事|儿童|电影|音乐|旅游|美食|动漫|游戏|\b(tiktok|music|movie|game)\b'),
 ('财经理财',r'理财|金融|银行|股票|基金|投资|\b(finance|trading|stock)\b'),
 ('安全与网络',r'网络安全|信息安全|网络协议|防火墙|密码学|\b(security|vpn|proxy|firewall|owasp)\b'),
 ('导航资源',r'导航|资源合集|工具合集|\b(awesome|directory|navigation)\b'),
]
GENERIC={'书签栏','收藏夹栏','其他书签','收藏夹','书签','bookmarks','bookmarks bar','favorites','other bookmarks','个人收藏夹','未分类'}

def classify(title,url,folders=()):
    text=unicodedata.normalize('NFKC',title+' '+unquote(url or '')+' '+' '.join(folders)).casefold()
    result=[]
    for name,pattern in RULES:
        if re.search(pattern,text): result.append({'name':name,'facet':'domain','confidence':.85})
    result=result[:4]
    for folder in folders:
        for name in folder.split('/'):
            name=''.join(c for c in name if unicodedata.category(c)!='Cf').strip()
            if name and name.casefold() not in GENERIC and len(name)<=40 and not any(t['name']==name for t in result):
                result.append({'name':name,'facet':'scenario','confidence':1})
        if len(result)>=6: break
    if not result:
        host=(urlsplit(url or '').hostname or '').lower()
        if host=='github.com': result=[{'name':'开源项目','facet':'scenario','confidence':1}]
        else: result=[{'name':'待分类','facet':'scenario','confidence':1}]
    return result[:6]
