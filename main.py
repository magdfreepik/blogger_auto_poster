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
TREND_GEO_LIST = os.getenv("TREND_GEO_LIST", "IQ").split(",")   # نستعملها لاحقًا لو وسّعنا المواضيع
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
    # صورة واحدة في بداية المقال، مرتبطة بالموضوع
    q = quote_plus(topic)
    return f"https://source.unsplash.com/1200x630/?{q}"

# ==================== Gemini عبر REST v1 ====================
def generate_article(topic: str) -> str:
    """
    نولّد مقالة عربية 1000-1400 كلمة مع مراجع قابلة للنقر.
    نستخدم REST v1 + الموديل gemini-1.5-flash-latest لتفادي خطأ v1beta.
    """
    assert GEMINI_API_KEY, "GEMINI_API_KEY مفقود في الأسرار."
    prompt = (
        f"اكتب مقالة بحثية احترافية بالعربية حول الموضوع: {topic}.\n"
        f"الطول بين {MIN_WORDS} و {MAX_WORDS} كلمة.\n"
        "قسّمها إلى مقدمة، عناوين فرعية واضحة، وخاتمة.\n"
        "ضع في النهاية قائمة مراجع بروابط قابلة للنقر (استخدم صيغة [النص](https://link)).\n"
        "اجعل المصطلحات الفنية المهمة بالعربية ومعها المصطلح الإنجليزي بين قوسين."
    )

    url = "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash-latest:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    # محاولات بسيطة مع backoff خفيف
    for attempt in range(3):
        r = requests.post(url, headers=headers, json=body, timeout=60)
        data = r.json()
        if r.ok and "candidates" in data and data["candidates"]:
            return data["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError(f"Gemini REST error: {data}")

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

    # اجلب blog_id بدقة من API بدل القص اليدوي
    blog = service.blogs().getByUrl(url=BLOG_URL).execute()
    blog_id = blog["id"]

    # صورة البداية + المحتوى
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
    # حالياً نولّد موضوع عام؛ يمكنك لاحقًا ربطه بترند حسب الدولة من TREND_GEO_LIST
    topic = "أثر الذكاء الاصطناعي (Artificial Intelligence) على الإنتاجية والاقتصاد الرقمي"
    print(f"🔎 توليد مقال حول: {topic}")

    article_md = generate_article(topic)
    # ضمان حد الكلمات
    if len(article_md.split()) < MIN_WORDS:
        article_md += "\n\n*ملحوظة: تم التوسّع لتلبية حدّ الكلمات.*"

    content_html = md_to_html(article_md)
    image_url = fetch_image(topic)
    return post_to_blogger(topic, content_html, image_url)

# ==================== نقطة التشغيل اليدوية ====================
if __name__ == "__main__":
    print("🚀 تشغيل يدوي لمقال واحد للتجربة…")
    make_article_once(0)
