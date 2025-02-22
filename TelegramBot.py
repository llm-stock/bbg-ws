import os
import json
import asyncio
import time
import aiohttp
import websockets
from datetime import datetime
from urllib.parse import quote
import dotenv
from dateutil import parser

dotenv.load_dotenv()

# 配置参数
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

    async def _escape_markdown(self, text: str) -> str:
        """优化的MarkdownV2转义方法"""
        escape_chars = '_*[]()~`>#+-=|{}.!'
        return text.translate(str.maketrans({c: f'\\{c}' for c in escape_chars}))

    async def _send_photo_message(self, url: str, caption: str):
        """发送带图片的消息（自动处理长说明）"""
        photo_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        params = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": url,
            "caption": caption[:1024],  # Telegram图片说明长度限制
            "parse_mode": "MarkdownV2"
        }
        async with self.session.post(photo_url, data=params) as resp:
            if resp.status != 200:
                error = await resp.text()
                print(f"Telegram图片发送失败: {error}")
                return False
            return True

    async def _send_text_message(self, message: str):
        """发送纯文本消息"""
        text_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4096],  # Telegram文本长度限制
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True
        }
        async with self.session.post(text_url, json=payload) as resp:
            if resp.status != 200:
                error = await resp.text()
                print(f"Telegram文本发送失败: {error}")
                return False
            return True

    async def _construct_message(self, article: dict) -> str:
        """构建结构化消息内容"""
        # 转义处理
        title = await self._escape_markdown(article.get('title', ''))
        description = await self._escape_markdown((article.get('description') or '')[:300] + '')
        stock_info = await self._escape_markdown(article.get('stock_stickers', ''))
        source = await self._escape_markdown(article.get('source', '').upper())
        category = await self._escape_markdown(article.get('category', ''))

        # 格式化发布时间
        pub_date = parser.parse(article['published']).strftime('%Y-%m-%d %H:%M UTC')

        # 构建消息结构
        message_lines = [
            f"**{title}**",
            f"*Stock*: `{stock_info}`" if stock_info else "",
            f"\n{description}" if description else "",
            f"\n[Read ALL]({article['link']})"
        ]

        # 清理空行并添加分隔符
        message = "\n".join(filter(None, message_lines))
        message += "\n\n" + "▬" * 20  # 添加分隔线
        return message

    async def send_article(self, article: dict):
        """带重试机制的消息发送"""
        async with self.semaphore:
            for attempt in range(self.retry_limit):
                try:
                    # 速率控制
                    now = time.monotonic()
                    if now - self.last_sent < RATE_LIMIT:
                        await asyncio.sleep(RATE_LIMIT - (now - self.last_sent))

                    message = await self._construct_message(article)
                    media_url = article.get('media_url', '')

                    # 优先尝试发送带图片的消息
                    if media_url:
                        success = await self._send_photo_message(media_url, message)
                        if success:
                            break
                        print("图片发送失败，尝试纯文本方式...")

                    # 纯文本回退方案
                    success = await self._send_text_message(message)
                    if success:
                        break

                except Exception as e:
                    print(f"发送尝试 {attempt + 1} 失败: {str(e)}")
                    await asyncio.sleep(2 ** attempt)  # 指数退避
                finally:
                    self.last_sent = time.monotonic()

class NewsTranslator:
    def __init__(self, api_key):
        self.api_key = api_key
        self.session = aiohttp.ClientSession()
        self.endpoint = DIFY_ENDPOINT

    async def translate_news(self, title: str, description: str) -> dict:
        """Translate news using Dify API."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "inputs": {},
            "response_mode": "blocking",  # Changed to blocking for simpler handling
            "user": "bloomberg-news",
            "title": title,
            "description": description
        }

        try:
            async with self.session.post(
                self.endpoint,
                headers=headers,
                json=payload
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"Dify API error: {response.status}")
                    return None
        except Exception as e:
            print(f"Translation request failed: {str(e)}")
            return None

    async def close(self):
        """Close the aiohttp session."""
        await self.session.close()



class RobustWSClient:
    def __init__(self):
        self.bot = EnhancedTelegramBot()
        self.reconnect_count = 0

    async def _safe_connect(self):
        """带指数退避的重连机制"""
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

    async def listen_forever(self):
        """持久化监听循环"""
        while True:
            try:
                async with await self._safe_connect() as ws:
                    print("成功连接WebSocket服务器")
                    self.reconnect_count = 0
                    await self._message_loop(ws)
            except websockets.ConnectionClosed as e:
                print(f"连接异常关闭: {e.code} {e.reason}")

    async def _message_loop(self, ws):
        """消息处理循环"""
        async for message in ws:
            try:
                data = json.loads(message)
                if data.get('type') == 'update':
                    await self._process_update(data['articles'])
                elif data.get('type') == 'history':
                    print(f"收到历史数据，共{len(data['articles'])}条")
            except Exception as e:
                print(f"消息处理错误: {str(e)}")

    async def _process_update(self, articles):
        """处理新闻更新"""
        print(f"收到{len(articles)}条新文章")
        translator = NewsTranslator(DIFY_API_KEY)

        for article in articles:
            try:
                # 只翻译标题和描述部分
                translation = await translator.translate_news(
                    title=article['title'],
                    description=article.get('description', '')
                )

                if translation and isinstance(translation.get('answer'), str):
                    # 在原文后添加翻译
                    article['description'] = (
                        f"{article.get('description', '')}\n\n"
                        f"Translation:\n{translation['answer']}"
                    )

                # 发送到 Telegram
                await self.bot.send_article(article)

            except Exception as e:
                print(f"处理文章失败: {str(e)}")
                await self.bot.send_article(article)  # 发送原始内容

        await translator.close()


async def main():
    client = RobustWSClient()
    await client.listen_forever()


if __name__ == "__main__":
    # 环境验证
    required_vars = ['TELEGRAM_TOKEN', 'TELEGRAM_CHAT_ID', 'WEBSOCKET_URI']
    missing = [var for var in required_vars if not os.getenv(var)]

    if missing:
        print(f"缺少必要环境变量: {', '.join(missing)}")
        exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n客户端已安全关闭")