"""
飞书 API 异步封装。
流式方案：发送内联卡片消息 → 用 patch 逐步更新内容（比 cardkit 流式卡片更简单可靠）。
"""

import json
import os
import tempfile
import time
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1.model import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


def _card_json(content: str, loading: bool = False) -> str:
    """生成卡片 JSON 字符串（Card JSON 2.0）"""
    elements = []
    if loading:
        elements.append({"tag": "markdown", "content": "⏳ 思考中..."})
    else:
        elements.append({"tag": "markdown", "content": content})
    return json.dumps({
        "schema": "2.0",
        "body": {"elements": elements},
    }, ensure_ascii=False)


class FeishuClient:
    def __init__(self, client: lark.Client, app_id: str = "", app_secret: str = ""):
        self.client = client
        self._app_id = app_id
        self._app_secret = app_secret

    # ── 发送消息 ──────────────────────────────────────────────

    async def send_card_to_user(self, open_id: str, content: str = "", loading: bool = True) -> str:
        """向用户发送卡片消息，返回 message_id"""
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("interactive")
                .content(_card_json(content, loading=loading))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"发送卡片消息失败: {resp.code} {resp.msg}")
        return resp.data.message_id

    async def reply_card(self, message_id: str, content: str = "", loading: bool = True) -> str:
        """回复用户消息（卡片形式），触发通知。返回回复消息的 message_id"""
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(_card_json(content, loading=loading))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.areply(req)
        if not resp.success():
            raise RuntimeError(f"回复卡片消息失败: {resp.code} {resp.msg}")
        return resp.data.message_id

    async def update_card(self, message_id: str, content: str):
        """用 patch 更新已发送的卡片内容（流式更新核心）"""
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(_card_json(content, loading=False))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.apatch(req)
        if not resp.success():
            print(f"[warn] patch 卡片失败: {resp.code} {resp.msg}")

    async def download_image(self, message_id: str, image_key: str) -> str:
        """下载飞书图片到临时文件，返回本地路径"""
        import asyncio
        import ssl
        import urllib.request

        ctx = ssl.create_default_context()

        # 获取 tenant_access_token
        token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
        token_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=token_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
            token = json.loads(r.read())["tenant_access_token"]

        # 下载图片
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image"
        img_req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        tmp_path = os.path.join(tempfile.gettempdir(), f"feishu-img-{int(time.time())}.jpg")
        with urllib.request.urlopen(img_req, context=ctx, timeout=15) as r:
            ct = r.headers.get("Content-Type", "")
            if "png" in ct:
                tmp_path = tmp_path.replace(".jpg", ".png")
            elif "gif" in ct:
                tmp_path = tmp_path.replace(".jpg", ".gif")
            with open(tmp_path, "wb") as f:
                f.write(r.read())

        return tmp_path

    async def send_text_to_user(self, open_id: str, text: str) -> str:
        """发送纯文本消息"""
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"发送文本消息失败: {resp.code} {resp.msg}")
        return resp.data.message_id
