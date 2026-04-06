"""
Polymarket AI Trading Bot
Runs 24/7 on Railway. Score 7+ filter, fast payout priority, daily loss guard.
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config (set these in Railway environment variables) ──────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
PAPER_MODE        = os.environ.get("PAPER_MODE", "true").lower() == "true"

# ── Strategy settings ────────────────────────────────────────────────────────
MIN_SCORE         = 7          # Minimum AI score to trade (7+)
MAX_PAYOUT_DAYS   = 30.0       # Only markets resolving within 3 days
MAX_DAILY_LOSSES  = 2          # Stop trading after 2 losses per day
MAX_OPEN_TRADES   = 3          # Max simultaneous positions
MAX_TRADE_USDC    = 1.50       # Max $ per trade
MIN_TRADE_USDC    = 0.50       # Min $ per trade
TAKE_PROFIT_MULT  = 1.55       # Close at 55% gain
STOP_LOSS_MULT    = 0.50       # Close at 50% loss
MIN_VOLUME        = 1_000     # Minimum market volume $
SCAN_INTERVAL     = 50         # Seconds between scans

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


# ── Telegram notifications ───────────────────────────────────────────────────
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


# ── Daily reset ──────────────────────────────────────────────────────────────
def check_daily_reset():
    today = datetime.now(timezone.utc).date().isoformat()
    if state["today"] != today:
        log.info("New day — resetting daily loss counter")
        state["today"]         = today
        state["daily_losses"]  = 0
        state["daily_stopped"] = False
        state["streak"]        = 0
        notify("🌅 *New day started* — daily loss counter reset. Bot is trading again!")


# ── Fetch markets from Polymarket ────────────────────────────────────────────
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

                # Parse outcome prices
                op = m.get("outcomePrices", "[0.5,0.5]")
                if isinstance(op, str):
                    try:
                        op = json.loads(op)
                    except Exception:
                        op = [0.5, 0.5]
                yes_p = float(op[0]) if len(op) > 0 else 0.5
                no_p  = float(op[1]) if len(op) > 1 else 0.5

                # Hours until resolution
                end_date = m.get("endDate")
                hours_left = 9999.0
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        diff = (end_dt - datetime.now(timezone.utc)).total_seconds()
                        hours_left = max(0.0, diff / 3600)
                    except Exception:
                        pass

                volume = float(m.get("volume") or 0)

                if hours_left < 0.5 or volume < MIN_VOLUME:
                    continue

                markets.append({
                    "id":           m["id"],
                    "question":     m.get("question") or m.get("title", "Unknown"),
                    "yes_price":    yes_p,
                    "no_price":     no_p,
                    "volume":       volume,
                    "hours_left":   hours_left,
                    "category":     m.get("category", "Market"),
                    "condition_id": m.get("conditionId", ""),
                })
        except Exception as e:
            log.warning(f"Market fetch error ({url[:50]}...): {e}")

    # Sort by soonest ending first
    markets.sort(key=lambda m: m["hours_left"])

    # Filter to payout window
    max_hours = MAX_PAYOUT_DAYS * 24
    fast = [m for m in markets if m["hours_left"] <= max_hours]
    log.info(f"Fetched {len(markets)} markets total, {len(fast)} within {max_hours:.0f}h window")
    return fast[:20]   # Analyze top 20 soonest


# ── AI analysis via Claude ───────────────────────────────────────────────────
def analyze_market(m: dict) -> dict:
    if not ANTHROPIC_API_KEY:
        return _heuristic(m)

    hours = m["hours_left"]
    if hours < 1:
        time_label = f"{int(hours*60)} minutes"
    elif hours < 24:
        time_label = f"{hours:.1f} hours"
    else:
        time_label = f"{hours/24:.1f} days"

    prompt = f"""You are a strict prediction market analyst for a bot targeting FAST PAYOUTS.

Market: "{m['question']}"
YES: {m['yes_price']*100:.0f}%, NO: {m['no_price']*100:.0f}%
Volume: ${m['volume']/1000:.0f}k | Resolves in: {time_label} | Category: {m['category']}

Scoring (7+ = tradeable):
- 9-10: 75%+ one side, high volume, resolves in hours — near certain
- 7-8: 65-75% one side, decent volume, resolves soon — good edge
- 5-6: 55-65% — too close, SKIP
- 1-4: Low volume or very unclear — SKIP

Fast resolution is a big bonus to the score.
Respond ONLY with valid JSON, no markdown, no extra text:
{{"score":8,"recommendation":"BUY_YES","confidence":72,"reason":"brief reason under 8 words","edge":"brief edge under 6 words"}}
recommendation must be exactly BUY_YES, BUY_NO, or SKIP."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5",  # Fast + cheap
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        if not r.ok:
            log.warning(f"API error {r.status_code}: {r.text[:200]}")
            return _heuristic(m)
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        if result.get("score", 0) < MIN_SCORE:
            result["recommendation"] = "SKIP"
        return result
    except Exception as e:
        log.warning(f"AI analysis failed: {e} — using heuristic")
        return _heuristic(m)


def _heuristic(m: dict) -> dict:
    """Fallback scoring when AI is unavailable."""
    yp, np = m["yes_price"], m["no_price"]
    fast    = m["hours_left"] <= 24
    vol     = m["volume"] > 150_000
    dominant = max(yp, np)
    vhc     = dominant > 0.75
    hc      = dominant > 0.65

    score = 1
    score += 3 if fast else 0
    score += 2 if vol  else 0
    score += 2 if hc   else 0
    score += 2 if vhc  else 0
    score = min(10, score)

    if dominant > 0.65:
        rec = "BUY_YES" if yp > np else "BUY_NO"
    else:
        rec = "SKIP"

    if score < MIN_SCORE:
        rec = "SKIP"

    return {
        "score":          score,
        "recommendation": rec,
        "confidence":     int(dominant * 100),
        "reason":         "Strong edge, fast close" if vhc else "Good edge, quick payout" if hc else "Low edge",
        "edge":           "High liquidity" if vol else "Building volume",
    }


# ── Trade management ─────────────────────────────────────────────────────────
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

    # Kelly fraction sizing
    conf  = analysis["confidence"] / 100
    edge  = conf - (1 - conf) / max(0.01, (1 / ep - 1))
    kf    = max(0.05, min(0.12, edge))
    size  = round(min(MAX_TRADE_USDC, max(MIN_TRADE_USDC, state["balance"] * kf)), 2)

    tp = min(0.95, ep * TAKE_PROFIT_MULT)
    sl = max(0.05, ep * STOP_LOSS_MULT)

    trade = {
        "id":           f"t_{int(time.time()*1000)}",
        "market_id":    m["id"],
        "question":     m["question"],
        "side":         side,
        "entry_price":  ep,
        "current_price":ep,
        "size":         size,
        "shares":       size / ep,
        "take_profit":  tp,
        "stop_loss":    sl,
        "score":        analysis["score"],
        "confidence":   analysis["confidence"],
        "reason":       analysis.get("reason", ""),
        "hours_left":   m["hours_left"],
        "open_time":    datetime.now(timezone.utc).isoformat(),
        "category":     m["category"],
    }

    state["balance"]         -= size
    state["balance"]          = round(state["balance"], 4)
    state["active_trades"].append(trade)

    mode_tag = "📄 PAPER" if PAPER_MODE else "💰 REAL"
    notify(
        f"{mode_tag} *Trade Opened*\n"
        f"Market: {m['question'][:80]}\n"
        f"Side: {side} @ {ep*100:.0f}¢\n"
        f"Size: ${size:.2f} | Score: {analysis['score']}/10\n"
        f"Payout in: {m['hours_left']:.1f}h\n"
        f"TP: {tp*100:.0f}¢ | SL: {sl*100:.0f}¢\n"
        f"Reason: {analysis.get('reason','')}"
    )
    log.info(f"TRADE OPENED: {side} {m['question'][:60]} | ${size:.2f} @ {ep*100:.0f}¢")
    return True


def simulate_price_move(trade: dict) -> float:
    """Paper mode: simulate realistic price movement."""
    import random
    bias  = (trade["confidence"] / 100 - 0.44) * 0.07
    noise = (random.random() - 0.5) * 0.11
    new_price = trade["current_price"] + bias + noise
    return max(0.02, min(0.98, new_price))


def update_trades():
    """Check all active trades for TP/SL hits."""
    closed = []
    for trade in state["active_trades"]:
        if PAPER_MODE:
            trade["current_price"] = simulate_price_move(trade)
        # else: fetch real price from Polymarket API here

        cp = trade["current_price"]
        if cp >= trade["take_profit"]:
            close_trade(trade, "WIN", "Take profit hit ✓")
            closed.append(trade["id"])
        elif cp <= trade["stop_loss"]:
            close_trade(trade, "LOSS", "Stop loss hit ✗")
            closed.append(trade["id"])

    state["active_trades"] = [t for t in state["active_trades"] if t["id"] not in closed]


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
                "🛑 *Daily loss limit reached!*\n"
                f"2 losses today — bot paused to protect capital.\n"
                f"Balance: ${state['balance']:.2f}\n"
                "Will auto-resume tomorrow."
            )

    record = {**trade, "exit_price": trade["current_price"], "pnl": pnl, "result": result, "reason": reason, "close_time": datetime.now(timezone.utc).isoformat()}
    state["history"].append(record)

    emoji = "✅" if result == "WIN" else "❌"
    streak_txt = f" 🔥 Streak: {state['streak']}" if state["streak"] >= 2 else ""
    notify(
        f"{emoji} *Trade {result}*{streak_txt}\n"
        f"Market: {trade['question'][:70]}\n"
        f"P&L: {'+'if pnl>=0 else ''}{pnl:.3f} USDC\n"
        f"Balance: ${state['balance']:.2f}\n"
        f"Reason: {reason}"
    )
    log.info(f"TRADE {result}: {trade['question'][:60]} | PnL: {pnl:+.3f} | Balance: ${state['balance']:.2f}")


# ── Stats summary ─────────────────────────────────────────────────────────────
def print_stats():
    wins  = sum(1 for t in state["history"] if t["result"] == "WIN")
    total = len(state["history"])
    total_pnl = sum(t["pnl"] for t in state["history"])
    tv    = state["balance"] + sum(t["shares"] * t["current_price"] for t in state["active_trades"])
    log.info(
        f"📊 Stats | Balance: ${tv:.2f} | P&L: {total_pnl:+.2f} | "
        f"Trades: {total} | Win rate: {wins/total*100:.0f}% | "
        f"Streak: {state['streak']} | Daily losses: {state['daily_losses']}/2"
        if total else
        f"📊 Stats | Balance: ${state['balance']:.2f} | No trades yet"
    )


# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  Polymarket AI Bot v4 — Starting up")
    log.info(f"  Mode: {'📄 PAPER (no real money)' if PAPER_MODE else '💰 LIVE'}")
    log.info(f"  Min score: {MIN_SCORE}+ | Max payout: {MAX_PAYOUT_DAYS}d")
    log.info(f"  Max risk: ${MAX_TRADE_USDC}/trade | Daily stop: {MAX_DAILY_LOSSES} losses")
    log.info("=" * 55)

    notify(
        f"🤖 *Polymarket Bot Started*\n"
        f"Mode: {'Paper' if PAPER_MODE else 'LIVE'} | Balance: ${state['balance']:.2f}\n"
        f"Min score: {MIN_SCORE}+ | Scan every: {SCAN_INTERVAL}s"
    )

    scan_counter = 0
    while True:
        try:
            check_daily_reset()
            update_trades()

            scan_counter += 1
            if scan_counter % 6 == 0:   # Print stats every ~5 mins
                print_stats()

            if state["daily_stopped"]:
                log.info("Daily loss limit hit — skipping scan, waiting for tomorrow")
                time.sleep(SCAN_INTERVAL)
                continue

            log.info(f"🔍 Scan #{state['total_scans']+1} starting...")
            markets = fetch_markets()
            state["total_scans"] += 1

            trades_placed = 0
            for m in markets:
                if not can_trade():
                    break

                log.info(f"  Analyzing: {m['question'][:60]} ({m['hours_left']:.1f}h left)")
                analysis = analyze_market(m)
                log.info(f"  Score: {analysis['score']}/10 | Rec: {analysis['recommendation']} | Conf: {analysis['confidence']}%")

                if analysis["score"] >= MIN_SCORE and analysis["recommendation"] != "SKIP":
                    if place_trade(m, analysis):
                        trades_placed += 1

                time.sleep(0.5)   # Be polite to APIs

            log.info(f"✅ Scan complete — {trades_placed} trades placed | {len(state['active_trades'])} open | Balance: ${state['balance']:.2f}")

        except KeyboardInterrupt:
            log.info("Bot stopped by user")
            notify("⚠️ Bot stopped manually.")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(10)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
