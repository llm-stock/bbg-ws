import asyncio
import json
import os
import time
from collections import deque
from threading import Lock
from urllib.parse import urlparse
import aiohttp
import dotenv
import feedparser
import pytz
from dateutil import parser
from websockets.exceptions import ConnectionClosedOK
from websockets.legacy.server import WebSocketServerProtocol, serve
from xml.etree import ElementTree as ET
from DatabaseManager import DatabaseManager
from dateutil.tz import UTC

dotenv.load_dotenv()
# 全局配置
# RSS_URL = os.getenv('BLOOMBERG_RSS_URL')
NEWS_SITEMAP = os.getenv('BLOOMBERG_NEWS_SITEMAP')
CHECK_INTERVAL = 60  # 数据检查间隔（秒）
SITEMAP_INTERVAL = 60  # 站点地图抓取间隔（秒）
PING_INTERVAL = 20
PING_TIMEOUT = 20

# 线程安全连接管理
connected_clients = deque()
clients_lock = Lock()


class NewsCache:
    def __init__(self):
        self.latest_pub_date = None
        self.cache = deque(maxlen=1000)
        self.lock = Lock()
        self.db = DatabaseManager()
        self.page_size = 100

    def get_history(self, page=1):
        """分页获取历史数据"""
        start_idx = (page - 1) * self.page_size
        end_idx = start_idx + self.page_size
        total = self.db.get_total_count()
        history = self.db.get_history_page(start_idx, self.page_size)
        return {
            "articles": history,
            "total": total,
            "page": page,
            "page_size": self.page_size,
            "total_pages": (total + self.page_size - 1) // self.page_size
        }
    def _process_entries(self, entries):
        """处理并存储条目，返回分页结果"""
        valid_entries = [e for e in entries if e is not None]
        sorted_entries = sorted(valid_entries,
                              key=lambda x: parser.parse(x["published"]))

        new_articles = []
        with self.lock:
            for entry in sorted_entries:
                pub_date = parser.parse(entry["published"])
                if not self.latest_pub_date or pub_date > self.latest_pub_date:
                    new_articles.append(entry)
                    self.cache.append(entry)
                    if not self.latest_pub_date or pub_date > self.latest_pub_date:
                        self.latest_pub_date = pub_date

            if new_articles:
                self.db.save_news(new_articles)

        # 对新文章进行分页
        total = len(new_articles)
        return {
            "articles": new_articles[:self.page_size],
            "total": total,
            "page": 1,
            "page_size": self.page_size,
            "total_pages": (total + self.page_size - 1) // self.page_size
        }
    async def fetch(self):
        """统一数据抓取入口"""
        try:
            # # 并行获取数据源
            # rss, sitemap = await asyncio.gather(
            #     self._fetch_rss(),
            #     self._fetch_sitemap()
            # )
            rss, sitemap = await  self._fetch_sitemap()
            return self._process_entries(rss + sitemap)
        except Exception as e:
            print(f"数据抓取失败: {str(e)}")
            return []

    # async def _fetch_rss(self):
    #     """处理RSS源"""
    #     async with aiohttp.ClientSession() as session:
    #         try:
    #             async with session.get(RSS_URL) as resp:
    #                 data = await resp.text()
    #                 feed = feedparser.parse(data)
    #                 return [self._parse_rss_entry(e) for e in feed.entries]
    #         except Exception as e:
    #             print(f"RSS抓取失败: {str(e)}")
    #             return []

    def _parse_rss_entry(self, entry):
        """解析RSS条目"""
        try:
            pub_date = parser.parse(entry.published).astimezone(UTC)
            tags = getattr(entry, "tags", None)
            stock_tickers = ", ".join(t.term for t in tags) if tags else ""
            return {
                "guid": entry.get("id", entry.link),
                "title": entry.title,
                "description": entry.description,
                "link": entry.link,
                "published": pub_date.isoformat(),
                "stock_tickers": stock_tickers,
                "media_url": entry.enclosures[0].href if entry.enclosures else "",
                "source": "rss"
            }
        except Exception as e:
            print(f"RSS条目解析失败: {str(e)}")
            return None

    async def _fetch_sitemap(self):
        """处理站点地图"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(NEWS_SITEMAP, timeout=10) as resp:
                    xml_data = await resp.text()
                    return self._parse_sitemap(xml_data)
            except Exception as e:
                print(f"Sitemap抓取失败: {str(e)}")
                return []

    def _parse_sitemap(self, xml_data):
        """解析XML站点地图"""
        namespaces = {
            'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9',
            'news': 'http://www.google.com/schemas/sitemap-news/0.9',
            'image': 'http://www.google.com/schemas/sitemap-image/1.1'
        }

        entries = []
        try:
            root = ET.fromstring(xml_data)
            for url in root.findall('ns:url', namespaces):
                entry = self._parse_sitemap_entry(url, namespaces)
                if entry: entries.append(entry)
        except Exception as e:
            print(f"Sitemap解析失败: {str(e)}")
        return entries

    def _parse_sitemap_entry(self, url, namespaces):
        """解析单个站点地图条目"""
        try:
            loc = url.find('ns:loc', namespaces).text
            news = url.find('news:news', namespaces)

            # 解析元数据
            pub_date = parser.parse(news.find('news:publication_date', namespaces).text).astimezone(UTC)
            title = news.find('news:title', namespaces).text

            # 股票代码处理
            stock_elem = news.find('news:stock_tickers', namespaces)
            stock_tickers = stock_elem.text if stock_elem is not None else ""

            # 图片处理
            image = url.find('image:image', namespaces)
            media_url = image.find('image:loc', namespaces).text if image is not None else ""

            return {
                "guid": loc,  # 从URL生成唯一ID
                "title": title,
                "link": loc,
                "published": pub_date.isoformat(),
                "stock_tickers": stock_tickers,
                "media_url": media_url,
                "source": "sitemap",
                "description": ""  # Sitemap无描述字段
            }
        except Exception as e:
            print(f"条目解析异常: {str(e)}")
            return None

    def _process_entries(self, entries):
        """处理并存储条目"""
        valid_entries = [e for e in entries if e is not None]
        sorted_entries = sorted(valid_entries,
                                key=lambda x: parser.parse(x["published"]))

        new_articles = []
        with self.lock:
            for entry in sorted_entries:
                pub_date = parser.parse(entry["published"])
                if not self.latest_pub_date or pub_date > self.latest_pub_date:
                    new_articles.append(entry)
                    self.cache.append(entry)
                    if not self.latest_pub_date or pub_date > self.latest_pub_date:
                        self.latest_pub_date = pub_date

            if new_articles:
                self.db.save_news(new_articles)
        return new_articles


news_cache = NewsCache()


async def broadcast_news():
    """新闻广播主循环"""
    last_sitemap_fetch = 0
    while True:
        try:
            # 定时抓取sitemap
            now = time.time()
            if now - last_sitemap_fetch > SITEMAP_INTERVAL:
                print("开始抓取站点地图...")
                await news_cache._fetch_sitemap()
                last_sitemap_fetch = now

            # 获取并广播新文章
            new_articles = await news_cache.fetch()
            if new_articles:
                print(f"广播 {len(new_articles)} 条新文章")
                msg = json.dumps({
                    "type": "update",
                    "count": len(new_articles),
                    "articles": new_articles
                })

                # 并行发送
                with clients_lock:
                    tasks = [asyncio.create_task(safe_send(ws, msg))
                             for ws in list(connected_clients)]
                    await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            print(f"广播异常: {str(e)}")
        finally:
            await asyncio.sleep(CHECK_INTERVAL)


async def safe_send(websocket, message):
    """安全发送消息"""
    try:
        await websocket.send(message)
    except (ConnectionClosedOK, asyncio.TimeoutError):
        await disconnect_client(websocket)
    except Exception as e:
        print(f"发送错误: {str(e)}")
        await disconnect_client(websocket)


async def disconnect_client(websocket):
    """断开客户端连接"""
    with clients_lock:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
    print(f"客户端断开: {websocket.remote_address}")


async def client_handler(websocket: WebSocketServerProtocol):
    """客户端连接处理器"""
    remote = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
    print(f"新连接来自: {remote}")

    try:
        with clients_lock:
            connected_clients.append(websocket)

        # 发送历史数据
        history = json.dumps({
            "type": "history",
            **news_cache.get_history(page=1)
        })
        await safe_send(websocket, history)

        # 消息监听循环
        async for message in websocket:
            await handle_client_message(message, remote, websocket)

    except ConnectionClosedOK:
        pass
    except Exception as e:
        print(f"客户端错误 ({remote}): {str(e)}")
    finally:
        await disconnect_client(websocket)
        print(f"连接关闭: {remote}")


async def handle_client_message(message, remote, websocket):
    """处理客户端消息"""
    try:
        cmd = json.loads(message)
        if cmd.get("action") == "get_page":
            page = int(cmd.get("page", 1))
            history = json.dumps({
                "type": "history",
                **news_cache.get_history(page=page)
            })
            await safe_send(websocket, history)
            print(f"{remote} 请求第 {page} 页数据")
        elif cmd.get("action") == "reload":
            print(f"{remote} 请求重载历史数据")
            await news_cache.fetch()

    except json.JSONDecodeError:
        print(f"无效消息来自 {remote}: {message[:50]}...")


async def main():
    """主服务入口"""
    server = await serve(
        client_handler,
        "localhost",
        8765,
        ping_interval=PING_INTERVAL,
        ping_timeout=PING_TIMEOUT,
        max_size=2 ** 20  # 1MB
    )
    print(f"服务已启动: ws://localhost:8765")

    broadcast_task = asyncio.create_task(broadcast_news())
    try:
        await asyncio.Future()  # 永久运行
    except asyncio.CancelledError:
        print("\n正在关闭服务...")
        broadcast_task.cancel()
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n服务已安全关闭")