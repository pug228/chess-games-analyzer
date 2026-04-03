import re
import json
import requests
from flask import Flask, render_template, request, jsonify
import chess
import chess.pgn
import io

app = Flask(__name__)

LLM_API_URL = "http://10.93.24.194:42005"

# Proxy for accessing chess.com from Russia
CHESSCOM_PROXIES = {
    "http": "http://cnwgjtmx:5swmqv9vloap@142.111.67.146:5611",
    "https": "http://cnwgjtmx:5swmqv9vloap@142.111.67.146:5611"
}


def parse_chesscom_url(url):
    """Extract game_type and game_id from chess.com URL."""
    pattern = r"chess\.com/game/(live|daily)/([a-zA-Z0-9]+)"
    match = re.search(pattern, url)
    if match:
        return match.group(1), match.group(2)
    return "live", None


def fetch_chesscom_game(game_id, game_type="live"):
    """
    Fetch game data from chess.com API.
    Returns (pgn_string, game_metadata) tuple.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    # Step 1: Try the callback API
    callback_url = f"https://www.chess.com/callback/{game_type}/game/{game_id}"
    response = requests.get(callback_url, headers=headers, proxies=CHESSCOM_PROXIES, timeout=15)
    
    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, dict) and "game" in data:
                game_data = data["game"]
                pgn_headers = game_data.get("pgnHeaders", {})
                
                if pgn_headers:
                    pgn = _build_pgn_from_callback(game_data, game_id)
                    metadata = {
                        "white": pgn_headers.get("White", "?"),
                        "black": pgn_headers.get("Black", "?"),
                        "result": pgn_headers.get("Result", "?"),
                        "white_elo": pgn_headers.get("WhiteElo", "?"),
                        "black_elo": pgn_headers.get("BlackElo", "?"),
                        "eco": pgn_headers.get("ECO", "?"),
                        "termination": game_data.get("resultMessage", "?"),
                        "time_control": pgn_headers.get("TimeControl", "?"),
                        "date": pgn_headers.get("Date", "?"),
                    }
                    return pgn, metadata
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            pass  # Fall through to archive search
    
    # Step 2: Search player archives
    page_url = f"https://www.chess.com/game/{game_type}/{game_id}"
    response = requests.get(page_url, headers=headers, proxies=CHESSCOM_PROXIES, timeout=15)
    
    if response.status_code != 200:
        raise Exception(f"Could not access game page (status {response.status_code}).")
    
    html_content = response.text
    
    vs_pattern = r'content="(\S+)\s*\(\d+\)\s*vs\s*(\S+)\s*\(\d+\)'
    match = re.search(vs_pattern, html_content)
    
    if not match:
        vs_pattern2 = r'<title>(\S+)\s+vs\s+(\S+)'
        match = re.search(vs_pattern2, html_content)
    
    if not match:
        raise Exception("Could not extract player names from the game page.")
    
    white_username = match.group(1).rstrip('.,;:!?')
    black_username = match.group(2).rstrip('.,;:!?')
    
    for username in [white_username, black_username]:
        pgn = _search_archives(username, game_id, headers, game_type)
        if pgn:
            # Extract metadata from PGN
            game_obj = chess.pgn.read_game(io.StringIO(pgn))
            if game_obj:
                metadata = {
                    "white": game_obj.headers.get("White", "?"),
                    "black": game_obj.headers.get("Black", "?"),
                    "result": game_obj.headers.get("Result", "?"),
                    "white_elo": game_obj.headers.get("WhiteElo", "?"),
                    "black_elo": game_obj.headers.get("BlackElo", "?"),
                    "eco": game_obj.headers.get("ECO", "?"),
                    "termination": game_obj.headers.get("Termination", "?"),
                    "time_control": game_obj.headers.get("TimeControl", "?"),
                    "date": game_obj.headers.get("Date", "?"),
                }
            else:
                metadata = {"white": white_username, "black": black_username, "result": "?"}
            return pgn, metadata
    
    raise Exception(
        f"Could not find game {game_id}. Players: {white_username} vs {black_username}. "
        f"The game may be very recent - wait a few minutes and try again."
    )


def _build_pgn_from_callback(game_data, game_id):
    """Build PGN from callback API, trying to decode moves."""
    import chess
    
    game = game_data["game"]
    pgn_headers = game.get("pgnHeaders", {})
    move_list = game.get("moveList", "")
    
    # Try to decode moves
    moves = _decode_movelist(move_list) if move_list else []
    
    board = chess.Board()
    pgn_lines = []
    for key in ["Event", "Site", "Date", "White", "Black", "Result", "ECO", 
                "WhiteElo", "BlackElo", "TimeControl", "Termination"]:
        val = pgn_headers.get(key, "?")
        pgn_lines.append(f'[{key} "{val}"]')
    pgn_lines.append('')
    
    if moves:
        move_text = ""
        for i, move_uci in enumerate(moves):
            try:
                move = chess.Move.from_uci(move_uci)
                if move in board.legal_moves:
                    san = board.san(move)
                    board.push(move)
                    if i % 2 == 0:
                        move_text += f"{i // 2 + 1}. {san} "
                    else:
                        move_text += f"{san} "
            except Exception:
                continue
        pgn_lines.append(move_text.strip())
    else:
        pgn_lines.append(f"; Game ID: {game_id}")
        pgn_lines.append(f"; Result: {game.get('resultMessage', '?')}")
        pgn_lines.append(f"; Total plies: {pgn_headers.get('plyCount', '?')}")
    
    pgn_lines.append(pgn_headers.get("Result", "*"))
    return '\n'.join(pgn_lines)


def _decode_movelist(move_list):
    """
    Decode chess.com's moveList format.
    Uses a compact encoding: each pair of chars = from/to square.
    Character set: 0-9, a-z, A-Z, ?, !, ~ (64 chars for 64 squares)
    """
    import chess
    
    # The 64 characters map to squares 0-63 (a1-h8)
    # Chess.com uses this specific character ordering
    SQ = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ?!"
    square_to_idx = {c: i for i, c in enumerate(SQ)}
    
    board = chess.Board()
    moves = []
    i = 0
    
    while i < len(move_list) - 1:
        c1 = move_list[i]
        c2 = move_list[i + 1]
        
        # Skip special markers
        if c1 in '~!+#':
            i += 1
            continue
        
        if c1 in square_to_idx and c2 in square_to_idx:
            from_sq = square_to_idx[c1]
            to_sq = square_to_idx[c2]
            
            from_square = chess.square(from_sq % 8, from_sq // 8)
            to_square = chess.square(to_sq % 8, to_sq // 8)
            
            # Find matching legal move
            found = False
            for move in list(board.legal_moves):
                if move.from_square == from_square and move.to_square == to_square:
                    moves.append(move.uci())
                    board.push(move)
                    found = True
                    break
            
            # Try with promotions if not found
            if not found and to_sq >= 56:  # 8th rank
                for promo in [chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT]:
                    move = chess.Move(from_square, to_square, promotion=promo)
                    if move in board.legal_moves:
                        moves.append(move.uci())
                        board.push(move)
                        found = True
                        break
            
            i += 2
        else:
            i += 1
    
    return moves


def _search_archives(username, game_id, headers, game_type="live"):
    """Search through a player's monthly archives."""
    from datetime import datetime
    
    now = datetime.now()
    # Search last 24 months to be safe
    for i in range(24):
        year = now.year
        month = now.month - i
        while month <= 0:
            month += 12
            year -= 1
        
        archive_url = f"https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"
        
        try:
            response = requests.get(archive_url, headers=headers, proxies=CHESSCOM_PROXIES, timeout=15)
            if response.status_code == 200:
                data = response.json()
                for g in data.get("games", []):
                    if game_id in g.get("url", ""):
                        return g.get("pgn")
        except Exception:
            continue
    
    return None


def analyze_with_llm(pgn, metadata):
    """Send the game to the Qwen LLM for analysis."""
    # Parse the game to get moves
    game = chess.pgn.read_game(io.StringIO(pgn))
    moves_list = []
    
    if game:
        board = game.board()
        node = game
        while node.variations:
            next_node = node.variation(0)
            move = next_node.move
            if move:
                san = board.san(move)
                moves_list.append(f"{board.fullmove_number}. {san}" if board.turn == chess.WHITE else f"{board.fullmove_number}... {san}")
                board.push(move)
    
    # Build the prompt
    prompt = f"""You are a chess grandmaster and coach. Analyze the following chess game.

Game Info:
- White: {metadata.get('white', '?')} (ELO: {metadata.get('white_elo', '?')})
- Black: {metadata.get('black', '?')} (ELO: {metadata.get('black_elo', '?')})
- Result: {metadata.get('result', '?')}
- Opening: {metadata.get('eco', '?')}
- Time Control: {metadata.get('time_control', '?')}
- Termination: {metadata.get('termination', '?')}

PGN:
{pgn}

Moves:
{', '.join(moves_list)}

Please provide a detailed analysis:
1. **Opening Analysis** - What opening was played? Was it handled correctly?
2. **Key Moments** - Identify 3-5 critical positions that determined the game
3. **Mistakes & Blunders** - Point out bad moves by both sides with better alternatives
4. **Tactical Opportunities** - Were there any tactics missed (forks, pins, skewers, etc.)?
5. **Strategic Assessment** - Positional strengths and weaknesses
6. **Endgame** (if applicable) - How was the endgame handled?
7. **Lessons** - What can both players learn from this game?

Be specific with move numbers and explain your reasoning clearly."""

    try:
        response = requests.post(
            f"{LLM_API_URL}/v1/chat/completions",
            json={
                "model": "qwen",
                "messages": [
                    {"role": "system", "content": "You are a chess grandmaster and expert coach."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 4000
            },
            timeout=120
        )
        
        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"]
        else:
            raise Exception(f"LLM API returned status {response.status_code}: {response.text}")
    except requests.exceptions.ConnectionError:
        raise Exception(f"Cannot connect to LLM API at {LLM_API_URL}. Please ensure the server is running.")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.json
    url = data.get("url", "").strip()
    
    if not url:
        return jsonify({"error": "Please provide a chess.com game URL"}), 400
    
    if "chess.com/game/" not in url:
        return jsonify({"error": "URL must be a chess.com game URL"}), 400
    
    game_type, game_id = parse_chesscom_url(url)
    
    if not game_id:
        return jsonify({"error": "Could not extract game ID from URL. Please check the format."}), 400
    
    try:
        pgn, metadata = fetch_chesscom_game(game_id, game_type)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch game: {str(e)}"}), 400
    
    try:
        analysis = analyze_with_llm(pgn, metadata)
    except Exception as e:
        return jsonify({"error": f"Failed to analyze game: {str(e)}"}), 500
    
    # Parse the game to get board positions for each move
    board_positions = []
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
        if game:
            board = game.board()
            for move in game.mainline_moves():
                san = board.san(move)
                board.push(move)
                board_positions.append({
                    "uci": move.uci(),
                    "san": san,
                    "fen": board.fen()
                })
    except Exception:
        pass
    
    return jsonify({
        "pgn": pgn,
        "analysis": analysis,
        "metadata": metadata,
        "moves": board_positions
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
