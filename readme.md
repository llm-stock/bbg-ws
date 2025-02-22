# News Aggregator Service

A real-time news aggregation service with WebSocket server and Telegram bot integration.

## Features

- Real-time news updates via WebSocket
- Telegram bot notifications
- News translation support via Dify API
- Pagination support for news history
- Automatic sitemap parsing

## Setup

1. Install dependencies:

```bash
apt install sqlite3
pip install -r requirements.txt
```

2. Configure environment variables in .env:

```dotenv
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
WEBSOCKET_URI=your_websocket_uri
DIFY_API_KEY=your_dify_api_key  
DIFY_ENDPOINT=your_dify_endpoint
```

3. Create database from schema:

```bash
sqlite3 news.db < schema.sql
```

## WebSocket Server Usage

### Connect to WebSocket Server

```bash

websocat ws://localhost:8765 --ping-interval=20
```

#### Message Formats

##### Receiving News Updates

Server sends news updates in format:

```json
{
  "type": "update",
  "count": 10,
  "articles": [
    "..."
  ]
}
```

#### Requesting History Pages

Send request with page number:

```json
{
  "type": "history",
  "page": 1
}
```

Response format:

```json
{
  "type": "history",
  "articles": [
    "..."
  ],
  "total": 100,
  "page": 1,
  "page_size": 100,
  "total_pages": 10
}
```

### Telegram Bot Usage

1. Create new bot through @BotFather
2. Get bot token and add to .env
3. Add bot to target channel/group
4. Get chat ID and add to .env

# The bot will:

Forward all news updates to the specified channel
Translate titles and descriptions using Dify API
Include media attachments when available

## Configuration Parameters

| Parameter        | Description                  | Default |
|------------------|------------------------------|---------|
| PING_INTERVAL    | WebSocket ping interval      | 20s     |
| PING_TIMEOUT     | WebSocket connection timeout | 20s     |
| CHECK_INTERVAL   | News update check interval   | 60s     |
| SITEMAP_INTERVAL | Sitemap refresh interval     | 300s    |