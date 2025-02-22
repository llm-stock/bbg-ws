import os
import json
import asyncio
import time
import aiohttp
import websockets
from typing import Optional
import dotenv

dotenv.load_dotenv()

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
        for _ in range(5):  # 初始化速率限制令牌
            self.rate_limiter.put_nowait(None)

    async def _escape_markdown(self, text: str) -> str:
        """处理MarkdownV2转义，增强空值处理"""
        if not text:
            return ''
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

            async with self.session.post(photo_url, data=form) as resp:
                if resp.status == 200:
                    return True
                error = await resp.text()
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

    async def _construct_message(self, article: dict) -> str:
        """健壮的消息构建方法"""
        try:
            # 安全获取字段值
            raw_title = article.get('title', '') or 'Untitled'
            raw_description = article.get('description', '')[:300]
            translated_title = article.get('translated_title', '')
            translated_description = article.get('translated_description', '')
            stock_tickers = str(article.get('stock_tickers', '暂无代码'))
            article_link = article.get('link', '#')

            # 转义处理
            title = await self._escape_markdown(raw_title)
            description = await self._escape_markdown(raw_description)
            stock_info = await self._escape_markdown(stock_tickers)

            # 构建消息段落
            message_parts = []
            if title:
                message_parts.append(title)
            if translated_title:
                message_parts.append(f"*{await self._escape_markdown(translated_title)}*")
            if description:
                message_parts.append(description)
            if translated_description:
                message_parts.append(f"*{await self._escape_markdown(translated_description)}*")

            message_parts.append(f"*Stock*: `{stock_info}`")
            message_parts.append(f"[Read ALL]({article_link})")

            # 过滤空段落并拼接
            filtered_parts = list(filter(None, message_parts))
            return "\n\n".join(filtered_parts) + "\n\n" + "▬" * 20
        except Exception as e:
            print(f"[ERROR] 消息构建异常: {str(e)}")
            return f"{raw_title}\n\n[阅读全文]({article.get('link', '#')})"

    async def send_article(self, article: dict):
        """增强的消息发送方法，包含速率控制和重试机制"""
        async with self.semaphore:
            await self.rate_limiter.get()  # 等待可用令牌
            try:
                for attempt in range(self.retry_limit):
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
                            success = await self._send_photo_message(media_url, message)
                            if success:
                                break
                            print("图片发送失败，尝试纯文本方式...")

                        # 纯文本回退
                        success = await self._send_text_message(message)
                        if success:
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
                    timeout=15
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    print(f"[ERROR] Dify API响应异常: {resp.status} - {error}")
                    return {}

                result = await resp.json()
                if not result.get('data', {}).get('outputs'):
                    print(f"[WARN] 无效的翻译结果: {json.dumps(result, indent=2)}")
                    return {}

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
        """增强的文章处理方法"""
        print(f"\n[Processing] 收到 {len(articles)} 篇新文章")

        try:
            for index, article in enumerate(articles, 1):
                try:
                    if not isinstance(article, dict):
                        print(f"[WARN] 无效的文章格式，索引 {index}")
                        continue

                    print(f"\n[Article {index}] 处理: {article.get('title', '无标题')[:50]}...")

                    # 安全获取字段
                    title = article.get('title', '')
                    description = article.get('description', '')

                    # 翻译处理
                    translation = await self.translator.translate_news(title, description)
                    processed = article.copy()

                    if translation:
                        processed.update({
                            'translated_title': translation.get('title', ''),
                            'translated_description': translation.get('description', '')
                        })

                    # 发送处理后的文章
                    await self.bot.send_article(processed)
                except KeyError as e:
                    print(f"[ERROR] 文章缺少必要字段: {str(e)}")
                    print(f"[DUMP] 问题文章: {json.dumps(article, indent=2)}")
                except Exception as e:
                    print(f"[ERROR] 处理文章异常: {str(e)}")
                    await self.bot.send_article(article)  # 降级处理
        finally:
            await self.translator.close()

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