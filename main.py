import os, random, time, json, requests, re, markdown, bleach, backoff, feedparser
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# إعدادات عامة
MIN_WORDS, MAX_WORDS = 1000, 1400
TREND_GEO = "IQ"
BLOG_URL = os.getenv("BLOG_URL")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# -------------------------------------------
#  1. جلب مواضيع الترند من Google News
# -------------------------------------------
def get_trending_topics():
    feed = feedparser.parse("https://news.google.com/rss?hl=ar&gl=IQ&ceid=IQ:ar")
    topics = [entry.title for entry in feed.entries[:10]]
    random.shuffle(topics)
    return topics

# -------------------------------------------
#  2. توليد نص المقال من Gemini
# -------------------------------------------
def generate_article(prompt):
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    body = {
        "contents": [{
            "parts": [{
                "text": (
                    f"اكتب مقالة بحثية بالعربية لا تقل عن {MIN_WORDS} كلمة ولا تزيد عن {MAX_WORDS} "
                    f"حول الموضوع: {prompt}. اجعل المقال منظمًا، بمقدمة وعناوين فرعية وخاتمة. "
                    "ضع مراجع حقيقية في النهاية بشكل روابط قابلة للنقر. استخدم مصطلحات إنجليزية بين قوسين عند الحاجة."
                )
            }]
        }]
    }
    res = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent",
        headers=headers, json=body
    )
    data = res.json()
    if "candidates" in data:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    else:
        raise RuntimeError(f"Gemini error: {data}")

# -------------------------------------------
#  3. تنظيف النص وتحويله إلى HTML
# -------------------------------------------
def markdown_to_html(text):
    html = markdown.markdown(text)
    return bleach.clean(html, tags=["p","a","strong","em","h1","h2","h3","ul","ol","li","blockquote","br"], attributes={"a": ["href", "title"]})

# -------------------------------------------
#  4. جلب صورة مناسبة للموضوع
# -------------------------------------------
def fetch_image(topic):
    query = quote_plus(topic)
    url = f"https://source.unsplash.com/1200x630/?{query}"
    return url

# -------------------------------------------
#  5. إنشاء المقال في Blogger
# -------------------------------------------
def post_to_blogger(title, content, image_url):
    creds = Credentials(
        None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )
    service = build("blogger", "v3", credentials=creds)
    blog_id = BLOG_URL.split("blogspot.com/")[-1].replace("/", "")
    post_body = {
        "kind": "blogger#post",
        "blog": {"id": blog_id},
        "title": title,
        "content": f'<img src="{image_url}" style="width:100%;border-radius:8px;"><br>{content}',
    }
    post = service.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
    print("✅ تم النشر:", post["url"])
    return post["url"]

# -------------------------------------------
#  6. توليد ونشر مقال واحد
# -------------------------------------------
def make_article_once(slot=0):
    topics = get_trending_topics()
    for topic in topics:
        print(f"🔎 توليد مقال حول: {topic}")
        article_md = generate_article(topic)
        if len(article_md.split()) < MIN_WORDS:
            continue
        image = fetch_image(topic)
        html_content = markdown_to_html(article_md)
        post_to_blogger(topic, html_content, image)
        break

# -------------------------------------------
#  نقطة التشغيل
# -------------------------------------------
if __name__ == "__main__":
    print("🚀 تشغيل يدوي لمقال واحد للتجربة...")
    make_article_once(0)
