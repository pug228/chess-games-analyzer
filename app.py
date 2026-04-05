import re
import json
import requests
from flask import Flask, render_template, request, jsonify
import chess
import chess.engine
import chess.pgn
import io
import os

app = Flask(__name__)

LLM_API_URL = "http://10.93.24.194:42005"
LLM_API_KEY = "my-secret-api-key"  # Set your API key here if required

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

    # Step 1: Fetch the game page to extract player info
    page_url = f"https://www.chess.com/game/{game_type}/{game_id}"
    response = requests.get(page_url, headers=headers, proxies=CHESSCOM_PROXIES, timeout=15)
    
    if response.status_code != 200:
        raise Exception(f"Could not access game page (status {response.status_code}).")
    
    html_content = response.text
    
    # Extract player usernames from meta description
    vs_pattern = r'content="(\S+)\s*\(\d+\)\s*vs\s*(\S+)\s*\(\d+\)'
    match = re.search(vs_pattern, html_content)
    
    if not match:
        vs_pattern2 = r'<title>(\S+)\s+vs\s+(\S+)'
        match = re.search(vs_pattern2, html_content)
    
    if not match:
        raise Exception("Could not extract player names from the game page.")
    
    white_username = match.group(1).rstrip('.,;:!?')
    black_username = match.group(2).rstrip('.,;:!?')
    
    # Step 2: Search both players' archives for the full PGN
    for username in [white_username, black_username]:
        pgn = _search_archives(username, game_id, headers, game_type)
        if pgn and '[Event' in pgn and '1.' in pgn:
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
    
    # Step 3: Fallback to callback API (metadata only, no moves)
    callback_url = f"https://www.chess.com/callback/{game_type}/game/{game_id}"
    response = requests.get(callback_url, headers=headers, proxies=CHESSCOM_PROXIES, timeout=15)
    
    if response.status_code == 200:
        try:
            data = response.json()
            if isinstance(data, dict) and "game" in data:
                game_data = data["game"]
                pgn_headers = game_data.get("pgnHeaders", {})
                
                if pgn_headers:
                    pgn = _build_pgn_headers_only(pgn_headers, game_id, game_data)
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
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    
    raise Exception(
        f"Could not find game {game_id}. Players: {white_username} vs {black_username}. "
        f"The game may be very recent - wait a few minutes and try again."
    )


def _build_pgn_headers_only(pgn_headers, game_id, game_data):
    """Build minimal PGN with just headers for fallback."""
    pgn_lines = []
    for key in ["Event", "Site", "Date", "White", "Black", "Result", "ECO",
                "WhiteElo", "BlackElo", "TimeControl", "Termination"]:
        val = pgn_headers.get(key, "?")
        pgn_lines.append(f'[{key} "{val}"]')
    pgn_lines.append('')
    pgn_lines.append(f"; Game ID: {game_id}")
    pgn_lines.append(f"; Result: {game_data.get('resultMessage', '?')}")
    pgn_lines.append(pgn_headers.get("Result", "*"))
    return '\n'.join(pgn_lines)


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


def _analyze_with_llm(pgn, metadata):
    """Send the game to the Qwen LLM for commentary."""
    # Extract moves from PGN for the prompt
    moves_list = []
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
        if game:
            board = game.board()
            for move in game.mainline_moves():
                san = board.san(move)
                moves_list.append(san)
                board.push(move)
    except Exception:
        pass

    # Build the prompt - request structured per-move analysis
    moves_str = '\n'.join([f"Move {i+1}: {m}" for i, m in enumerate(moves_list)])
    
    prompt = f"""You are a chess grandmaster and coach. Analyze the following game move by move.

Game Info:
- White: {metadata.get('white', '?')} (ELO: {metadata.get('white_elo', '?')})
- Black: {metadata.get('black', '?')} (ELO: {metadata.get('black_elo', '?')})
- Result: {metadata.get('result', '?')}
- Opening: {metadata.get('eco', '?')}

Moves:
{moves_str}

Provide TWO things:

1. **Move-by-move evaluation** - For EACH move, classify it into exactly ONE category:
   - **Brilliant** - Exceptional, hard-to-find move
   - **Excellent** - Very strong move
   - **Good** - Solid, reasonable move
   - **Inaccuracy** - Slightly suboptimal but not terrible
   - **Mistake** - Significant error
   - **Blunder** - Major error losing material or advantage
   - **Book** - Standard opening theory move

2. **Overall game analysis** with:
   - Opening assessment
   - Key turning points
   - Critical mistakes with better alternatives
   - Endgame notes
   - Lessons for both players

Format your response EXACTLY like this:

EVALUATIONS:
Move 1: Good - [brief reason]
Move 2: Book - [brief reason]
Move 3: Blunder - [brief reason]
... (one line per move, same number as moves above)

ANALYSIS:
[Your detailed game analysis here]"""

    try:
        headers = {"Content-Type": "application/json"}
        if LLM_API_KEY:
            headers["Authorization"] = f"Bearer {LLM_API_KEY}"
        
        response = requests.post(
            f"{LLM_API_URL}/v1/chat/completions",
            headers=headers,
            json={
                "model": "coder-model",
                "messages": [
                    {"role": "system", "content": "You are a chess grandmaster and expert coach."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 4000
            },
            timeout=600
        )
        
        if response.status_code == 200:
            data = response.json()
            raw = data["choices"][0]["message"]["content"]
            return parse_llm_response(raw)
        else:
            raise Exception(f"LLM API returned status {response.status_code}: {response.text}")
    except requests.exceptions.ConnectionError:
        raise Exception(f"Cannot connect to LLM API at {LLM_API_URL}. Please ensure the server is running.")


def parse_llm_response(raw):
    """
    Parse LLM response into evaluations list and analysis text.
    Expected format:
    EVALUATIONS:
    Move 1: Good - reason
    ...
    ANALYSIS:
    text...
    """
    evaluations = []
    analysis_text = raw
    
    eval_section = False
    analysis_section = False
    
    for line in raw.split('\n'):
        stripped = line.strip()
        
        if stripped.startswith('EVALUATIONS:'):
            eval_section = True
            analysis_section = False
            continue
        elif stripped.startswith('ANALYSIS:'):
            eval_section = False
            analysis_section = True
            continue
        
        if eval_section:
            # Parse "Move N: Category - reason"
            match = re.match(r'Move\s+(\d+):\s*(\w+)\s*-?\s*(.*)', stripped)
            if match:
                move_num = int(match.group(1))
                category = match.group(2).strip()
                reason = match.group(3).strip()
                evaluations.append({
                    "move": move_num - 1,  # 0-indexed
                    "category": normalize_category(category),
                    "reason": reason
                })
    
    # If no EVALUATIONS section found, use entire response as analysis
    if not evaluations:
        analysis_text = raw
    
    return analysis_text, evaluations


def normalize_category(cat):
    """Normalize category names to a fixed set."""
    cat_lower = cat.lower()
    mapping = {
        'brilliant': 'brilliant',
        'excellent': 'excellent',
        'good': 'good',
        'book': 'book',
        'standard': 'book',
        'opening': 'book',
        'inaccuracy': 'inaccuracy',
        'inaccurate': 'inaccuracy',
        'mistake': 'mistake',
        'error': 'mistake',
        'blunder': 'blunder',
        'blundered': 'blunder',
    }
    return mapping.get(cat_lower, 'good')


def evaluate_with_stockfish(pgn, time_limit=0.5):
    """
    Use Stockfish to evaluate each position and classify moves.
    Returns list of evaluations with categories based on centipawn loss.
    """
    import logging
    stockfish_path = _find_stockfish()
    logging.info(f"Stockfish path: {stockfish_path}")
    
    if not stockfish_path:
        logging.warning("Stockfish not found")
        return None
    
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
        if not game:
            logging.warning("Failed to parse PGN")
            return None
        
        evaluations = []
        board = game.board()
        
        with chess.engine.SimpleEngine.popen_uci(stockfish_path) as engine:
            for move in game.mainline_moves():
                side_to_move = board.turn
                
                result = engine.analyse(board, chess.engine.Limit(time=time_limit))
                pv = result.get("pv", [])
                best_move = pv[0] if pv else move
                
                pov_score = result["score"].pov(side_to_move)
                if pov_score.is_mate():
                    best_cp = 10000 if pov_score.mate() > 0 else -10000
                else:
                    best_cp = pov_score.score()
                
                # Convert best_move to SAN BEFORE pushing
                try:
                    best_san = board.san(best_move)
                except Exception:
                    best_san = str(best_move)

                # Analyze what the move does (captures, checks, material loss)
                move_detail = _analyze_move(board, move, best_move)

                board.push(move)

                result2 = engine.analyse(board, chess.engine.Limit(time=time_limit))
                pov_score2 = result2["score"].pov(side_to_move)
                if pov_score2.is_mate():
                    actual_cp = 10000 if pov_score2.mate() > 0 else -10000
                else:
                    actual_cp = pov_score2.score()

                cp_loss = min(500, max(0, best_cp - actual_cp))

                is_book = board.fullmove_number <= 10 and cp_loss < 20
                category, reason = _classify_move(cp_loss, board.turn, move, best_move, is_book, best_cp, actual_cp, move_detail)
                
                evaluations.append({
                    "category": category,
                    "reason": reason,
                    "cp_loss": cp_loss,
                    "score": _format_score(result2["score"]),
                    "best_move": best_san if cp_loss > 50 else None
                })
        
        logging.info(f"Generated {len(evaluations)} evaluations")
        return evaluations
    except Exception as e:
        import traceback
        logging.error(f"Stockfish error: {e}\n{traceback.format_exc()}")
        return None


def _find_stockfish():
    """Find stockfish binary on the system."""
    candidates = [
        "/usr/games/stockfish",      # Debian/Ubuntu package location
        "/usr/bin/stockfish",
        "/usr/local/bin/stockfish",
        "stockfish",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    import shutil
    return shutil.which("stockfish")


def _calc_centipawn_loss(board, before_score, after_score):
    """Calculate centipawn loss for the side that just moved."""
    # Convert scores to perspective of side that just moved
    turn = board.turn  # WHITE or BLACK - this is the side that will move NEXT
    # The side that just moved is the OPPOSITE
    moved_side = not turn
    
    def cp_for_pov(score, pov):
        return score.relative.score() if pov else -score.relative.score()
    
    before_cp = before_score.pov(moved_side).score()
    after_cp = after_score.pov(moved_side).score()
    
    # Clamp mate scores
    if before_cp > 10000: before_cp = 10000
    if before_cp < -10000: before_cp = -10000
    if after_cp > 10000: after_cp = 10000
    if after_cp < -10000: after_cp = -10000
    
    loss = max(0, before_cp - after_cp)
    return loss


def _analyze_move(board, move, best_move):
    """
    Analyze a move to understand what happened.
    Returns a dict with tactical and positional details.
    """
    detail = {
        "is_capture": board.is_capture(move),
        "is_check": False,
        "is_checkmate": False,
        "piece_moved": board.piece_at(move.from_square),
        "captured_piece": board.piece_at(move.to_square),
        "best_was_capture": board.is_capture(best_move),
        "tactic": None,
        "reason": None,
    }

    # Check if move gives check
    board.push(move)
    detail["is_check"] = board.is_check()
    detail["is_checkmate"] = board.is_checkmate()
    board.pop()

    piece_type = detail["piece_moved"].piece_type if detail["piece_moved"] else None
    captured_type = detail["captured_piece"].piece_type if detail["captured_piece"] else None
    detail["piece_name"] = _piece_name(piece_type) if piece_type else "?"
    detail["captured_name"] = _piece_name(captured_type) if captured_type else None

    # Compare material balance: before vs after played move vs after best move
    material_before = _count_material(board)
    
    # Material after played move
    board.push(move)
    material_after = _count_material(board)
    
    # Material after best move (undo first, play best, count)
    board.pop()
    board.push(best_move)
    material_best = _count_material(board)
    board.pop()
    
    material_change = material_after - material_before  # negative = lost material
    material_vs_best = material_after - material_best  # positive = best move kept more material

    detail["material_change"] = material_change
    detail["material_vs_best"] = material_vs_best

    # Determine tactic and reason
    if detail["is_checkmate"]:
        detail["tactic"] = "checkmate"
        detail["reason"] = "Delivers checkmate!"
    elif detail["is_capture"] and not detail["best_was_capture"]:
        if captured_type == chess.QUEEN:
            detail["tactic"] = "won the queen"
            detail["reason"] = "Captures the queen"
        elif piece_type == chess.QUEEN:
            detail["tactic"] = "lost the queen"
            detail["reason"] = "Gave away the queen for a cheap piece"
        elif material_vs_best < -200:
            detail["tactic"] = "bad capture"
            detail["reason"] = f"Taking the {_piece_name(captured_type)} allows a devastating reply"
        else:
            detail["tactic"] = "unnecessary capture"
            detail["reason"] = f"Taking the {_piece_name(captured_type)} weakens the position"
    elif detail["best_was_capture"] and not detail["is_capture"]:
        detail["tactic"] = "missed capture"
        if captured_type:
            detail["reason"] = f"Missed capturing the {_piece_name(captured_type)}"
        elif material_vs_best > 100:
            detail["reason"] = "Missed winning material"
        else:
            detail["reason"] = "Missed a tactical opportunity"
    elif piece_type == chess.QUEEN and material_change < -500:
        detail["tactic"] = "lost the queen"
        detail["reason"] = "Blundered the queen"
    elif material_vs_best < -800:
        lost = _what_was_lost(board, move, best_move, material_change)
        detail["tactic"] = "blunder"
        detail["reason"] = f"Blundered {lost}"
    elif material_vs_best < -300:
        lost = _what_was_lost(board, move, best_move, material_change)
        detail["tactic"] = "mistake"
        detail["reason"] = f"Lost {lost}" if lost else "Made a serious positional error"
    elif material_vs_best < -100:
        detail["tactic"] = "weakens position"
        if material_change < 0:
            lost = _what_was_lost(board, move, best_move, material_change)
            detail["reason"] = f"Lost {lost}" if lost else "Weakened the position"
        else:
            detail["reason"] = "Created a weakness in the position"
    elif captured_type and piece_type and piece_type != chess.KING:
        if captured_type > piece_type:
            detail["tactic"] = "good exchange"
            detail["reason"] = f"Good exchange (took {_piece_name(captured_type)} for {_piece_name(piece_type)})"
        elif piece_type > captured_type:
            detail["tactic"] = "bad exchange"
            detail["reason"] = f"Bad exchange (lost {_piece_name(piece_type)} for {_piece_name(captured_type)})"
        else:
            detail["tactic"] = "even exchange"
            detail["reason"] = "Even material exchange"
    
    # Default reason if still none
    if not detail["reason"]:
        if detail["is_check"]:
            detail["reason"] = "Delivers check"
        elif material_change < 0:
            detail["reason"] = "Lost material"
        # For quiet moves that are bad: the position is strategically lost
        elif material_vs_best < -300:
            detail["reason"] = "Allows a devastating tactical reply"
        elif material_vs_best < -100:
            detail["reason"] = "Creates a serious weakness"
        elif piece_type == chess.KNIGHT:
            detail["reason"] = "Knight move"
        elif piece_type == chess.BISHOP:
            detail["reason"] = "Bishop move"
        elif piece_type == chess.ROOK:
            detail["reason"] = "Rook move"
        else:
            detail["reason"] = f"{detail['piece_name'].capitalize()} move"

    return detail


def _count_material(board):
    """Count material balance (positive = white advantage, negative = black)."""
    piece_values = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, 
                    chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
    total = 0
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece:
            val = piece_values.get(piece.piece_type, 0)
            if piece.color == chess.WHITE:
                total += val
            else:
                total -= val
    return total


def _what_was_lost(board, move, best_move, material_change):
    """Determine what piece was lost due to the bad move."""
    piece_type = board.piece_at(move.from_square)
    
    # If material was lost, identify what
    if material_change < -200:
        if piece_type and piece_type.piece_type == chess.QUEEN:
            return "the queen"
        if piece_type and piece_type.piece_type == chess.ROOK:
            return "a rook"
        if piece_type and piece_type.piece_type == chess.BISHOP:
            return "a bishop"
        if piece_type and piece_type.piece_type == chess.KNIGHT:
            return "a knight"
        # Check what the opponent can now capture
        # Look at squares the moved piece now attacks
        if piece_type:
            return f"significant material ({_piece_name(piece_type.piece_type)})"
    return None


def _piece_name(piece_type):
    """Get piece name in English."""
    names = {
        chess.PAWN: "pawn",
        chess.KNIGHT: "knight",
        chess.BISHOP: "bishop",
        chess.ROOK: "rook",
        chess.QUEEN: "queen",
        chess.KING: "king",
    }
    return names.get(piece_type, "?")


def _classify_move(cp_loss, turn, move, best_move, is_book, best_cp, actual_cp, detail):
    """Classify a move with specific human-friendly explanations."""
    tactic = detail.get("tactic")
    is_capture = detail.get("is_capture", False)
    is_check = detail.get("is_check", False)
    piece = detail.get("piece_name", "?")
    captured = detail.get("captured_name")
    reason = detail.get("reason", "")
    material_change = detail.get("material_change", 0)
    material_vs_best = detail.get("material_vs_best", 0)
    missed_capture = detail.get("best_was_capture", False) and not is_capture
    
    # Mate situations
    if actual_cp < -5000 and best_cp > 5000:
        return "blunder", "Missed a forced checkmate"
    if best_cp >= 9000:
        return "excellent", "Found the winning continuation"
    
    if is_book:
        return "book", "Standard opening theory move"
    
    if move == best_move and cp_loss < 5:
        return "excellent", "Best move"
    
    # For non-tactical blunders (no material lost but position is lost)
    is_positional_blunder = (abs(material_change) < 50 and abs(material_vs_best) < 50 and cp_loss > 200)
    
    if is_positional_blunder:
        if cp_loss > 400:
            return "blunder", "Allows a devastating tactical combination"
        else:
            return "mistake", "Allows a strong tactical reply"
    
    # Use the pre-computed reason from _analyze_move, refine based on severity
    base_reason = reason if reason else f"{piece.capitalize()} move"
    
    if cp_loss <= 15:
        return "excellent", f"Strong move" + (f" ({base_reason.lower()})" if base_reason else "")
    elif cp_loss <= 50:
        return "good", f"Solid move" + (f" ({base_reason.lower()})" if base_reason else "")
    elif cp_loss <= 100:
        if missed_capture and captured:
            return "inaccuracy", f"Missed capturing the {captured}"
        if tactic == "unnecessary capture":
            return "inaccuracy", f"Unnecessary capture — better positional moves exist"
        if tactic == "weakens position":
            return "inaccuracy", f"Weakened the position — better alternatives exist"
        return "inaccuracy", f"Slightly suboptimal — {base_reason.lower()}"
    elif cp_loss <= 250:
        if tactic == "blunder" or (material_vs_best < -300):
            return "blunder", f"Blundered {base_reason.lower()}" if base_reason else "Blunder — severely weakens the position"
        if missed_capture and captured:
            return "mistake", f"Missed capturing the {captured} — free material"
        if tactic == "missed capture":
            return "mistake", f"Missed tactical opportunity — {base_reason.lower()}"
        if is_capture and captured:
            return "mistake", f"Bad capture of the {captured} — allows a strong reply"
        return "mistake", f"Error — {base_reason.lower()}"
    else:  # cp_loss > 250
        if tactic == "lost the queen" or (material_change < -800 and "queen" in str(base_reason).lower()):
            return "blunder", "Blundered the queen — major material loss"
        if tactic == "blunder" or material_vs_best < -800:
            return "blunder", f"Blundered {base_reason.lower()}"
        if missed_capture and captured:
            return "blunder", f"Missed winning the {captured}"
        if is_capture and captured:
            return "blunder", f"Disastrous capture — fatally weakens the position"
        if actual_cp < -3000:
            return "blunder", "Loses on the spot — position becomes completely lost"
        return "blunder", f"Major blunder — {base_reason.lower()}"


def _format_score(score):
    """Format score for display. score is a PovScore from engine.analyse()."""
    rel = score.relative
    if rel.is_mate():
        return f"M{abs(rel.mate())}"
    return f"{rel.score() / 100:+.2f}"


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
        # Use Stockfish for accurate move evaluations
        stockfish_evals = evaluate_with_stockfish(pgn, time_limit=0.1)
        
        # Use LLM for commentary on critical moments only
        analysis_text = ""
        llm_evals = []
        try:
            analysis_text, llm_evals = _analyze_with_llm(pgn, metadata)
        except Exception as e:
            pass  # Continue without LLM analysis if it fails
        
        # Merge Stockfish evaluations with the moves
        evaluations = stockfish_evals or []
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
        "analysis": analysis_text,
        "evaluations": evaluations,
        "metadata": metadata,
        "moves": board_positions
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
