# ♟️ Chess Game Analyzer

A web application that analyzes chess games from chess.com using AI (Qwen LLM). Paste a chess.com game URL, and get a detailed grandmaster-level analysis with an interactive chessboard.

## Features

- 📋 Paste any chess.com live/daily game URL
- ♟️ Interactive chessboard with move navigation
- 🤖 AI-powered analysis via Qwen LLM
- 📊 Game metadata display (players, ELOs, opening, result)
- 🌐 Proxy support for restricted regions

## Quick Start

```bash
docker-compose up --build
```

Open `http://localhost:5000` in your browser.

## Configuration

### Proxy (for restricted regions)

Edit `app.py` to configure your proxy:

```python
CHESSCOM_PROXIES = {
    "http": "http://user:pass@host:port",
    "https": "http://user:pass@host:port"
}
```

### LLM API

By default, the app connects to a Qwen LLM at `http://10.93.24.194:42005`. Change it in `app.py`:

```python
LLM_API_URL = "http://your-llm-host:port"
```

## Tech Stack

- **Backend:** Flask, python-chess, requests
- **Frontend:** chessboard.js, chess.js, jQuery
- **Chess.com Data:** Callback API + Public Archive API
- **Deployment:** Docker

---

## Implementation Plan

### ✅ Version 1 — MVP (Current)

**What's implemented:**

| Component | Details |
|-----------|---------|
| **Chess.com Fetcher** | Callback API (`/callback/live/game/{id}`) with fallback to player archive search (24 months) |
| **Move Decoder** | Decodes chess.com proprietary `moveList` format (64-char encoding) |
| **LLM Integration** | Sends PGN + game metadata to Qwen LLM via OpenAI-compatible API (`/v1/chat/completions`) |
| **Frontend** | Responsive page with chessboard.js, move navigation (⏮◀▶⏭), keyboard shortcuts (←→Home→End) |
| **Game Info Bar** | Displays White vs Black with ELOs and result |
| **Analysis Display** | Formatted AI analysis with markdown-like styling |
| **Proxy Support** | All chess.com requests route through configurable HTTP proxy |
| **Docker** | Dockerfile + docker-compose.yml for one-command deployment |

### 🔜 Version 2 — Minor Improvements

| Feature | Description | Priority |
|---------|-------------|----------|
| **Move-by-move analysis** | Show LLM comments for each move as you navigate the board | High |
| **Evaluation bar** | Visual centipawn evaluation bar alongside the board | Medium |
| **PGN export** | Download the analyzed game as a PGN file with comments | Medium |
| **Game history** | Store and display previously analyzed games (SQLite) | Low |
| **Dark/light theme toggle** | User-selectable UI theme | Low |
| **Error toast notifications** | Better UX for network/API errors | Medium |
| **Loading progress indicator** | Show which step of the pipeline is running | Medium |
| **Multi-game batch analysis** | Paste multiple URLs and analyze them in sequence | Low |
| **Opening book reference** | Show opening name and wiki link from ECO code | Low |
| **Mobile-responsive board** | Better touch controls for mobile devices | Medium |

### 🔮 Future Versions (Beyond v2)

- Stockfish engine integration for engine evaluation comparison
- Player stats lookup via chess.com public API
- Video/PDF report generation
- Multi-language support

---

## License

MIT
