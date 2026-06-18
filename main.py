import asyncio
import base64
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx
from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import PlainTextResponse
from openai import OpenAI

app = FastAPI()

WX_APP_ID = os.environ["WX_APP_ID"]
WX_APP_SECRET = os.environ["WX_APP_SECRET"]
WX_TOKEN = os.environ["WX_TOKEN"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


# ── 签名验证 ────────────────────────────────────────────────────────

def _check_signature(timestamp: str, nonce: str, signature: str) -> bool:
    items = sorted([WX_TOKEN, timestamp, nonce])
    expected = hashlib.sha1("".join(items).encode()).hexdigest()
    return expected == signature


def _reply_text(to_user: str, from_user: str, content: str) -> str:
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"</xml>"
    )


# ── 微信公众号 API ──────────────────────────────────────────────────

async def get_access_token() -> str:
    url = (
        f"https://api.weixin.qq.com/cgi-bin/token"
        f"?grant_type=client_credential&appid={WX_APP_ID}&secret={WX_APP_SECRET}"
    )
    async with httpx.AsyncClient() as c:
        r = await c.get(url)
        return r.json()["access_token"]


async def download_media(media_id: str, token: str) -> bytes:
    url = f"https://api.weixin.qq.com/cgi-bin/media/get?access_token={token}&media_id={media_id}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as c:
        r = await c.get(url)
        return r.content


# ── 网页抓取 ────────────────────────────────────────────────────────

async def fetch_url_content(url: str) -> str:
    """抓取网页正文，微信文章走 Jina Reader"""
    if "mp.weixin.qq.com" in url:
        fetch_url = f"https://r.jina.ai/{url}"
    else:
        fetch_url = url

    headers = {"User-Agent": "Mozilla/5.0 (compatible; ObsidianBot/1.0)"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(fetch_url, headers=headers)
        return r.text[:8000]


# ── AI 分析 ────────────────────────────────────────────────────────

def analyze_image(image_bytes: bytes, hint: str = "") -> str:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = (
        "请详细分析这张图片的内容，提取所有文字信息、关键概念和要点，"
        "整理成结构清晰的Markdown笔记。使用##标题、-列表等Markdown格式。"
    )
    if hint:
        prompt += f"\n文件名提示：{hint}"

    resp = deepseek.chat.completions.create(
        model="deepseek-vl2",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        max_tokens=2048,
    )
    return resp.choices[0].message.content


def summarize_url(title: str, url: str, page_text: str) -> str:
    resp = deepseek.chat.completions.create(
        model="deepseek-chat",
        messages=[{
            "role": "user",
            "content": (
                f"请总结以下网页内容，整理成结构清晰的Markdown笔记，"
                f"包含关键要点、重要概念、结论等。\n"
                f"标题：{title}\n链接：{url}\n\n内容：\n{page_text}"
            ),
        }],
        max_tokens=2048,
    )
    return resp.choices[0].message.content


# ── Supabase 写入 ───────────────────────────────────────────────────

async def save_note(title: str, content: str, source_type: str):
    async with httpx.AsyncClient() as c:
        await c.post(
            f"{SUPABASE_URL}/rest/v1/wechat_notes",
            json={"title": title, "content": content, "source_type": source_type},
            headers=SUPABASE_HEADERS,
        )


# ── 后台处理任务 ────────────────────────────────────────────────────

async def process_image(media_id: str, ts: str):
    token = await get_access_token()
    img_bytes = await download_media(media_id, token)
    content = analyze_image(img_bytes)
    await save_note(f"微信图片_{ts}", content, "image")


async def process_link(title: str, url: str, ts: str):
    page_text = await fetch_url_content(url)
    content = summarize_url(title or url, url, page_text)
    await save_note(f"{title or '链接'}_{ts}", content, "link")


async def process_text(text: str, ts: str):
    urls = re.findall(r'https?://\S+', text)
    if not urls:
        return
    url = urls[0]
    page_text = await fetch_url_content(url)
    content = summarize_url(url, url, page_text)
    await save_note(f"链接_{ts}", content, "link")


# ── FastAPI 路由 ────────────────────────────────────────────────────

@app.get("/wx")
async def verify(
    signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """公众号服务器验证"""
    if not _check_signature(timestamp, nonce, signature):
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(echostr)


@app.post("/wx")
async def receive(
    request: Request,
    background_tasks: BackgroundTasks,
    signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """接收公众号消息"""
    if not _check_signature(timestamp, nonce, signature):
        return PlainTextResponse("forbidden", status_code=403)

    body = (await request.body()).decode()
    root = ET.fromstring(body)

    msg_type = root.findtext("MsgType")
    from_user = root.findtext("FromUserName")
    to_user = root.findtext("ToUserName")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if msg_type == "image":
        media_id = root.findtext("MediaId")
        background_tasks.add_task(process_image, media_id, ts)
        reply = _reply_text(from_user, to_user, "图片已收到，正在分析中，稍后在Obsidian查看笔记~")

    elif msg_type == "link":
        title = root.findtext("Title") or ""
        url = root.findtext("Url") or ""
        background_tasks.add_task(process_link, title, url, ts)
        reply = _reply_text(from_user, to_user, f"链接已收到，正在总结中~\n《{title}》")

    elif msg_type == "text":
        content = root.findtext("Content") or ""
        background_tasks.add_task(process_text, content, ts)
        urls = re.findall(r'https?://\S+', content)
        if urls:
            reply = _reply_text(from_user, to_user, "链接已收到，正在抓取总结~")
        else:
            reply = _reply_text(from_user, to_user, "暂只支持图片和链接哦")

    else:
        reply = _reply_text(from_user, to_user, f"暂不支持 {msg_type} 类型")

    return PlainTextResponse(reply, media_type="application/xml")
