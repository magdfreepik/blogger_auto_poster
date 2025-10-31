# -*- coding: utf-8 -*-
import os, re, json, html, random, hashlib
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import requests
import markdown as md
import bleach

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ==================== الإعدادات العامة ====================
TZ = ZoneInfo("Asia/Baghdad")

# أسرار أساسية
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BLOG_URL       = os.getenv("BLOG_URL")
CLIENT_ID      = os.getenv("CLIENT_ID")
CLIENT_SECRET  = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN  = os.getenv("REFRESH_TOKEN")

# نشر مباشر أم مسودة
PUBLISH_MODE   = os.getenv("PUBLISH_MODE", "draft").lower()  # live | draft

# حدود المقال
MIN_WORDS, MAX_WORDS = 1000, 1400

# مفاتيح اختيارية للصور
PEXELS_API_KEY       = os.getenv("PEXELS_API_KEY", "")
PIXABAY_API_KEY      = os.getenv("PIXABAY_API_KEY", "")
UNSPLASH_ACCESS_KEY  = os.getenv("UNSPLASH_ACCESS_KEY", "")
FORCED_IMAGE         = (os.getenv("FEATURED_IMAGE_URL", "") or "").strip()

# ترند (تستطيع وضع قائمة دول بفاصلة عبر TREND_GEO_LIST)
TREND_GEO      = os.getenv("TREND_GEO", "IQ")
TREND_GEO_LIST = [g.strip() for g in os.getenv("TREND_GEO_LIST", "").split(",") if g.strip()]

# منع التكرار موضوعيًا (بالأيام)
TOPIC_WINDOW_DAYS = int(os.getenv("TOPIC_WINDOW_DAYS", "14"))

# ==================== أدوات HTML ====================
def md_to_html(text: str) -> str:
    html_raw = md.markdown(text, extensions=["extra", "sane_lists"])
    return bleach.clean(
        html_raw,
        tags={"p","a","strong","em","h2","h3","h4","ul","ol","li","blockquote","br","code","pre","hr","img"},
        attributes={
            "a": ["href","title","rel","target"],
            "img": ["src","alt","title","loading","decoding","width","height"]
        },
        protocols=["http","https","mailto"],
        strip=True
    ).replace("<a ", '<a target="_blank" rel="noopener" ')

def linkify_urls_md(text: str) -> str:
    return re.sub(r'(?<!\()https?://[^\s)]+', lambda m: f"[المصدر]({m.group(0)})", text)

def clamp_words_ar(text, min_words=MIN_WORDS, max_words=MAX_WORDS):
    words = text.split()
    if len(words) < min_words: return text
    if len(words) <= max_words: return text
    clipped = " ".join(words[:max_words])
    m = re.search(r"(.+[.!؟…])", clipped, flags=re.S)
    return m.group(1) if m else clipped

# ==================== Blogger API + منع التكرار ====================
def blogger_service():
    creds = Credentials(
        None, refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/blogger"]
    )
    return build("blogger", "v3", credentials=creds, cache_discovery=False)

def get_blog_id(svc, blog_url):
    return svc.blogs().getByUrl(url=blog_url).execute()["id"]

def _norm_title(s: str) -> str:
    s = (s or "").strip().lower()
    return re.sub(r"\s+", " ", s)

def _fingerprint(title: str, html_content: str) -> str:
    snippet = re.sub(r"<[^>]+>", " ", html_content or "")
    snippet = re.sub(r"\s+", " ", snippet).strip()[:100]
    return hashlib.sha1((_norm_title(title)+"|"+snippet).encode("utf-8")).hexdigest()

def _find_existing_post_by_title(svc, blog_id, title):
    norm = _norm_title(title)

    # live
    try:
        resp = svc.posts().list(blogId=blog_id, fetchBodies=False,
                                maxResults=50, orderBy="UPDATED", status=["live"]).execute()
        for it in (resp.get("items") or []):
            if _norm_title(it.get("title")) == norm:
                return it["id"]
    except Exception:
        pass

    # drafts
    try:
        resp = svc.posts().list(blogId=blog_id, fetchBodies=False,
                                maxResults=50, orderBy="UPDATED", status=["draft"]).execute()
        for it in (resp.get("items") or []):
            if _norm_title(it.get("title")) == norm:
                return it["id"]
    except Exception:
        pass
    return None

def post_or_update(title: str, html_content: str, labels=None):
    svc = blogger_service()
    blog_id = get_blog_id(svc, BLOG_URL)
    body = {"kind": "blogger#post", "title": title, "content": html_content}
    if labels: body["labels"] = labels
    is_draft = (PUBLISH_MODE != "live")

    # منع توازي داخل Job واحد (على Actions عادة لا تتوازى نفس الـ job)
    fp = _fingerprint(title, html_content)
    body.setdefault("labels", []).append(f"fp-{fp}")

    existing = _find_existing_post_by_title(svc, blog_id, title)
    if existing:
        upd = svc.posts().update(blogId=blog_id, postId=existing, body=body).execute()
        print("UPDATED:", upd.get("url", upd.get("id")))
        return upd
    ins = svc.posts().insert(blogId=blog_id, body=body, isDraft=is_draft).execute()
    print("CREATED:", ins.get("url", ins.get("id")))
    return ins

def recent_titles(limit=30):
    try:
        svc = blogger_service()
        blog_id = get_blog_id(svc, BLOG_URL)
        res = svc.posts().list(blogId=blog_id, fetchBodies=False,
                               maxResults=limit, orderBy="PUBLISHED").execute()
        items = res.get("items", []) or []
        return { (it.get("title","") or "").strip() for it in items }
    except Exception:
        return set()

# ==================== Gemini REST مباشر ====================
def _rest_generate(ver: str, model: str, prompt: str):
    """يرجع نصًا أو None."""
    if model.startswith("models/"):
        model = model.split("/", 1)[1]
    url = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_API_KEY}"
    body = {"contents":[{"parts":[{"text": prompt}]}],
            "generationConfig":{"temperature":0.7,"topP":0.9,"maxOutputTokens":4096}}
    try:
        r = requests.post(url, json=body, timeout=120)
        data = r.json()
        if r.ok and data.get("candidates"):
            return data["candidates"][0]["content"]["parts"][0]["text"]
        return None
    except Exception:
        return None

def ask_gemini(prompt: str) -> str:
    attempts = [
        ("v1beta", "gemini-2.5-flash"),
        ("v1",     "gemini-2.5-flash"),
        ("v1beta", "gemini-2.0-flash"),
        ("v1",     "gemini-2.0-flash"),
        ("v1beta", "gemini-pro"),
        ("v1",     "gemini-pro"),
    ]
    last = None
    for ver, model in attempts:
        txt = _rest_generate(ver, model, prompt)
        if txt:
            txt = txt.strip()
            return clamp_words_ar(txt, MIN_WORDS, MAX_WORDS)
        last = f"{ver}/{model}"
    raise RuntimeError(f"Gemini REST error (last tried {last})")

# ==================== اختيار الفئة والموضوع ====================
def cycle_cat(slot_idx, today=None):
    """الفئات التي طلبتها فقط: tech / science / social / news."""
    d = today or date.today()
    mod = ((d - date(2025,1,1)).days) % 3
    if mod == 0:
        return "tech" if slot_idx == 0 else "science"
    if mod == 1:
        return "social" if slot_idx == 0 else "tech"
    return "news" if slot_idx == 0 else "social"

def propose_topic_by_ai(category: str) -> str:
    name_ar = {"tech":"تقنية","science":"علوم","social":"اجتماعية"}[category]
    prompt = f"""
اقترح عنوان موضوع عربي موجز (سطر واحد فقط) يناسب مقالة {name_ar} راهنة ومثيرة للاهتمام
بدون أسماء أشخاص/شركات بعينها وبدون تواريخ محددة، وبدون علامات اقتباس. أعد العنوان فقط.
""".strip()
    t = ask_gemini(prompt).splitlines()[0].strip()
    t = re.sub(r'[\"«»]+', "", t)[:90]
    return t or f"مقالة {name_ar} معاصرة"

def fetch_trends_list(geo: str, max_items=10):
    url  = f"https://trends.google.com/trends/trendingsearches/daily/rss?geo={geo}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        import feedparser
        feed = feedparser.parse(r.text)
        out = []
        for e in feed.entries[:max_items]:
            t = e.title
            link = f"https://www.google.com/search?q={quote_plus(t)}"
            out.append((t, link))
        return out
    except Exception:
        return []

def fetch_trends_region(geos, per_geo=10):
    bucket = {}
    for geo in geos:
        for t, link in fetch_trends_list(geo, max_items=per_geo):
            k = re.sub(r"[^\w\u0600-\u06FF]+", " ", t.lower())
            k = re.sub(r"\s+", " ", k).strip()
            if not k: continue
            bucket.setdefault(k, {"count":0,"title":t,"link":link})
            bucket[k]["count"] += 1
    ranked = sorted(bucket.values(), key=lambda x:(-x["count"], x["title"]))
    return [(r["title"], r["link"]) for r in ranked]

def fetch_top_me_news(n=0):
    url = "https://news.google.com/rss?hl=ar&gl=IQ&ceid=IQ:ar"
    try:
        r = requests.get(url, timeout=30)
        import feedparser
        feed = feedparser.parse(r.text)
        if feed.entries:
            idx = min(n, len(feed.entries)-1)
            e = feed.entries[idx]
            return e.title, e.link
    except Exception:
        pass
    return None, None

def choose_topic(category: str, slot_idx: int):
    if category == "news":
        trends = fetch_trends_region(TREND_GEO_LIST, per_geo=10) if TREND_GEO_LIST else fetch_trends_list(TREND_GEO, max_items=10)
        if trends:
            i = 0 if slot_idx == 0 else 1
            if len(trends) > i:
                return trends[i]  # (title, link)
        t, l = fetch_top_me_news(n=slot_idx)
        if t: return (t, l)
        return ("خبر وتحليل راهن", "https://news.google.com/")
    # غير إخباري: نطلب من Gemini اقتراح العنوان
    return propose_topic_by_ai(category)

def labels_for(category: str):
    return {
        "tech":    ["تكنولوجيا","ابتكار","رقمنة"],
        "science": ["علوم","بحث علمي"],
        "social":  ["مجتمع","تنمية","سياسات"],
        "news":    ["أخبار","ترند"],
    }.get(category, ["بحث"])

# ==================== بناء المقال ====================
def ensure_refs(article_md, category, topic, news_link=None):
    text = article_md.strip()
    if "المراجع" not in text:
        text += "\n\n## المراجع\n"
    links = re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", text)
    need = max(0, 4 - len(links))

    base = {
        "tech":[("MIT Tech Review","https://www.technologyreview.com/"),
                ("ACM Digital Library","https://dl.acm.org/"),
                ("IEEE Spectrum","https://spectrum.ieee.org/"),
                ("WEF Tech","https://www.weforum.org/focus/technology/")],
        "science":[("Nature","https://www.nature.com/"),
                   ("Science","https://www.science.org/"),
                   ("Royal Society","https://royalsociety.org/"),
                   ("UNESCO Science","https://www.unesco.org/reports/science/")],
        "social":[("UNDP","https://www.undp.org/publications"),
                  ("OECD Library","https://www.oecd-ilibrary.org/"),
                  ("World Bank Data","https://data.worldbank.org/"),
                  ("Brookings","https://www.brookings.edu/")],
        "news":[("Google News","https://news.google.com/"),
                ("BBC Arabic","https://www.bbc.com/arabic"),
                ("Reuters MENA","https://www.reuters.com/world/middle-east/"),
                ("Al Jazeera","https://www.aljazeera.net/")]
    }
    extras = []
    if category == "news" and news_link:
        extras.append(("مصدر الخبر", news_link))
    extras += base.get(category, base["science"])
    if need > 0:
        text += "\n"
        for name, url in extras[:need]:
            text += f"- [{name}]({url})\n"
    return text

def build_prompt(topic, kind="general", news_link=None):
    base = f"""
- اكتب مقالة عربية واضحة للقراء العامّين.
- الطول بين {MIN_WORDS} و {MAX_WORDS} كلمة.
- بنية: مقدمة موجزة، عناوين فرعية منظمة، أمثلة/شواهد، خاتمة.
- بدون كود/سكريبت/أقواس ثلاثية.
- أضف قسم "المراجع" مع ≥ 4 مصادر بروابط Markdown قابلة للنقر.
- لا تُدخل صورًا داخل النص (ستُضاف صورة الغلاف برمجيًا في الأعلى).
- تجنب الحشو والتكرار.
""".strip()
    extra = ""
    if kind == "news":
        extra = f"""
- اربط التحليل بسياق المنطقة العربية عندما يكون مناسبًا.
- أدرج رابط المصدر ضمن "المراجع": {news_link or "—"}.
""".strip()
    return f"الموضوع: «{topic}»\n{base}\n{extra}\nأنتج النص النهائي مباشرة."

def extract_title(article_md, fallback_topic):
    m = re.search(r"^\s*#+\s*(.+)$", article_md, flags=re.M)
    if m: return m.group(1).strip()[:90]
    for line in article_md.splitlines():
        t = line.strip()
        if t and not t.startswith("#"):
            return t[:90]
    return (fallback_topic if isinstance(fallback_topic,str) else str(fallback_topic))[:90]

# ==================== الصور (مضمونة) ====================
def wiki_lead_image(title, lang="ar"):
    try:
        r = requests.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={"action":"query","format":"json","prop":"pageimages",
                    "piprop":"original|thumbnail","pithumbsize":"1200","titles":title},
            timeout=20
        )
        if not r.ok: return None
        pages = r.json().get("query",{}).get("pages",{})
        for _,p in pages.items():
            if "original" in p:  return p["original"]["source"]
            if "thumbnail" in p: return p["thumbnail"]["source"]
    except Exception:
        pass
    return None

def fetch_img_wiki(topic):
    for lang in ("ar","en"):
        url = wiki_lead_image(topic, lang)
        if url:
            return {"url": url, "credit": f"Image via Wikipedia ({lang})"}
    return None

def fetch_img_pexels(topic):
    if not PEXELS_API_KEY: return None
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         headers={"Authorization": PEXELS_API_KEY},
                         params={"query": topic, "per_page": 10, "orientation": "landscape"},
                         timeout=30)
        if not r.ok: return None
        photos = r.json().get("photos") or []
        if not photos: return None
        p = random.choice(photos)
        return {"url": p["src"]["large2x"],
                "credit": f'صورة من Pexels — <a href="{html.escape(p["url"])}" target="_blank" rel="noopener">المصدر</a>'}
    except Exception:
        return None

def fetch_img_pixabay(topic):
    if not PIXABAY_API_KEY: return None
    try:
        r = requests.get("https://pixabay.com/api/",
                         params={"key": PIXABAY_API_KEY, "q": topic, "image_type":"photo",
                                 "per_page":10, "safesearch":"true", "orientation":"horizontal"},
                         timeout=30)
        if not r.ok: return None
        hits = r.json().get("hits") or []
        if not hits: return None
        p = random.choice(hits)
        return {"url": p["largeImageURL"],
                "credit": f'صورة من Pixabay — <a href="{html.escape(p["pageURL"])}" target="_blank" rel="noopener">المصدر</a>'}
    except Exception:
        return None

def fetch_img_unsplash(topic):
    if not UNSPLASH_ACCESS_KEY: return None
    try:
        r = requests.get("https://api.unsplash.com/search/photos",
                         headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
                         params={"query": topic, "per_page": 10, "orientation":"landscape"},
                         timeout=30)
        if not r.ok: return None
        results = r.json().get("results") or []
        if not results: return None
        p = random.choice(results)
        url = (p.get("urls") or {}).get("regular") or (p.get("urls") or {}).get("full")
        if not url: return None
        user = p.get("user") or {}
        credit = f'صورة من Unsplash — <a href="{html.escape(user.get("links",{}).get("html","https://unsplash.com"))}" target="_blank" rel="noopener">{html.escape(user.get("name","Unsplash"))}</a>'
        return {"url": url, "credit": credit}
    except Exception:
        return None

def _ensure_https(u: str) -> str:
    if not u: return u
    if u.startswith("//"): return "https:" + u
    if u.startswith("http://"): return "https://" + u[7:]
    return u

_BAD_SRC_RE = re.compile(r'(?:المصدر|source)\s*[:\-–]?\s*(pexels|pixabay|unsplash)', re.I)

def build_post_html(title, img, article_md):
    # تسلسل صيد الصورة + Placeholder مضمون
    if FORCED_IMAGE:
        img = {"url": FORCED_IMAGE, "credit": "Featured image"}
    if not img: img = fetch_img_wiki(title)
    if not img: img = fetch_img_pexels(title)
    if not img: img = fetch_img_pixabay(title)
    if not img: img = fetch_img_unsplash(title)
    if not img: img = {"url":"https://via.placeholder.com/1200x630.png?text=LoadingAPK", "credit":"Placeholder"}

    cover = _ensure_https(img.get("url",""))
    if not cover or not cover.startswith("https://"):
        cover = "https://via.placeholder.com/1200x630.png?text=LoadingAPK"

    img_html = f"""
<figure class="post-cover" style="margin:0 0 12px 0;">
  <img src="{html.escape(cover)}" alt="{html.escape(title)}"
       width="1200" height="675" loading="lazy" decoding="async"
       style="max-width:100%;height:auto;border-radius:8px;display:block;margin:auto;" />
</figure>
<p style="font-size:0.9em;color:#555;margin:4px 0 16px 0;">{img.get("credit","")}</p>
<hr/>
""".strip()+"\n"

    body_md = linkify_urls_md(article_md)
    body_html = md_to_html(body_md)
    body_html = _BAD_SRC_RE.sub("", body_html)
    return img_html + body_html

# ==================== توليد المقال ونشره ====================
def build_article(category: str, picked):
    if isinstance(picked, tuple):  # news
        t, link = picked
        prompt = build_prompt(t, kind="news", news_link=link)
        article = ask_gemini(prompt)
        article = ensure_refs(article, "news", t, news_link=link)
        title   = extract_title(article, t)
        query   = t
    else:
        t = picked
        prompt = build_prompt(t, kind="general")
        article = ask_gemini(prompt)
        article = ensure_refs(article, category, t)
        title   = extract_title(article, t)
        query   = t
    return title, article, query

def make_article_once(slot: int = 0):
    # 1) اختيار الفئة وفق اليوم / الفتحة
    category = cycle_cat(slot, date.today())

    # 2) اختيار الموضوع (News من الترند/الـRSS، غير ذلك من Gemini)
    picked = choose_topic(category, slot)

    # 3) توليد المقال
    title, article_md, _ = build_article(category, picked)

    # 4) بناء HTML مع صورة غلاف مضمونة
    html_content = build_post_html(title, article_md, article_md)

    # 5) منع التكرار عبر التحديث
    labels = labels_for(category)
    res = post_or_update(title, html_content, labels=labels)

    state = "مسودة" if PUBLISH_MODE != "live" else "منشور حي"
    print(f"[{datetime.now(TZ)}] {state}: {res.get('url','(بدون رابط)')} | {category} | {title}")

# تشغيل يدوي محلي (غير مستخدم في GitHub Actions عادة)
if __name__ == "__main__":
    make_article_once(0)
