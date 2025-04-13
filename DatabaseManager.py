# DatabaseManager.py
import sqlite3
from threading import Lock


class DatabaseManager:
    def __init__(self, db_path='news.db'):
        self.db_path = db_path
        self.lock = Lock()

        # 初始化数据库
        with self._get_conn() as conn:
            with open('schema.sql') as f:
                conn.executescript(f.read())

    def _get_conn(self):
        return sqlite3.connect(
            self.db_path,
            check_same_thread=False,  # 允许多线程访问
            isolation_level=None  # 自动提交模式
        )
    def get_total_count(self):
        """获取总记录数"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM news")
            return cursor.fetchone()[0]

    def save_news(self, items):
        """批量保存新闻"""
        with self.lock, self._get_conn() as conn:
            try:
                conn.executemany('''
                    INSERT OR IGNORE INTO news 
                    (guid, title, link, pub_date, stock_tickers, media_url, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', [
                    (
                        item['guid'],
                        item['title'],
                        item['link'],
                        item['published'],
                        item.get('stock_tickers', ''),
                        item.get('media_url', ''),
                        item.get('source', 'rss')
                    ) for item in items
                ])
            except sqlite3.Error as e:
                print(f"Database error: {e}")
    def get_news_by_guid(self, guid: str):
        """根据 GUID 获取新闻"""
        with self.lock, self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute('''
                SELECT guid, title, description, link, 
                       pub_date as published, category, media_url 
                FROM news 
                WHERE guid = ?
            ''', (guid,))
            row = cursor.fetchone()
            return dict(row) if row else None


    def update_or_insert_news(self, item):
        """更新或插入新闻"""
        with self.lock, self._get_conn() as conn:
            try:
                conn.execute('''
                    INSERT INTO news 
                    (guid, title, link, pub_date, stock_tickers, media_url, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guid) DO UPDATE SET
                        title = excluded.title,
                        link = excluded.link,
                        pub_date = excluded.pub_date,
                        stock_tickers = excluded.stock_tickers,
                        media_url = excluded.media_url,
                        source = excluded.source
                ''', (
                    item['guid'],
                    item['title'],
                    item['link'],
                    item['published'],
                    item.get('stock_tickers', ''),
                    item.get('media_url', ''),
                    item.get('source', 'rss')
                ))
            except sqlite3.Error as e:
                print(f"Database error: {e}")
    def is_news_exists(self, guid):
        """检查新闻是否存在"""
        with self.lock, self._get_conn() as conn:
            cursor = conn.execute('SELECT COUNT(*) FROM news WHERE guid = ?', (guid,))
            return cursor.fetchone()[0] > 0

    def get_history_page(self, offset, limit):
        """分页获取历史数据"""
        with self.lock, self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute('''
                SELECT guid, title, description, link, 
                       pub_date as published, category, media_url 
                FROM news 
                ORDER BY pub_date DESC 
                LIMIT ? OFFSET ?
            ''', (limit, offset))
            return [dict(row) for row in cursor.fetchall()]

    def get_history(self, limit=1000):
        """获取历史新闻"""
        return self.get_history_page(0, limit)