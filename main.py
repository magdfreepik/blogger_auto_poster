import os
import time
import requests
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.generativeai import configure, GenerativeModel

# ==================== الإعدادات العامة ====================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BLOG_URL = os.getenv("BLOG_URL")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")

PUBLISH_MODE = os.getenv("PUBLISH_MODE", "draft")
TREND_GEO_LIST = os.getenv("TREND_GEO_LIST", "IQ").split(",")
TOPIC_WINDOW_DAYS = int(os.getenv("TOPIC_WINDOW_DAYS", "14"))
SAFE_CALLS_PER_MIN = int(os.getenv("SAFE_CALLS_PER_MIN", "3"))
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "3"))
AI_BACKOFF_BASE = int(os.getenv("AI_BACKOFF_BASE", "4"))

# ==================== تهيئة Gemini ====================

configure(api_key=GEMINI_API_KEY)
model = GenerativeModel("gemini-1.5-flash")

# ==================== دالة جلب الاتجاهات ====================

def get_trending_topic(geo="IQ"):
    try:
        url = f"https://trends.google.com/trends/api/dailytrends?geo={geo}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.text.splitlines()[-1]
            if '"title":{"query":"' in data:
                topic = data.split('"title":{"query":"')[1].split('"')[0]
                return topic
    except Exception as e:
        print("⚠️ خطأ في جلب الاتجاهات:", e)
    return "Artificial Intelligence"

# ==================== دالة توليد المقال ====================

def generate_article(topic):
    prompt = f"""
    Write a professional Arabic research article about "{topic}".
    The article must include introduction, analysis, and conclusion.
    Length: 1500–2000 words.
    Include references at the end.
    """
    response = model.generate_content(prompt)
    return response.text if response and response.text else f"بحث حول {topic}"

# ==================== دالة النشر في Blogger ====================

def post_to_blogger(title, content, image_url):
    creds = Credentials(
        None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )
    service = build("blogger", "v3", credentials=creds)

    # استخراج blog_id تلقائيًا من عنوان المدونة
    blog = service.blogs().getByUrl(url=BLOG_URL).execute()
    blog_id = blog["id"]

    post_body = {
        "kind": "blogger#post",
        "blog": {"id": blog_id},
        "title": title,
        "content": f'<img src="{image_url}" style="width:100%;border-radius:8px;"><br>{content}',
    }

    post = service.posts().insert(
        blogId=blog_id,
        body=post_body,
        isDraft=(PUBLISH_MODE != "live")
    ).execute()

    print("✅ تم النشر:", post.get("url", "(مسودة)"))
    return post.get("url")

# ==================== الدالة الرئيسية ====================

def make_article_once(slot=0):
    geo = TREND_GEO_LIST[slot % len(TREND_GEO_LIST)]
    print(f"🔎 توليد موضوع من الدولة: {geo}")
    topic = get_trending_topic(geo)
    article = generate_article(topic)

    image_url = "https://via.placeholder.com/1200x630.png?text=Research+Image"
    post_url = post_to_blogger(f"بحث حول {topic}", article, image_url)
    return post_url

# ==================== التنفيذ ====================

if __name__ == "__main__":
    print("🚀 بدء التشغيل اليدوي...")
    make_article_once(0)
