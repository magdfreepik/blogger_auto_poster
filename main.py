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

# =============== إعدادات عامة ===============
TZ = ZoneInfo("Asia/Baghdad")
# --- منع تكرار الصور: إعدادات ومساعدات ---
IMAGE_DENY_HASHES = set(h.strip() for h in os.getenv("IMAGE_DENY_HASHES", "").split(",") if h.strip())

_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)

def _img_hash(u: str) -> str:
    return hashlib.sha1((u or "").encode("utf-8")).hexdigest()[:12]

# أسرار أساسية من Secrets
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BLOG_URL       = os.getenv("BLOG_URL")
CLIENT_ID      = os.getenv("CLIENT_ID")
CLIENT_SECRET  = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN  = os.getenv("REFRESH_TOKEN")

# وضع النشر: live | draft
PUBLISH_MODE   = (os.getenv("PUBLISH_MODE","draft") or "draft").lower()
UPDATE_IF_TITLE_EXISTS = (os.getenv("UPDATE_IF_TITLE_EXISTS", "0") == "1")
ADD_TECH_LABELS = (os.getenv("ADD_TECH_LABELS", "1") == "1")  # اجعله "0" لإلغاء ليبلات k-/img-/fp-

# حدود المقال
MIN_WORDS, MAX_WORDS = 1000, 1400

# مفاتيح اختيارية للصور (اتركها فارغة = حرية كاملة)
PEXELS_API_KEY      = os.getenv("PEXELS_API_KEY","")
PIXABAY_API_KEY     = os.getenv("PIXABAY_API_KEY","")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY","")
FORCED_IMAGE        = (os.getenv("FEATURED_IMAGE_URL","") or "").strip()
def recent_image_hashes(limit=60) -> set[str]:
    """يرجع مجموعة بآخر هاشّات الصور المستخدمة (من محتوى آخر المنشورات)."""
    hashes = set(IMAGE_DENY_HASHES)  # ابدأ بالمنع اليدوي من Secrets
    try:
        svc = get_blogger_service()
        bid = get_blog_id(svc, BLOG_URL)
        res = svc.posts().list(blogId=bid, fetchBodies=True, maxResults=limit, orderBy="PUBLISHED").execute()
        for it in (res.get("items") or []):
            html_body = it.get("content", "") or ""
            m = _IMG_RE.search(html_body)
            if m:
                url = m.group(1)
                hashes.add(_img_hash(url))
    except Exception:
        pass
    return hashes
def _image_is_forbidden(url: str) -> bool:
    if not url:
        return True
    h = _img_hash(_ensure_https(url))
    return h in recent_image_hashes()  # يحتوي أيضًا على IMAGE_DENY_HASHES

# ======= مفاتيح موضوع/صورة بناءً على الليبلات في Blogger =======
def blogger_service():
    creds = Credentials(
        None,
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/blogger"]
    )
    return build("blogger","v3",credentials=creds, cache_discovery=False)

def get_blog_id(svc, blog_url):
    return svc.blogs().getByUrl(url=blog_url).execute()["id"]

def all_recent_labels(limit=200):
    svc = blogger_service()
    bid = get_blog_id(svc, BLOG_URL)
    labels = set()
    for st in (["live"], ["draft"]):
        try:
            res = svc.posts().list(blogId=bid, fetchBodies=False, maxResults=limit,
                                   status=st, orderBy="PUBLISHED").execute()
            for it in (res.get("items") or []):
                for lb in (it.get("labels") or []):
                    labels.add(lb)
        except Exception:
            pass
    return labels

def label_used(key_label: str) -> bool:
    try:
        return key_label in all_recent_labels(300)
    except Exception:
        return False

# ======= أدوات HTML =======
def md_to_html(text: str) -> str:
    html_raw = md.markdown(text, extensions=["extra","sane_lists"])
    clean = bleach.clean(
        html_raw,
        tags={"p","a","strong","em","h2","h3","h4","ul","ol","li","blockquote","br","code","pre","hr","img"},
        attributes={"a":["href","title","rel","target"],
                    "img":["src","alt","title","loading","decoding","width","height"]},
        protocols=["http","https","mailto"], strip=True
    )
    return clean.replace("<a ","<a target=\"_blank\" rel=\"noopener\" ")

def clamp_words_ar(text, min_words=MIN_WORDS, max_words=MAX_WORDS):
    words = text.split()
    if len(words) < min_words: return text
    if len(words) <= max_words: return text
    clipped = " ".join(words[:max_words])
    m = re.search(r"(.+[.!؟…])", clipped, flags=re.S)
    return m.group(1) if m else clipped

def linkify_urls_md(text: str) -> str:
    return re.sub(r'(?<!\()https?://[^\s)]+', lambda m: f"[المصدر]({m.group(0)})", text)

# ======= تخزين محلي لمنع التكرار =======
def _jsonl_read(path):
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8") as f:
        for ln in f:
            try: out.append(json.loads(ln))
            except: pass
    return out

def _jsonl_append(path, obj):
    with open(path,"a",encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False)+"\n")

def _norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\u0600-\u06FF]+"," ", s)
    return re.sub(r"\s+"," ", s).strip()

TITLE_WINDOW        = int(os.getenv("TITLE_WINDOW","40"))
TOPIC_WINDOW_DAYS   = int(os.getenv("TOPIC_WINDOW_DAYS","14"))
HISTORY_TITLES_FILE = "posted_titles.jsonl"
HISTORY_TOPICS_FILE = "used_topics.jsonl"
# =================== سياسة اختيار المواضيع ===================
_TOPIC_POLICY = os.getenv("TOPIC_POLICY", "")
POLICY = {
    "avoid_repeat": "true" in _TOPIC_POLICY,
    "diversification": "high" in _TOPIC_POLICY,
    "allow_old_topics_after_days": int(re.search(r"after_days:(\d+)", _TOPIC_POLICY or "")[1]) if re.search(r"after_days:(\d+)", _TOPIC_POLICY or "") else 90,
    "prefer_new_domains": "true" in _TOPIC_POLICY,
    "trend_mix": float(re.search(r"trend_mix:(\d+(?:\.\d+)?)", _TOPIC_POLICY or "")[1]) if re.search(r"trend_mix:(\d+(?:\.\d+)?)", _TOPIC_POLICY or "") else 0.4
}

def should_skip_topic(topic_key: str) -> bool:
    """تحقق إن كان الموضوع تكرر مؤخرًا."""
    if not POLICY["avoid_repeat"]:
        return False
    cutoff = datetime.now(TZ) - timedelta(days=POLICY["allow_old_topics_after_days"])
    for rec in load_jsonl(HISTORY_TOPICS_FILE):
        try:
            if rec.get("topic_key") == topic_key and datetime.fromisoformat(rec["time"]) > cutoff:
                return True
        except:
            pass
    return False

def diversify_topic_request(category: str) -> str:
    """إضافة توجيه ذكي لجيميناي لتوسيع مجالات البحث."""
    base = f"فئة المقال: {category}\n"
    if POLICY["diversification"]:
        base += "اختر مجالًا جديدًا لم يتم تناوله مؤخرًا، استكشف زاوية غير مألوفة.\n"
    if POLICY["prefer_new_domains"]:
        base += "اختر مواضيع من مجالات ناشئة أو متطورة حديثًا بدل المواضيع التقليدية.\n"
    return base

def recent_titles(limit=TITLE_WINDOW):
    titles=set()
    try:
        svc   = blogger_service()
        bid   = get_blog_id(svc, BLOG_URL)
        resp  = svc.posts().list(blogId=bid, fetchBodies=False, maxResults=limit,
                                 orderBy="PUBLISHED").execute()
        for it in (resp.get("items",[]) or []):
            t = (it.get("title","") or "").strip()
            if t: titles.add(t)
    except Exception:
        pass
    for r in _jsonl_read(HISTORY_TITLES_FILE)[-limit:]:
        t = r.get("title","").strip()
        if t: titles.add(t)
    return titles

def recent_topic_keys(days=TOPIC_WINDOW_DAYS):
    cutoff = datetime.now(TZ) - timedelta(days=days)
    keys=set()
    for r in _jsonl_read(HISTORY_TOPICS_FILE):
        try:
            dt = datetime.fromisoformat(r.get("time"))
            if dt >= cutoff:
                k = r.get("topic_key","")
                if k: keys.add(k)
        except: pass
    return keys

def record_publish(title, topic_key):
    _jsonl_append(HISTORY_TITLES_FILE, {"title":title,"time":datetime.now(TZ).isoformat()})
    _jsonl_append(HISTORY_TOPICS_FILE, {"topic_key":topic_key,"time":datetime.now(TZ).isoformat()})

# ======= بصمات =======
def _fingerprint(title: str, html_content: str) -> str:
    snippet = re.sub(r"<[^>]+>"," ", html_content or "")
    snippet = re.sub(r"\s+"," ", snippet).strip()[:100]
    return hashlib.sha1(( _norm_text(title) + "|" + snippet ).encode("utf-8")).hexdigest()

# ======= Blogger: إنشاء/تحديث =======
def _find_existing_post_by_title(svc, blog_id, title):
    norm = _norm_text(title)
    for status in (["live"], ["draft"]):
        try:
            resp = svc.posts().list(blogId=blog_id, fetchBodies=False, maxResults=50,
                                    orderBy="UPDATED", status=status).execute()
            for it in (resp.get("items") or []):
                if _norm_text(it.get("title")) == norm:
                    return it["id"]
        except Exception:
            pass
    return None

def post_or_update(title: str, html_content: str, labels=None,
                   topic_key_label: str = None, image_hash_label: str = None):
    svc     = blogger_service()
    blog_id = get_blog_id(svc, BLOG_URL)
    body    = {"kind": "blogger#post", "title": title, "content": html_content}

    body_labels = list(labels or [])
    if ADD_TECH_LABELS:
        if topic_key_label: body_labels.append(topic_key_label)
        if image_hash_label: body_labels.append(image_hash_label)
        fp = _fingerprint(title, html_content)
        body_labels.append(f"fp-{fp}")
    if body_labels:
        body["labels"] = body_labels

    is_draft = (PUBLISH_MODE != "live")
    existing = _find_existing_post_by_title(svc, blog_id, title)

    if existing and UPDATE_IF_TITLE_EXISTS:
        upd = svc.posts().update(blogId=blog_id, postId=existing, body=body).execute()
        print("UPDATED:", upd.get("url", upd.get("id")))
        return upd

    if existing and not UPDATE_IF_TITLE_EXISTS:
        title = f"{title} — {datetime.now(TZ).strftime('%Y/%m/%d %H:%M')}"
        body["title"] = title

    ins = svc.posts().insert(blogId=blog_id, body=body, isDraft=is_draft).execute()
    print("CREATED:", ins.get("url", ins.get("id")))
    return ins

# ======= Gemini REST =======
def _rest_generate(ver: str, model: str, prompt: str):
    if model.startswith("models/"): model = model.split("/",1)[1]
    url  = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_API_KEY}"
    body = {"contents":[{"parts":[{"text":prompt}]}],
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
        ("v1beta","gemini-2.5-flash"),
        ("v1","gemini-2.5-flash"),
        ("v1beta","gemini-2.0-flash"),
        ("v1","gemini-2.0-flash"),
        ("v1beta","gemini-pro"),
        ("v1","gemini-pro"),
    ]
    last=None
    for ver, model in attempts:
        txt = _rest_generate(ver, model, prompt)
        if txt:
            return clamp_words_ar(txt.strip(), MIN_WORDS, MAX_WORDS)
        last = f"{ver}/{model}"
    raise RuntimeError(f"Gemini REST error (last tried {last})")

# ======= اختيار الفئات والموضوعات =======
CATEGORIES = [
    "تقنية","علوم","حضارة وتاريخ","بزنس وريادة","اقتصاد","تطبيقات وخدمات رقمية",
    "تنمية الذات","صحة","تغذية ولياقة","تجميل وعناية","سيارات ونقل ذكي","طاقة وبيئة",
    "تعليم ومهارات","مجتمع وثقافة","سياسة عامة","أمن سيبراني","إعلام وصناعة المحتوى",
    "فنون وإبداع بصري","سفر ومدن","ماليات وأسواق"
]

def category_for_slot(slot_idx: int, today=None) -> str:
    d = today or date.today()
    base = (d - date(2025,1,1)).days
    idx  = (base*2 + slot_idx) % len(CATEGORIES)
    return CATEGORIES[idx]
def fetch_image(query):
    # 0) صورة مفروضة؟
    if FORCED_IMAGE and not _image_is_forbidden(FORCED_IMAGE):
        return {"url": _ensure_https(FORCED_IMAGE), "credit": "Featured image"}

    topic = (query or "Research").split("،")[0].split(":")[0].strip()

    # 1) Wikipedia (ar → en)
    wiki = fetch_image_general(topic)  # ترجع {"url","credit"} أو None
    if wiki and not _image_is_forbidden(wiki["url"]):
        wiki["url"] = _ensure_https(wiki["url"])
        return wiki

    # 2) Pexels
    if PEXELS_API_KEY:
        try:
            r = requests.get("https://api.pexels.com/v1/search",
                             headers={"Authorization": PEXELS_API_KEY},
                             params={"query": topic, "per_page": 10, "orientation": "landscape"},
                             timeout=30)
            photos = r.json().get("photos", []) or []
            random.shuffle(photos)
            for p in photos:
                url = _ensure_https(p["src"]["large2x"])
                if not _image_is_forbidden(url):
                    credit = f'صورة من Pexels — <a href="{html.escape(p["url"])}" target="_blank" rel="noopener">المصدر</a>'
                    return {"url": url, "credit": credit}
        except Exception:
            pass

    # 3) Pixabay
    if PIXABAY_API_KEY:
        try:
            r = requests.get("https://pixabay.com/api/",
                             params={"key": PIXABAY_API_KEY, "q": topic, "image_type": "photo",
                                     "per_page": 10, "safesearch": "true", "orientation": "horizontal"},
                             timeout=30)
            hits = r.json().get("hits", []) or []
            random.shuffle(hits)
            for p in hits:
                url = _ensure_https(p["largeImageURL"])
                if not _image_is_forbidden(url):
                    credit = f'صورة من Pixabay — <a href="{html.escape(p["pageURL"])}" target="_blank" rel="noopener">المصدر</a>'
                    return {"url": url, "credit": credit}
        except Exception:
            pass

    # 4) Unsplash (API اختياري)
    us = fetch_unsplash(topic)
    if us and not _image_is_forbidden(us["url"]):
        us["url"] = _ensure_https(us["url"])
        return us

    # 5) Placeholder مضمون (لن يُمنع)
    return {"url": "https://via.placeholder.com/1200x630.png?text=LoadingAPK", "credit": "Placeholder"}

def labels_for(category: str):
    mapping = {
        "تقنية":["تقنية","ابتكار","رقمنة"],
        "علوم":["علوم","بحث"],
        "حضارة وتاريخ":["تاريخ","حضارة"],
        "بزنس وريادة":["بزنس","ريادة"],
        "اقتصاد":["اقتصاد","تنمية"],
        "تطبيقات وخدمات رقمية":["تطبيقات","خدمات","ويب"],
        "تنمية الذات":["تنمية","مهارات"],
        "صحة":["صحة"],
        "تغذية ولياقة":["تغذية","لياقة"],
        "تجميل وعناية":["تجميل","عناية"],
        "سيارات ونقل ذكي":["سيارات","نقل"],
        "طاقة وبيئة":["طاقة","بيئة"],
        "تعليم ومهارات":["تعليم","تعلم"],
        "مجتمع وثقافة":["مجتمع","ثقافة"],
        "سياسة عامة":["تحليل","سياسة"],
        "أمن سيبراني":["أمن","سيبراني"],
        "إعلام وصناعة المحتوى":["إعلام","محتوى"],
        "فنون وإبداع بصري":["فن","إبداع"],
        "سفر ومدن":["سفر","مدن"],
        "ماليات وأسواق":["ماليات","أسواق","كريبتو"]
    }
    return mapping.get(category,["بحث"])

def propose_topic_for_category(category: str, slot_idx: int) -> str:
    prompt = f"""
أعطني عنوان مقالة عربيّة موجزاً (سطر واحد فقط) ضمن مجال: {category}.
الشروط:
- عنوان عام وحيادي، بلا أسماء أشخاص/شركات/علامات تجارية وبلا تواريخ محددة.
- لا تضع علامات اقتباس.
- رجّع العنوان كسطر واحد فقط.
"""
    raw = ask_gemini(prompt).splitlines()[0].strip()
    title = re.sub(r'[\"«»]+','', raw)[:90]
    return title or f"مقال عام في {category}"

def topic_key(s: str) -> str:
    return _norm_text(s)

def build_prompt(topic: str, category: str):
    base = f"""
- اكتب مقالة عربية واضحة للقراء العامّين عن: {topic}.
- اجعل الطول بين {MIN_WORDS} و {MAX_WORDS} كلمة.
- بنية: مقدمة موجزة، عناوين فرعية، أمثلة/شواهد، خاتمة.
- لا تستخدم كود/سكريبت/أقواس ثلاثية.
- أضف قسم "المراجع" في النهاية مع ≥ 4 مصادر بروابط Markdown قابلة للنقر.
- لا تُدرج صورًا داخل النص (ستضاف صورة غلاف برمجياً).
- تجنب الحشو والتكرار.
"""
    if category == "سياسة عامة":
        base += "\n- اربط التحليل بسياق المنطقة العربية عند الاقتضاء، من دون تبنّي مواقف حادّة."
    return base.strip()

def ensure_refs(article_md: str, category: str) -> str:
    text = article_md.strip()
    if re.search(r"^\s*#+\s*المراجع\s*$", text, flags=re.M) is None and "المراجع" not in text:
        text += "\n\n## المراجع\n"
    links = re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", text)
    need  = max(0, 4 - len(links))
    base_refs = [
        ("Nature","https://www.nature.com/"),
        ("Science","https://www.science.org/"),
        ("OECD Library","https://www.oecd-ilibrary.org/"),
        ("World Bank Data","https://data.worldbank.org/"),
        ("MIT Technology Review","https://www.technologyreview.com/"),
        ("Royal Society","https://royalsociety.org/"),
        ("UNDP Publications","https://www.undp.org/publications")
    ]
    for name,url in base_refs[:need]:
        text += f"- [{name}]({url})\n"
    return text

def extract_title(article_md: str, fallback: str) -> str:
    m = re.search(r"^\s*#+\s*(.+)$", article_md, flags=re.M)
    if m:
        t = m.group(1).strip()
        if _norm_text(t) not in ("المراجع","references"):
            return t[:90]
    for ln in article_md.splitlines():
        t = ln.strip()
        if t and not t.startswith("#") and _norm_text(t) not in ("المراجع","references"):
            return t[:90]
    return (fallback or "مقال")[:90]

# ======= الصور (مع منع تكرار فعلي) =======
def _ensure_https(u: str) -> str:
    if not u: return u
    if u.startswith("//"): return "https:" + u
    if u.startswith("http://"): return "https://" + u[7:]
    return u

def _http_ok(url: str):
    try:
        r = requests.get(url, timeout=20, allow_redirects=True, stream=True)
        if r.status_code in (200, 304):
            return r.url  # بعد التحويل
    except Exception:
        pass
    return None

def _canonical_for_hash(u: str) -> str:
    """تطبيع رابط الصورة قبل حساب الهاش: نحذف ?query و#fragment ونوحّد https."""
    u = _ensure_https(u or "")
    u = u.split("#", 1)[0]
    u = u.split("?", 1)[0]
    return u.lower()

def wiki_lead_image(title, lang="ar"):
    try:
        r = requests.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action":"query","format":"json","prop":"pageimages",
                "piprop":"original|thumbnail","pithumbsize":"1200","titles":title
            }, timeout=20
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
            ok = _http_ok(_ensure_https(url))
            if ok:
                return {"url": ok, "credit": f"Image via Wikipedia ({lang})"}
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
        ok = _http_ok(_ensure_https(p["src"]["large2x"]))
        if ok:
            return {"url": ok,
                    "credit": f'صورة من Pexels — <a href="{html.escape(p["url"])}" target="_blank" rel="noopener">المصدر</a>'}
    except Exception:
        return None
    return None

def fetch_img_pixabay(topic):
    if not PIXABAY_API_KEY: return None
    try:
        r = requests.get("https://pixabay.com/api/",
                         params={"key": PIXABAY_API_KEY, "q": topic, "image_type": "photo",
                                 "per_page": 10, "safesearch": "true", "orientation": "horizontal"},
                         timeout=30)
        if not r.ok: return None
        hits = r.json().get("hits") or []
        if not hits: return None
        p = random.choice(hits)
        ok = _http_ok(_ensure_https(p["largeImageURL"]))
        if ok:
            return {"url": ok,
                    "credit": f'صورة من Pixabay — <a href="{html.escape(p["pageURL"])}" target="_blank" rel="noopener">المصدر</a>'}
    except Exception:
        return None
    return None

def fetch_img_unsplash_api(topic):
    if not UNSPLASH_ACCESS_KEY: return None
    try:
        r = requests.get("https://api.unsplash.com/search/photos",
                         headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
                         params={"query": topic, "per_page": 10, "orientation": "landscape"},
                         timeout=30)
        if not r.ok: return None
        results = r.json().get("results") or []
        if not results: return None
        p = random.choice(results)
        urls = p.get("urls") or {}
        candidate = urls.get("regular") or urls.get("full") or urls.get("small")
        if candidate:
            ok = _http_ok(_ensure_https(candidate))
            if ok:
                user = p.get("user") or {}
                credit_url = (user.get("links") or {}).get("html", "https://unsplash.com")
                credit_name = user.get("name", "Unsplash")
                return {"url": ok,
                        "credit": f'صورة من Unsplash — <a href="{html.escape(credit_url)}" target="_blank" rel="noopener">{html.escape(credit_name)}</a>'}
    except Exception:
        return None
    return None

def _salt_from(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:10]

def fetch_img_free(topic, seed: str):
    q   = quote_plus((topic or "abstract"))
    sig = _salt_from(seed)
    now = datetime.now(TZ).strftime("%Y%m%d%H")
    candidates = [
        f"https://source.unsplash.com/1200x630/?{q}&sig={sig}",
        f"https://loremflickr.com/1200/630/{q}?lock={sig}",
        f"https://picsum.photos/seed/{sig}/1200/630",
    ]
    for url in candidates:
        ok = _http_ok(_ensure_https(url))
        if ok:
            # لا نستخدم ?v= لكسر الكاش في الهاش؛ سنحذف الاستعلام عند الحساب
            return {"url": ok, "credit": "Free image source"}
    return None

def _img_hash(url: str) -> str:
    return hashlib.sha1(_canonical_for_hash(url).encode("utf-8")).hexdigest()[:12]

def extract_keywords_ar(*texts, k=4):
    # (نُعرّفها هنا ثانية إذا احتجتها أعلاه — أو اترك التعريف الأعلى)
    txt = " ".join([t or "" for t in texts])
    words = re.findall(r"[A-Za-z\u0600-\u06FF]{3,}", txt)
    _AR_STOP = set("من في على عن إلى الى و أو ثم كما حيث بما أن إن كان هذه هذا ذلك التي الذين إذا إذ قد سوف مع لدى بين خلال ضد نحو دون غير فوق تحت حول عبر حتى جدا كثيرًا كثيرا بدون".split())
    clean=[]
    for w in words:
        w2=w.strip().lower()
        if w2 in _AR_STOP: continue
        if w2 not in clean: clean.append(w2)
    return " ".join(clean[:k]) or "abstract"

def pick_image(topic_or_title: str, slot_idx: int = 0, article_text: str = "") -> dict:
    q = extract_keywords_ar(topic_or_title, article_text, k=5)
    base_topic = (topic_or_title or "").split("،")[0].split(":")[0].strip() or "abstract"
    seed  = f"{base_topic}|{q}|{slot_idx}|{datetime.now(TZ).date().isoformat()}"

    if FORCED_IMAGE:
        ok = _http_ok(_ensure_https(FORCED_IMAGE))
        if ok and not label_used(f"img-{_img_hash(ok)}"):
            return {"url": ok, "credit": "Featured image"}

    candidates = []

    for key in (base_topic, q):
        cand = fetch_img_wiki(key)
        if cand: candidates.append(cand)

    for key in (q, base_topic):
        for fn in (fetch_img_pexels, fetch_img_pixabay, fetch_img_unsplash_api):
            cand = fn(key)
            if cand: candidates.append(cand)

    free = fetch_img_free(q or base_topic, seed)
    if free: candidates.append(free)

    # اختر أول صورة لم تُستخدم سابقاً بحسب الهاش المُطبّع
    for cand in candidates:
        url = _ensure_https((cand or {}).get("url", ""))
        if not url: continue
        h = _img_hash(url)
        if not label_used(f"img-{h}"):
            return {"url": url, "credit": cand.get("credit", "Image source")}

    # بديل مضمون
    ph = fetch_img_free("abstract", seed) or {"url":"https://via.placeholder.com/1200x630.png?text=LoadingAPK","credit":"Placeholder"}
    return ph

_BAD_SRC_RE = re.compile(r'(?:المصدر|source)\s*[:\-–]?\s*(pexels|pixabay|unsplash)', re.I)

def build_post_html(title, img, article_md):
    if not img or not isinstance(img, dict):
        img = pick_image(title)

    cover = _ensure_https(img.get("url", ""))
    if not cover or not cover.startswith("https://"):
        cover = "https://via.placeholder.com/1200x630.png?text=LoadingAPK"

    img_html = f"""
<figure class="post-cover" style="margin:0 0 12px 0;">
  <img src="{html.escape(cover)}"
       alt="{html.escape(title)}"
       width="1200" height="675"
       loading="lazy" decoding="async"
       style="max-width:100%;height:auto;border-radius:8px;display:block;margin:auto;" />
</figure>
<p style="font-size:0.9em;color:#555;margin:4px 0 16px 0;">{img.get("credit","")}</p>
<hr/>
""".strip() + "\n"

    body_md   = linkify_urls_md(article_md)
    body_html = md_to_html(body_md)
    body_html = _BAD_SRC_RE.sub("", body_html)
    return img_html + body_html

# ======= توليد ونشر =======
def labels_for(category: str):
    mapping = {
        "تقنية":["تقنية","ابتكار","رقمنة"], "علوم":["علوم","بحث"], "حضارة وتاريخ":["تاريخ","حضارة"],
        "بزنس وريادة":["بزنس","ريادة"], "اقتصاد":["اقتصاد","تنمية"], "تطبيقات وخدمات رقمية":["تطبيقات","خدمات","ويب"],
        "تنمية الذات":["تنمية","مهارات"], "صحة":["صحة"], "تغذية ولياقة":["تغذية","لياقة"],
        "تجميل وعناية":["تجميل","عناية"], "سيارات ونقل ذكي":["سيارات","نقل"], "طاقة وبيئة":["طاقة","بيئة"],
        "تعليم ومهارات":["تعليم","تعلم"], "مجتمع وثقافة":["مجتمع","ثقافة"], "سياسة عامة":["تحليل","سياسة"],
        "أمن سيبراني":["أمن","سيبراني"], "إعلام وصناعة المحتوى":["إعلام","محتوى"], "فنون وإبداع بصري":["فن","إبداع"],
        "سفر ومدن":["سفر","مدن"], "ماليات وأسواق":["ماليات","أسواق","كريبتو"]
    }
    return mapping.get(category,["بحث"])

def make_article_once(slot: int = 0):
    category = category_for_slot(slot, date.today())
    topic    = propose_topic_for_category(category, slot)

    used_titles = { _norm_text(t) for t in recent_titles(TITLE_WINDOW) }
    used_keys   = recent_topic_keys(TOPIC_WINDOW_DAYS)
    key         = topic_key(f"{category}::{topic}")
    if key in used_keys or _norm_text(topic) in used_titles:
        topic = propose_topic_for_category(category, slot ^ 1)
        key   = topic_key(f"{category}::{topic}")

    prompt     = build_prompt(topic, category)
    article_md = ask_gemini(prompt)
    article_md = ensure_refs(article_md, category)
    title      = extract_title(article_md, topic)
    if _norm_text(title) in used_titles:
        title += f" — {datetime.now(TZ).strftime('%Y/%m/%d %H:%M')}"

    img = pick_image(f"{category} {topic}", slot_idx=slot, article_text=article_md)
    html_content = build_post_html(title, img, article_md)

    # ليبل بصمة الموضوع والصورة (لن تُضاف إن ADD_TECH_LABELS=0)
    k_label = f"k-{hashlib.sha1(topic_key(f'{category}::{topic}').encode('utf-8')).hexdigest()[:12]}"
    i_label = f"img-{_img_hash(_ensure_https(img.get('url','')))}"

    if ADD_TECH_LABELS and label_used(k_label):
        title += f" — {datetime.now(TZ).strftime('%Y/%m/%d %H:%M')}"

    labels = labels_for(category)
    res    = post_or_update(title, html_content, labels=labels,
                            topic_key_label=(k_label if ADD_TECH_LABELS else None),
                            image_hash_label=(i_label if ADD_TECH_LABELS else None))

# تشغيل يدوي
if __name__ == "__main__":
    make_article_once(0)
    make_article_once(1)
