import base64
import hashlib
import os
import struct
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx
from Crypto.Cipher import AES
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse
from openai import OpenAI

app = FastAPI()

CORP_ID = os.environ["CORP_ID"]
SECRET = os.environ["SECRET"]
TOKEN = os.environ["TOKEN"]
ENCODING_AES_KEY = os.environ["ENCODING_AES_KEY"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


# ── 企业微信加解密 ──────────────────────────────────────────────────

def _sign(timestamp: str, nonce: str, *extra: str) -> str:
    items = sorted([TOKEN, timestamp, nonce, *extra])
    return hashlib.sha1("".join(items).encode()).hexdigest()


def _decrypt(encrypted: str) -> bytes:
    key = base64.b64decode(ENCODING_AES_KEY + "=")
    iv = key[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    raw = base64.b64decode(encrypted)
    decrypted = cipher.decrypt(raw)

    pad = decrypted[-1]
    content = decrypted[16:-pad]           # 跳过16字节随机串，去掉填充
    msg_len = struct.unpack(">I", content[:4])[0]
    return content[4 : 4 + msg_len]        # 纯消息体


# ── 企业微信 API ────────────────────────────────────────────────────

async def get_access_token() -> str:
    url = (
        f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        f"?corpid={CORP_ID}&corpsecret={SECRET}"
    )
    async with httpx.AsyncClient() as c:
        r = await c.get(url)
        return r.json()["access_token"]


async def download_media(media_id: str, token: str) -> bytes:
    url = (
        f"https://qyapi.weixin.qq.com/cgi-bin/media/get"
        f"?access_token={token}&media_id={media_id}"
    )
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as c:
        r = await c.get(url)
        return r.content


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
        model="deepseek-vl2",   # 如不可用可换 deepseek-chat
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
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


# ── FastAPI 路由 ────────────────────────────────────────────────────

@app.get("/wx")
async def verify(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """企业微信服务器验证"""
    if _sign(timestamp, nonce, echostr) != msg_signature:
        return PlainTextResponse("forbidden", status_code=403)
    plaintext = _decrypt(echostr)
    return PlainTextResponse(plaintext.decode())


@app.post("/wx")
async def receive(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """接收企业微信消息"""
    body = (await request.body()).decode()
    root = ET.fromstring(body)
    encrypted = root.findtext("Encrypt")

    if _sign(timestamp, nonce, encrypted) != msg_signature:
        return PlainTextResponse("forbidden", status_code=403)

    msg = ET.fromstring(_decrypt(encrypted))
    msg_type = msg.findtext("MsgType")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if msg_type == "image":
        media_id = msg.findtext("MediaId")
        token = await get_access_token()
        img_bytes = await download_media(media_id, token)
        content = analyze_image(img_bytes)
        await save_note(f"微信图片_{ts}", content, "image")

    elif msg_type == "file":
        media_id = msg.findtext("MediaId")
        filename = msg.findtext("FileName") or "file"
        token = await get_access_token()
        file_bytes = await download_media(media_id, token)

        ext = filename.rsplit(".", 1)[-1].lower()
        if ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
            content = analyze_image(file_bytes, hint=filename)
        else:
            # 非图片文件：记录基本信息，后续可扩展PDF解析
            content = (
                f"# {filename}\n\n"
                f"- **类型**: {ext.upper()}\n"
                f"- **大小**: {len(file_bytes):,} bytes\n"
                f"- **接收时间**: {ts}\n\n"
                f"> 文件已接收，如需内容分析请回复文件内容。"
            )
        await save_note(f"{filename}_{ts}", content, "file")

    return PlainTextResponse("success")
