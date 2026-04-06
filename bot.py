"""
Polymarket AI Trading Bot v5
Fixed: uses heuristic scoring directly, no AI over-restriction
Trades any market with clear edge (one side 60%+)
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
PAPER_MODE        = os.environ.get("PAPER_MODE", "true").lower() == "true"

# ── Strategy ─────────────────────────────────────────────────────────────────
MIN_SCORE        = int(os.environ.get("MIN_SCORE", "5"))       # Trade score 5+
MAX_PAYOUT_DAYS  = float(os.environ.get("MAX_PAYOUT_DAYS", "30"))
MAX_DAILY_LOSSES = 2
MAX_OPEN_TRADES  = 3
MAX_TRADE_USDC   = 1.50
MIN_TRADE_USDC   = 0.50
TAKE_PROFIT_MULT = 1.55
STOP_LOSS_MULT   = 0.50
MIN_VOLUME       = 5_000
SCAN_INTERVAL    = 50

# ── State ────────────────────────────────────────────────────────────────────
state = {
    "balance":       25.0,
    "active_trades": [],
    "history":       [],
    "daily_losses":  0,
    "daily_stopped": False,
    "streak":        0,
    "today":         datetime.now(timezone.utc).date().isoformat(),
    "total_scans":   0,
}


def notify(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[NOTIFY] {msg}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")


def check_daily_reset():
    today = datetime.now(timezone.utc).date().isoformat()
    if state["today"] != today:
        log.info("New day — resetting daily loss counter")
        state["today"]         = today
        state["daily_losses"]  = 0
        state["daily_stopped"] = False
        state["streak"]        = 0
        notify("🌅 *New day* — bot trading again!")


def fetch_markets() -> list[dict]:
    markets = []
    urls = [
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50&order=endDate&ascending=true",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50&order=volume&ascending=false",
    ]
    seen = set()
    for url in urls:
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            for m in r.json():
                if m.get("id") in seen:
                    continue
                seen.add(m["id"])

                op = m.get("outcomePrices", "[0.5,0.5]")
                if isinstance(op, str):
                    try:
                        op = json.loads(op)
                    except Exception:
                        op = [0.5, 0.5]
                yes_p = float(op[0]) if len(op) > 0 else 0.5
                no_p  = float(op[1]) if len(op) > 1 else 0.5

                end_date   = m.get("endDate")
                hours_left = 9999.0
                if end_date:
                    try:
                        end_dt     = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        diff       = (end_dt - datetime.now(timezone.utc)).total_seconds()
                        hours_left = max(0.0, diff / 3600)
                    except Exception:
                        pass

                volume = float(m.get("volume") or 0)
                if hours_left < 0.5 or volume < MIN_VOLUME:
                    continue

                markets.append({
                    "id":         m["id"],
                    "question":   m.get("question") or m.get("title", "Unknown"),
                    "yes_price":  yes_p,
                    "no_price":   no_p,
                    "volume":     volume,
                    "hours_left": hours_left,
                    "category":   m.get("category", "Market"),
                })
        except Exception as e:
            log.warning(f"Fetch error: {e}")

    markets.sort(key=lambda m: m["hours_left"])
    max_hours = MAX_PAYOUT_DAYS * 24
    filtered  = [m for m in markets if m["hours_left"] <= max_hours]
    log.info(f"Fetched {len(markets)} markets, {len(filtered)} in window")
    return filtered[:20]


def score_market(m: dict) -> dict:
    """
    Pure heuristic scoring — no AI needed.
    Scores based on: how one-sided the odds are + volume + time to resolve.
    """
    yp       = m["yes_price"]
    np       = m["no_price"]
    dominant = max(yp, np)
    side     = "BUY_YES" if yp > np else "BUY_NO"

    # Base score from odds dominance
    if dominant >= 0.85:
        score = 9
    elif dominant >= 0.78:
        score = 8
    elif dominant >= 0.70:
        score = 7
    elif dominant >= 0.65:
        score = 6
    elif dominant >= 0.60:
        score = 5
    elif dominant >= 0.55:
        score = 4
    else:
        score = 2   # Too close to call

    # Bonus for fast resolution
    if m["hours_left"] <= 6:
        score = min(10, score + 2)
    elif m["hours_left"] <= 24:
        score = min(10, score + 1)

    # Bonus for high volume (more reliable odds)
    if m["volume"] >= 500_000:
        score = min(10, score + 1)
    elif m["volume"] >= 200_000:
        score = min(10, score + 0)

    # Only trade if one side has clear edge
    rec = side if dominant >= 0.60 else "SKIP"
    if score < MIN_SCORE:
        rec = "SKIP"

    return {
        "score":          score,
        "recommendation": rec,
        "confidence":     int(dominant * 100),
        "reason":         f"{int(dominant*100)}% edge, {m['hours_left']:.0f}h left",
        "edge":           f"${m['volume']/1000:.0f}k volume",
    }


def can_trade() -> bool:
    return (
        not state["daily_stopped"]
        and len(state["active_trades"]) < MAX_OPEN_TRADES
        and state["balance"] >= MIN_TRADE_USDC
    )


def place_trade(m: dict, analysis: dict) -> bool:
    if not can_trade():
        return False
    if analysis["recommendation"] == "SKIP":
        return False
    if any(t["market_id"] == m["id"] for t in state["active_trades"]):
        return False

    side = "YES" if analysis["recommendation"] == "BUY_YES" else "NO"
    ep   = m["yes_price"] if side == "YES" else m["no_price"]
    ep   = max(0.01, min(0.99, ep))

    conf  = analysis["confidence"] / 100
    edge  = max(0.01, conf - (1 - conf) / max(0.01, (1 / ep - 1)))
    kf    = max(0.05, min(0.12, edge))
    size  = round(min(MAX_TRADE_USDC, max(MIN_TRADE_USDC, state["balance"] * kf)), 2)

    tp = min(0.95, ep * TAKE_PROFIT_MULT)
    sl = max(0.05, ep * STOP_LOSS_MULT)

    trade = {
        "id":            f"t_{int(time.time()*1000)}",
        "market_id":     m["id"],
        "question":      m["question"],
        "side":          side,
        "entry_price":   ep,
        "current_price": ep,
        "size":          size,
        "shares":        size / ep,
        "take_profit":   tp,
        "stop_loss":     sl,
        "score":         analysis["score"],
        "confidence":    analysis["confidence"],
        "reason":        analysis.get("reason", ""),
        "hours_left":    m["hours_left"],
        "open_time":     datetime.now(timezone.utc).isoformat(),
        "category":      m["category"],
    }

    state["balance"]          -= size
    state["balance"]           = round(state["balance"], 4)
    state["active_trades"].append(trade)

    mode = "📄 PAPER" if PAPER_MODE else "💰 REAL"
    notify(
        f"{mode} *Trade Opened!*\n"
        f"📊 {m['question'][:80]}\n"
        f"Side: *{side}* @ {ep*100:.0f}¢\n"
        f"Size: ${size:.2f} | Score: {analysis['score']}/10\n"
        f"Confidence: {analysis['confidence']}%\n"
        f"Payout in: {m['hours_left']:.1f}h\n"
        f"TP: {tp*100:.0f}¢ | SL: {sl*100:.0f}¢"
    )
    log.info(
        f"✅ TRADE OPENED: {side} | {m['question'][:55]} "
        f"| ${size:.2f} @ {ep*100:.0f}¢ | Score {analysis['score']}/10"
    )
    return True


def simulate_price(trade: dict) -> float:
    import random
    bias  = (trade["confidence"] / 100 - 0.44) * 0.07
    noise = (random.random() - 0.5) * 0.11
    return max(0.02, min(0.98, trade["current_price"] + bias + noise))


def update_trades():
    closed = []
    for trade in state["active_trades"]:
        if PAPER_MODE:
            trade["current_price"] = simulate_price(trade)

        cp = trade["current_price"]
        if cp >= trade["take_profit"]:
            close_trade(trade, "WIN", "Take profit ✓")
            closed.append(trade["id"])
        elif cp <= trade["stop_loss"]:
            close_trade(trade, "LOSS", "Stop loss ✗")
            closed.append(trade["id"])

    state["active_trades"] = [
        t for t in state["active_trades"] if t["id"] not in closed
    ]


def close_trade(trade: dict, result: str, reason: str):
    val = trade["shares"] * trade["current_price"]
    pnl = round(val - trade["size"], 4)
    state["balance"] = round(state["balance"] + val, 4)

    if result == "WIN":
        state["streak"] += 1
    else:
        state["streak"]       = 0
        state["daily_losses"] += 1
        if state["daily_losses"] >= MAX_DAILY_LOSSES:
            state["daily_stopped"] = True
            notify(
                "🛑 *Daily loss limit hit!*\n"
                f"2 losses today — paused to protect capital.\n"
                f"Balance: ${state['balance']:.2f}\n"
                "Resumes tomorrow automatically."
            )

    state["history"].append({
        **trade,
        "exit_price": trade["current_price"],
        "pnl":        pnl,
        "result":     result,
        "reason":     reason,
        "close_time": datetime.now(timezone.utc).isoformat(),
    })

    emoji      = "✅" if result == "WIN" else "❌"
    streak_txt = f" 🔥 Streak: {state['streak']}" if state["streak"] >= 2 else ""
    notify(
        f"{emoji} *Trade {result}*{streak_txt}\n"
        f"{trade['question'][:70]}\n"
        f"P&L: {'+'if pnl>=0 else ''}{pnl:.3f} USDC\n"
        f"Balance: ${state['balance']:.2f}\n"
        f"{reason}"
    )
    log.info(
        f"{'✅' if result=='WIN' else '❌'} TRADE {result}: "
        f"{trade['question'][:50]} | PnL: {pnl:+.3f} | "
        f"Balance: ${state['balance']:.2f}"
    )


def print_stats():
    wins  = sum(1 for t in state["history"] if t["result"] == "WIN")
    total = len(state["history"])
    pnl   = sum(t["pnl"] for t in state["history"])
    tv    = state["balance"] + sum(
        t["shares"] * t["current_price"] for t in state["active_trades"]
    )
    if total:
        log.info(
            f"📊 Balance: ${tv:.2f} | P&L: {pnl:+.2f} | "
            f"Trades: {total} | WR: {wins/total*100:.0f}% | "
            f"Streak: {state['streak']} | Daily losses: {state['daily_losses']}/2"
        )
    else:
        log.info(f"📊 Balance: ${state['balance']:.2f} | No trades yet | Scans: {state['total_scans']}")


def main():
    log.info("=" * 55)
    log.info("  Polymarket AI Bot v5 — Starting up")
    log.info(f"  Mode: {'📄 PAPER' if PAPER_MODE else '💰 LIVE'}")
    log.info(f"  Min score: {MIN_SCORE}+ | Window: {MAX_PAYOUT_DAYS}d")
    log.info(f"  Max risk: ${MAX_TRADE_USDC}/trade | Stop: {MAX_DAILY_LOSSES} losses/day")
    log.info("=" * 55)

    notify(
        f"🤖 *Polymarket Bot v5 Started!*\n"
        f"Mode: {'Paper' if PAPER_MODE else 'LIVE'}\n"
        f"Balance: ${state['balance']:.2f}\n"
        f"Min score: {MIN_SCORE}+ | Scanning every {SCAN_INTERVAL}s"
    )

    tick = 0
    while True:
        try:
            check_daily_reset()
            update_trades()
            tick += 1

            if tick % 6 == 0:
                print_stats()

            if state["daily_stopped"]:
                log.info("Daily limit hit — waiting for tomorrow...")
                time.sleep(SCAN_INTERVAL)
                continue

            log.info(f"🔍 Scan #{state['total_scans']+1}...")
            markets = fetch_markets()
            state["total_scans"] += 1

            placed = 0
            for m in markets:
                if not can_trade():
                    break

                analysis = score_market(m)
                log.info(
                    f"  {m['question'][:55]} | "
                    f"Score: {analysis['score']}/10 | "
                    f"Rec: {analysis['recommendation']} | "
                    f"Conf: {analysis['confidence']}% | "
                    f"{m['hours_left']:.0f}h left"
                )

                if analysis["recommendation"] != "SKIP":
                    if place_trade(m, analysis):
                        placed += 1

                time.sleep(0.3)

            log.info(
                f"✅ Scan done — {placed} trades placed | "
                f"{len(state['active_trades'])} open | "
                f"Balance: ${state['balance']:.2f}"
            )

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            notify("⚠️ Bot stopped manually.")
            break
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
            time.sleep(10)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
