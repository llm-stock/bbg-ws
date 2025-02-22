-- schema.sql
BEGIN TRANSACTION;

-- 新闻主表
CREATE TABLE IF NOT EXISTS news (
    guid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    link TEXT NOT NULL,
    pub_date DATETIME NOT NULL,
    category TEXT,
    media_url TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    stock_tickers TEXT,
    source TEXT

);

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_pub_date ON news(pub_date DESC);
CREATE INDEX IF NOT EXISTS idx_category ON news(category);

COMMIT;