"""
飞书 API 异步封装。
流式方案：发送内联卡片消息 → 用 patch 逐步更新内容（比 cardkit 流式卡片更简单可靠）。
"""

import asyncio
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
    UpdateMessageRequest,
    UpdateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


def _post_json(content: str, loading: bool = False) -> str:
    """
    生成飞书 post（富文本）JSON 字符串。
    post 格式 API 完全可读，其他 Bot 拉历史时能看到完整内容。
    支持 markdown（代码块、加粗、链接等），超长时自动分段。
    """
    text = "⏳ 思考中..." if loading else content

    # post 消息单个 md 块限制约 4000 字符
    MAX_CHUNK_SIZE = 3800

    if len(text) <= MAX_CHUNK_SIZE:
        chunks = [text]
    else:
        chunks = []
        current_chunk = ""
        for line in text.split('\n'):
            if len(line) > MAX_CHUNK_SIZE:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""
                for i in range(0, len(line), MAX_CHUNK_SIZE):
                    chunks.append(line[i:i + MAX_CHUNK_SIZE])
                continue
            if len(current_chunk) + len(line) + 1 > MAX_CHUNK_SIZE:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk = current_chunk + '\n' + line if current_chunk else line
        if current_chunk:
            chunks.append(current_chunk)

    # 多段时加续篇标记
    content_blocks = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            chunk = f"**（续 {i}）**\n\n{chunk}"
        content_blocks.append([{"tag": "md", "text": chunk}])

    return json.dumps({
        "zh_cn": {"content": content_blocks}
    }, ensure_ascii=False)


class FeishuClient:
    def __init__(self, client: lark.Client, app_id: str = "", app_secret: str = ""):
        self.client = client
        self._app_id = app_id
        self._app_secret = app_secret

    async def _retry_with_backoff(self, coro_func, max_retries: int = 3, initial_delay: float = 0.5):
        """
        执行异步操作，失败时指数退避重试。

        Args:
            coro_func: 返回 coroutine 的可调用对象
            max_retries: 最多重试次数（不包括首次尝试）
            initial_delay: 初始延迟秒数

        Returns:
            操作结果

        Raises:
            最后一次尝试的异常
        """
        delay = initial_delay
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                return await coro_func()
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    print(f"[retry] 第 {attempt + 1} 次失败，{delay:.1f}s 后重试: {e}", flush=True)
                    await asyncio.sleep(delay)
                    delay *= 2  # 指数退避
                else:
                    print(f"[retry] 已达最大重试次数 {max_retries + 1}，放弃", flush=True)

        raise last_error

    # ── 发送消息 ──────────────────────────────────────────────

    async def send_card_to_user(self, open_id: str, content: str = "", loading: bool = True) -> str:
        """向用户发送 post 富文本消息，返回 message_id（带重试）"""
        async def _send():
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("post")
                    .content(_post_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.acreate(req)
            if not resp.success():
                raise RuntimeError(f"发送消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        return await self._retry_with_backoff(_send, max_retries=3)

    async def reply_card(self, message_id: str, content: str = "", loading: bool = True) -> str:
        """回复用户消息（post 富文本），触发通知。返回回复消息的 message_id（带重试）"""
        async def _reply():
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("post")
                    .content(_post_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                raise RuntimeError(f"回复消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        return await self._retry_with_backoff(_reply, max_retries=3)

    # 飞书 post 消息体上限约 30KB，安全阈值设为 25000 字符
    MAX_UPDATE_CHARS = 25000

    # 记录已降级的 message_id，防止重复 reply
    _reply_fallback_sent: set = set()

    async def update_card(self, message_id: str, content: str):
        """用 update（PUT）更新已发送的 post 消息内容。
        超长时自动截断；update 失败时降级为 reply（仅一次）。"""
        # 超长截断
        if len(content) > self.MAX_UPDATE_CHARS:
            content = content[:self.MAX_UPDATE_CHARS] + "\n\n⚠️ *（消息过长，已截断）*"
            print(f"[warn] 消息超长，已截断至 {self.MAX_UPDATE_CHARS} 字符", flush=True)

        class _PermanentError(Exception):
            """不可重试的永久性错误"""
            pass

        async def _update():
            req = (
                UpdateMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    UpdateMessageRequestBody.builder()
                    .msg_type("post")
                    .content(_post_json(content, loading=False))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.aupdate(req)
            if not resp.success():
                # 230072 = 编辑次数上限，重试无意义
                if resp.code == 230072:
                    raise _PermanentError(f"编辑次数上限: {resp.msg}")
                raise RuntimeError(f"update 消息失败: {resp.code} {resp.msg}")

        try:
            await self._retry_with_backoff(_update, max_retries=2)
        except Exception as e:
            # 降级：reply 一条新消息，但同一个 message_id 只降级一次
            if message_id in self._reply_fallback_sent:
                print(f"[warn] update 失败且已降级过，跳过: {e}", flush=True)
                return
            self._reply_fallback_sent.add(message_id)
            # 防止集合无限增长
            if len(self._reply_fallback_sent) > 100:
                self._reply_fallback_sent = set(list(self._reply_fallback_sent)[-50:])
            print(f"[warn] update 失败，reply 降级（仅一次）: {e}", flush=True)
            try:
                await self.reply_card(message_id, content=content[:self.MAX_UPDATE_CHARS], loading=False)
            except Exception as e2:
                print(f"[error] reply 降级也失败: {e2}", flush=True)

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

    async def send_card_to_group(self, chat_id: str, content: str = "", loading: bool = False) -> str:
        """向群聊发送 post 富文本消息，返回 message_id（带重试）"""
        async def _send():
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("post")
                    .content(_post_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.acreate(req)
            if not resp.success():
                raise RuntimeError(f"发送群消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        return await self._retry_with_backoff(_send, max_retries=3)

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

    async def fetch_group_history(
        self,
        chat_id: str,
        limit: int = 30,
        known_names: dict[str, str] | None = None,
    ) -> str:
        """
        拉取群聊最近 N 条消息，格式化为对话历史文本。
        known_names: {open_id: 显示名} 映射，用于标注发言者。
        返回可直接注入 Claude 上下文的字符串，空群返回 ""。
        """
        import ssl, urllib.request, urllib.parse

        known_names = known_names or {}
        ctx = ssl.create_default_context()

        # 1. 获取 tenant_access_token
        token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
        token_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=token_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        loop = asyncio.get_event_loop()

        def _http(req):
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                return json.loads(r.read())

        token_data = await loop.run_in_executor(None, _http, token_req)
        token = token_data.get("tenant_access_token", "")
        if not token:
            return ""

        # 2. 拉取消息列表（倒序，最新在前）
        # 飞书 API page_size 最大 50
        fetch_size = min(limit * 2, 50)
        params = urllib.parse.urlencode({
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": fetch_size,
        })
        msg_req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/im/v1/messages?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        msg_data = await loop.run_in_executor(None, _http, msg_req)
        items = msg_data.get("data", {}).get("items", [])
        if not items:
            return ""

        # 3. 解析并格式化（倒序→正序）
        lines = []
        for item in reversed(items):
            sender_id = item.get("sender", {}).get("id", "")
            msg_type = item.get("msg_type", "")
            raw_content = item.get("body", {}).get("content", "")

            # 只处理文本、post 富文本和卡片（跳过图片、文件等）
            text_content = ""
            if msg_type == "text":
                try:
                    text_content = json.loads(raw_content).get("text", "").strip()
                except Exception:
                    text_content = raw_content.strip()
            elif msg_type == "post":
                # post 富文本（OpenClaw raw 模式 / Claude Bot 新格式）
                try:
                    post = json.loads(raw_content)
                    parts = []
                    for block in post.get("zh_cn", {}).get("content", []):
                        for el in block:
                            t = el.get("text") or el.get("content", "")
                            if t:
                                parts.append(t)
                    text_content = "\n".join(parts).strip()
                except Exception:
                    text_content = raw_content.strip()
            elif msg_type == "interactive":
                # 卡片消息（Bot 回复）：提取 markdown 内容
                # 支持 Card JSON 2.0（schema:2.0）和旧版 Card Kit 1.0
                try:
                    card = json.loads(raw_content)
                    parts = []

                    if card.get("schema") == "2.0":
                        # 新版 Card JSON 2.0（Claude bot 格式）
                        for el in card.get("body", {}).get("elements", []):
                            if el.get("tag") == "markdown":
                                parts.append(el.get("content", ""))
                    else:
                        # 旧版 Card Kit 1.0（OpenClaw / 其他 bot 格式）
                        # header title
                        header = card.get("header", {})
                        title = header.get("title", {})
                        if isinstance(title, dict) and title.get("content"):
                            parts.append(f"**{title['content']}**")
                        # elements 可能是一维或二维数组
                        for el in card.get("elements", []):
                            if isinstance(el, list):
                                sub_els = el
                            else:
                                sub_els = [el]
                            for sub in sub_els:
                                tag = sub.get("tag", "")
                                if tag == "markdown":
                                    parts.append(sub.get("content", ""))
                                elif tag in ("div", "section"):
                                    text_obj = sub.get("text", {})
                                    if isinstance(text_obj, dict):
                                        parts.append(text_obj.get("content", ""))
                                    # fields 数组
                                    for field in sub.get("fields", []):
                                        if isinstance(field, dict):
                                            t = field.get("text", {})
                                            if isinstance(t, dict):
                                                parts.append(t.get("content", ""))

                    text_content = "\n".join(p for p in parts if p).strip()
                    # 跳过纯"思考中"占位卡片或空卡片
                    if text_content in ("⏳ 思考中...", ""):
                        continue
                except Exception:
                    # 解析失败：标记为"卡片消息"，不静默丢弃
                    text_content = "[卡片消息]"
            else:
                continue

            if not text_content:
                continue

            # 截断过长消息
            if len(text_content) > 3000:
                text_content = text_content[:3000] + "…"

            name = known_names.get(sender_id, sender_id[:8] if sender_id else "未知")
            lines.append(f"{name}: {text_content}")

        if not lines:
            return ""

        # 截取到目标数量（多拉的原始消息经过过滤后取前 limit 条）
        lines = lines[-limit:]
        history_text = "\n".join(lines)
        return (
            f"\n\n---\n[群聊最近 {len(lines)} 条消息记录]\n"
            f"{history_text}\n---\n"
        )

    async def send_at_message_to_group(self, chat_id: str, text: str, at_open_id: str, at_name: str) -> str:
        """向群聊发送带 @mention 的文本消息（真正的 @，会触发对方的消息事件）"""
        # 飞书文本消息中 @mention 的格式
        at_text = f'<at user_id="{at_open_id}">{at_name}</at> {text}'
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": at_text}))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"发送 @mention 消息失败: {resp.code} {resp.msg}")
        return resp.data.message_id

    def _get_tenant_token(self) -> str:
        """同步获取 tenant_access_token，供非异步方法使用"""
        import ssl
        import urllib.request
        try:
            ctx = ssl.create_default_context()
            token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
            token_req = urllib.request.Request(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                data=token_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
                return json.loads(r.read()).get("tenant_access_token", "")
        except Exception as e:
            print(f"[feishu] _get_tenant_token 失败: {e}", flush=True)
            return ""

    def get_user_info(self, open_id: str) -> dict | None:
        """通过飞书 API 获取用户基本信息（姓名等）"""
        try:
            import urllib.request
            import ssl

            token = self._get_tenant_token()
            if not token:
                return None

            ctx = ssl.create_default_context()
            req = urllib.request.Request(
                f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}?user_id_type=open_id",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, context=ctx) as resp:
                data = json.loads(resp.read())
                if data.get("code") == 0 and data.get("data", {}).get("user"):
                    user = data["data"]["user"]
                    return {"name": user.get("name", ""), "avatar": user.get("avatar", {}).get("avatar_72", "")}
            return None
        except Exception as e:
            print(f"[feishu] get_user_info 失败: {e}", flush=True)
            return None
