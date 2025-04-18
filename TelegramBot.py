# import hashlib
import html
import os
import json
import asyncio
import time
# from pyexpat.errors import messages

import aiohttp
import websockets
from typing import Optional
import dotenv
import DatabaseManager as db

dotenv.load_dotenv()
# dbConn = db.DatabaseManager()
# 环境配置
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
WEBSOCKET_URI = os.getenv('WEBSOCKET_URI')
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_ENDPOINT = os.getenv('DIFY_ENDPOINT')
RECONNECT_DELAY = 10
RATE_LIMIT = 1.2  # 严格遵循Telegram的速率限制


class EnhancedTelegramBot:
    def __init__(self):
        self.session = aiohttp.ClientSession()
        self.last_sent = time.monotonic()
        self.semaphore = asyncio.Semaphore(5)  # 并发控制
        self.retry_limit = 3
        self.rate_limiter = asyncio.Queue()
        self.send_lock = asyncio.Lock()
        for _ in range(5):  # 初始化速率限制令牌
            self.rate_limiter.put_nowait(None)

    async def _escape_markdown(self, text: str) -> str:
        """优化后的MarkdownV2转义方法"""
        if not text:
            return ''
        # 需要转义的字符（排除代码块中的反引号）
        escape_chars = '_*[]()~`>#+-=|{}.!'
        return text.translate(str.maketrans({c: f'\\{c}' for c in escape_chars}))

    async def _send_photo_message(self, url: str, caption: str) -> bool:
        """使用FormData发送带图片的消息"""
        try:
            # 验证图片URL有效性
            async with self.session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    print(f"图片资源不可用: {url}")
                    return False

            photo_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            form = aiohttp.FormData()
            form.add_field('chat_id', TELEGRAM_CHAT_ID)
            form.add_field('photo', url)
            form.add_field('caption', await self._escape_markdown(caption[:1024]))
            form.add_field('parse_mode', "MarkdownV2")
            form.add_field('disable_web_page_preview', "true")
            async with self.session.post(photo_url, data=form) as resp:
                if resp.status == 200:
                    return True
                error = await resp.text()
                print(f"图片地址: {url}")
                print(f"Telegram图片发送失败[{resp.status}]: {error}")
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"图片发送网络错误: {str(e)}")
            return False

    async def _send_text_message(self, message: str) -> bool:
        """发送纯文本消息，增强错误处理"""
        text_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4096],
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True
        }

        try:
            async with self.session.post(text_url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    return True
                error = await resp.text()
                print(f"Telegram文本发送失败[{resp.status}]: {error}")
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"文本发送网络错误: {str(e)}")
            return False

    async def _escape_markdown(self, text: str) -> str:
        """优化Markdown转义逻辑"""
        if not text:
            return ''
        # 需要转义的字符（排除用于格式化的符号）
        escape_chars = '_>#+-=|{}.!'
        return text.translate(str.maketrans({c: f'\\{c}' for c in escape_chars}))

    async def _construct_message(self, article: dict) -> str:
        """修复所有格式问题"""
        try:
            # 安全获取字段值（新增HTML解码）
            raw_title = html.unescape(article.get('title', '') or 'Untitled')
            raw_description = html.unescape(article.get('description', '')[:300])
            translated_title = html.unescape(article.get('translated_title', ''))
            translated_description = html.unescape(article.get('translated_description', ''))

            # 处理股票代码
            stock_tickers = str(article.get('stock_tickers', ''))
            stock_info = f"`{stock_tickers}`"  # 代码块处理

            # 构建消息段落（修复格式）
            message_parts = []
            if raw_title:
                message_parts.append(await self._escape_markdown(raw_title))
            if translated_title:
                # 手动添加星号，不转义
                message_parts.append(f"*{await self._escape_markdown(translated_title)}*")
            if raw_description:
                escaped_raw_desc = await self._escape_markdown(raw_description.replace('-', '\\-'))
                message_parts.append(escaped_raw_desc)
            if translated_description:
                escaped_trans_desc = await self._escape_markdown(translated_description.replace('-', '\\-'))
                message_parts.append(f"*{escaped_trans_desc}*")

            # 股票信息和链接（确保存在）
            message_parts.append(f"*Stock*: {stock_info}")
            message_parts.append(f"[Read ALL]({article.get('link', '#')})")  # 确保链接存在

            # 过滤空段落并拼接
            filtered_parts = list(filter(None, message_parts))
            return "\n\n".join(filtered_parts) + "\n\n" + "▬" * 20

        except Exception as e:
            print(f"[ERROR] 消息构建异常: {str(e)}")
            return f"{raw_title}\n\n[阅读全文]({article.get('link', '#')})"

    async def send_article(self, article: dict):
        """增强的消息发送方法，包含速率控制和重试机制"""
        async with self.send_lock:

            async with self.semaphore:
                await self.rate_limiter.get()  # 等待可用令牌
                try:
                    message_sent = False
                    for attempt in range(self.retry_limit):
                        if message_sent:
                            break
                        try:
                            # 速率控制
                            now = time.monotonic()
                            if now - self.last_sent < RATE_LIMIT:
                                await asyncio.sleep(RATE_LIMIT - (now - self.last_sent))

                            message = await self._construct_message(article)
                            media_url = article.get('media_url', '')

                            # 优先发送带图片的消息
                            success = False
                            if media_url:
                                message_sent = await self._send_photo_message(media_url, message)
                                if message_sent:
                                    break
                                print("图片发送失败，尝试纯文本方式...")

                            # 纯文本回退
                            message_sent = await self._send_text_message(message)
                            if message_sent:
                                break
                        except Exception as e:
                            print(f"发送尝试 {attempt + 1} 失败: {str(e)}")
                            await asyncio.sleep(2 ** attempt)

                    else:
                        print(f"消息发送失败，已达最大重试次数: {self.retry_limit}")
                finally:
                    self.last_sent = time.monotonic()
                    self.rate_limiter.put_nowait(None)  # 释放令牌


class NewsTranslator:
    def __init__(self, api_key: str, endpoint: str):
        self.api_key = api_key
        self.endpoint = endpoint
        self.session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        """按需创建会话"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self):
        """安全关闭会话"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def translate_news(self, title: str, description: str) -> dict:
        """增强的翻译方法，包含超时控制"""
        await self._ensure_session()
        print(f"[Translator] 开始翻译: {title[:50]}...")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "inputs": {
                "title": title,
                "description": description
            },
            "response_mode": "blocking",
            "user": "bloomberg-news"
        }

        try:
            async with self.session.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=30
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    print(f"[ERROR] Dify API响应异常: {resp.status} - {error}")
                    return {}
                print("[Translator] 正在解析JSON响应...")
                result = await resp.json()
                if not result.get('data', {}).get('outputs'):
                    print(f"[WARN] 无效的翻译结果: {json.dumps(result, indent=2)}")
                    return {}
                print(f"[Translator] 成功翻译: {title[:30]}...")
                return result['data']['outputs']
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"[ERROR] 翻译请求失败: {str(e)}")
            return {}
        except json.JSONDecodeError:
            print("[ERROR] 无效的JSON响应")
            return {}


class RobustWSClient:
    def __init__(self):
        self.bot = EnhancedTelegramBot()
        self.translator = NewsTranslator(DIFY_API_KEY, DIFY_ENDPOINT)
        self.reconnect_count = 0

    async def _safe_connect(self):
        """带指数退避的WebSocket连接"""
        while True:
            try:
                return await websockets.connect(
                    WEBSOCKET_URI,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2 ** 20  # 1MB
                )
            except Exception as e:
                delay = min(RECONNECT_DELAY * (2 ** self.reconnect_count), 300)
                print(f"连接失败: {str(e)}，{delay}秒后重试...")
                await asyncio.sleep(delay)
                self.reconnect_count = min(self.reconnect_count + 1, 5)

    async def _message_loop(self, ws):
        """增强的消息处理循环"""
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                    if data.get('type') == 'update':
                        await self._process_update(data.get('articles', []))
                    elif data.get('type') == 'history':
                        print(f"收到历史数据，共{len(data.get('articles', []))}条")
                except json.JSONDecodeError:
                    print("[ERROR] 无效的JSON消息")
                except KeyError as e:
                    print(f"[ERROR] 消息格式错误，缺少字段: {str(e)}")
                except Exception as e:
                    print(f"[ERROR] 消息处理异常: {str(e)}")
        except websockets.ConnectionClosed as e:
            print(f"连接关闭: {e.code} {e.reason}")
            raise

    async def _process_update(self, articles):
        """增加超时控制的处理逻辑"""
        print(f"\n[Processing] 收到 {len(articles)} 篇新文章")
        # 新增处理队列
        processing_queue = asyncio.Queue()
        for article in articles:
                #processing_queue.put_nowait(article)
                await processing_queue.put(article)
        while not processing_queue.empty():
            article = await processing_queue.get()
            try:
                await self._process_single_article(article)
            finally:
                processing_queue.task_done()

    async def _process_single_article(self, article: dict):
        """原子化处理单篇文章"""




        # 阶段1: 获取翻译结果
        try:
            translated = await self.translator.translate_news(
                article.get('title', ''),
                article.get('description', '')
            )
        except Exception as e:
            translated = None
            print(f"[ERROR] 翻译失败: {str(e)}")

        # 阶段2: 构建最终消息
        processed = article.copy()
        if translated:
            processed.update({
                'translated_title': translated.get('title', ''),
                'translated_description': translated.get('description', '')
            })

        # 阶段3: 发送完整消息
        await self.bot.send_article(processed)

        # 记录已处理
        # self.processed_ids.add(article_id)
    async def listen_forever(self):
        """持久化监听循环"""
        while True:
            try:
                async with (await self._safe_connect()) as ws:
                    print("成功连接WebSocket服务器")
                    self.reconnect_count = 0
                    await self._message_loop(ws)
            except Exception as e:
                print(f"连接异常: {str(e)}")
                await asyncio.sleep(RECONNECT_DELAY)


async def main():
    client = RobustWSClient()
    await client.listen_forever()


if __name__ == "__main__":
    required_vars = ['TELEGRAM_TOKEN', 'TELEGRAM_CHAT_ID', 'WEBSOCKET_URI']
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        print(f"缺少必要环境变量: {', '.join(missing)}")
        exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n客户端已安全关闭")
