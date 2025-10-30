import os
import requests
from urllib.parse import quote_plus
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import markdown, bleach

# ==================== الإعدادات العامة ====================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BLOG_URL       = os.getenv("BLOG_URL")
CLIENT_ID      = os.getenv("CLIENT_ID")
CLIENT_SECRET  = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN  = os.getenv("REFRESH_TOKEN")

PUBLISH_MODE   = os.getenv("PUBLISH_MODE", "draft")
MIN_WORDS, MAX_WORDS = 1000, 1400

# ==================== أدوات مساعدة ====================
def md_to_html(text: str) -> str:
    html = markdown.markdown(text)
    return bleach.clean(
        html,
        tags=["p","a","strong","em","h1","h2","h3","ul","ol","li","blockquote","br","code","pre"],
        attributes={"a": ["href","title","rel","target"]},
    )

def fetch_image(topic: str) -> str:
    q = quote_plus(topic)
    return f"https://source.unsplash.com/1200x630/?{q}"

# ==================== Gemini عبر REST ====================
def _rest_generate(ver: str, model: str, prompt: str):
    """نداء REST مباشر؛ model بدون بادئة 'models/'، والدالة ترجع (text | None, last_json)."""
    if model.startswith("models/"):
        model = model.split("/", 1)[1]
    url = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
        data = r.json()
        if r.ok and "candidates" in data and data["candidates"]:
            return data["candidates"][0]["content"]["parts"][0]["text"], data
        return None, data
    except Exception as e:
        return None, {"error": str(e)}

def _list_models(ver: str):
    try:
        r = requests.get(
            f"https://generativelanguage.googleapis.com/{ver}/models?key={GEMINI_API_KEY}",
            timeout=30,
        )
        if r.ok:
            return [m.get("name","") for m in r.json().get("models",[])]
    except:
        pass
    return []

def generate_article(topic: str) -> str:
    """
    نولّد مقالة عربية 1000–1400 كلمة مع مراجع قابلة للنقر.
    نستخدم النموذج المؤكد توافره لديك: gemini-2.5-flash (v1beta ثم v1 احتياط).
    """
    assert GEMINI_API_KEY, "GEMINI_API_KEY مفقود في الأسرار."

    prompt = (
        f"اكتب مقالة بحثية احترافية بالعربية حول الموضوع: {topic}.\n"
        f"الطول بين {MIN_WORDS} و {MAX_WORDS} كلمة.\n"
        "قسّمها إلى مقدمة، عناوين فرعية واضحة، وخاتمة.\n"
        "ضع في النهاية قائمة مراجع بروابط قابلة للنقر بصيغة Markdown مثل [اسم المرجع](https://example.com).\n"
        "استخدم المصطلحات العربية مع المصطلح الإنجليزي بين قوسين عند الحاجة."
    )

    attempts = [
        ("v1beta", "gemini-2.5-flash"),
        ("v1",     "gemini-2.5-flash"),
        ("v1beta", "gemini-2.0-flash"),
        ("v1",     "gemini-2.0-flash"),
        ("v1beta", "gemini-pro-latest"),
        ("v1",     "gemini-pro-latest"),
    ]

    last_json = None
    for ver, model in attempts:
        print(f"🧪 محاولة عبر {ver}/{model} …")
        text, last_json = _rest_generate(ver, model, prompt)
        if text:
            print(f"✅ Gemini OK via {ver}/{model}")
            return text
        print(f"⚠️ فشل {ver}/{model} — نجرّب التالي…")

    # لم ينجح أي نموذج: اطبع المتاح للمساعدة
    avail_v1beta = _list_models("v1beta")
    avail_v1     = _list_models("v1")
    raise RuntimeError(
        "Gemini REST error: لم نصل إلى نموذج يعمل في حسابك.\n"
        f"v1beta models: {avail_v1beta}\n"
        f"v1 models: {avail_v1}\n"
        f"آخر استجابة: {last_json}"
    )

# ==================== النشر على Blogger ====================
def post_to_blogger(title: str, content_html: str, image_url: str):
    creds = Credentials(
        None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )
    service = build("blogger", "v3", credentials=creds)

    blog = service.blogs().getByUrl(url=BLOG_URL).execute()
    blog_id = blog["id"]

    html = f'<img src="{image_url}" alt="" style="width:100%;border-radius:8px;"/><br/>' + content_html

    post_body = {
        "kind": "blogger#post",
        "blog": {"id": blog_id},
        "title": title,
        "content": html,
    }

    post = service.posts().insert(
        blogId=blog_id,
        body=post_body,
        isDraft=(PUBLISH_MODE != "live")
    ).execute()

    print("✅ تم النشر:", post.get("url", "(مسودة)"))
    return post.get("url")

# ==================== توليد ونشر مقال واحد ====================
def make_article_once(slot: int = 0):
    topic = "أثر الذكاء الاصطناعي (Artificial Intelligence) على الإنتاجية والاقتصاد الرقمي"
    print(f"🔎 توليد مقال حول: {topic}")

    article_md, _ = _rest_generate("v1beta", "gemini-2.5-flash",
                                   f"اكتب ملخصًا للجمهور: 3 أسطر حول {topic}.")
    # ليس ضروريًا، مجرد جس نبض
    article_md = generate_article(topic)
    if len(article_md.split()) < MIN_WORDS:
        article_md += "\n\n*إضافة توسّع لتلبية الحد الأدنى من الكلمات.*"

    content_html = md_to_html(article_md)
    image_url = fetch_image(topic)
    return post_to_blogger(topic, content_html, image_url)

# ==================== نقطة التشغيل اليدوية ====================
if __name__ == "__main__":
    print("🚀 تشغيل يدوي لمقال واحد للتجربة…")
    make_article_once(0)
