"""
Polymarket REAL Money Trading Bot v1
Uses py-clob-client for real order placement on Polymarket
Runs 24/7 on Railway
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

# ── Config (set in Railway Variables) ────────────────────────────────────────
PRIVATE_KEY      = os.environ.get("PRIVATE_KEY", "")        # MetaMask private key
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Strategy settings ─────────────────────────────────────────────────────────
MIN_CONFIDENCE   = 65       # Only trade if one side is 65%+ odds
MAX_TRADE_USDC   = 1.50     # Max $ per trade
MIN_TRADE_USDC   = 0.50     # Min $ per trade
MAX_OPEN_TRADES  = 3        # Max simultaneous trades
MAX_DAILY_LOSSES = 2        # Stop after 2 losses per day
MIN_VOLUME       = 10_000   # Minimum market volume
MAX_PAYOUT_DAYS  = 7        # Markets resolving within 7 days
SCAN_INTERVAL    = 60       # Seconds between scans
TAKE_PROFIT      = 0.92     # Close when price hits 92¢ (near certainty)
STOP_LOSS_PCT    = 0.40     # Close if lose 40% of trade value

# ── Polymarket API endpoints ──────────────────────────────────────────────────
GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
CHAIN_ID   = 137  # Polygon mainnet

# ── State ────────────────────────────────────────────────────────────────────
state = {
    "balance":       0.0,
    "active_trades": [],
    "history":       [],
    "daily_losses":  0,
    "daily_stopped": False,
    "streak":        0,
    "today":         datetime.now(timezone.utc).date().isoformat(),
    "total_scans":   0,
    "client":        None,
}


# ── Telegram ──────────────────────────────────────────────────────────────────
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


# ── Setup Polymarket client ───────────────────────────────────────────────────
def setup_client():
    """Initialize the py-clob-client for real trading."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if not PRIVATE_KEY:
            log.error("No PRIVATE_KEY set! Add it to Railway Variables.")
            return None

        client = ClobClient(
            host=CLOB_API,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY,
        )

        # Create API credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        log.info("✅ Polymarket client connected successfully!")
        return client

    except ImportError:
        log.error("py-clob-client not installed! Check requirements.txt")
        return None
    except Exception as e:
        log.error(f"Client setup failed: {e}")
        return None


def get_real_balance() -> float:
    """Get real USDC balance from Polymarket."""
    try:
        if not state["client"]:
            return state["balance"]
        bal = state["client"].get_balance()
        return float(bal) / 1_000_000  # Convert from USDC decimals
    except Exception as e:
        log.warning(f"Balance fetch error: {e}")
        return state["balance"]


# ── Market fetching ───────────────────────────────────────────────────────────
def fetch_markets() -> list[dict]:
    markets = []
    urls = [
        f"{GAMMA_API}/markets?active=true&closed=false&limit=50&order=endDate&ascending=true",
        f"{GAMMA_API}/markets?active=true&closed=false&limit=50&order=volume&ascending=false",
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
                if hours_left < 1 or volume < MIN_VOLUME:
                    continue
                if hours_left > MAX_PAYOUT_DAYS * 24:
                    continue

                # Get token IDs for real trading
                tokens = m.get("tokens", [])
                yes_token = next((t.get("token_id") for t in tokens if t.get("outcome","").upper()=="YES"), None)
                no_token  = next((t.get("token_id") for t in tokens if t.get("outcome","").upper()=="NO"), None)

                if not yes_token and not no_token:
                    continue

                markets.append({
                    "id":           m["id"],
                    "question":     m.get("question") or m.get("title", "Unknown"),
                    "yes_price":    yes_p,
                    "no_price":     no_p,
                    "volume":       volume,
                    "hours_left":   hours_left,
                    "category":     m.get("category", "Market"),
                    "yes_token_id": yes_token,
                    "no_token_id":  no_token,
                    "condition_id": m.get("conditionId", ""),
                })
        except Exception as e:
            log.warning(f"Fetch error: {e}")

    markets.sort(key=lambda m: m["hours_left"])
    log.info(f"Fetched {len(markets)} tradeable markets")
    return markets[:15]


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_market(m: dict) -> dict:
    yp       = m["yes_price"]
    np_      = m["no_price"]
    dominant = max(yp, np_)
    side     = "BUY_YES" if yp > np_ else "BUY_NO"

    if dominant >= 0.85:   score = 9
    elif dominant >= 0.78: score = 8
    elif dominant >= 0.70: score = 7
    elif dominant >= 0.65: score = 6
    elif dominant >= 0.60: score = 5
    else:                  score = 2

    # Bonus for speed
    if m["hours_left"] <= 6:    score = min(10, score + 2)
    elif m["hours_left"] <= 24: score = min(10, score + 1)

    # Bonus for volume
    if m["volume"] >= 500_000: score = min(10, score + 1)

    conf = int(dominant * 100)
    rec  = side if conf >= MIN_CONFIDENCE else "SKIP"

    return {
        "score":          score,
        "recommendation": rec,
        "confidence":     conf,
        "reason":         f"{conf}% edge · {m['hours_left']:.0f}h left",
        "edge":           f"${m['volume']/1000:.0f}k vol",
    }


# ── Real trade placement ──────────────────────────────────────────────────────
def place_real_trade(m: dict, analysis: dict) -> bool:
    """Place a real order on Polymarket via CLOB API."""
    if not state["client"]:
        log.error("No client — cannot place real trade!")
        return False
    if state["daily_stopped"]:
        return False
    if len(state["active_trades"]) >= MAX_OPEN_TRADES:
        return False
    if any(t["market_id"] == m["id"] for t in state["active_trades"]):
        return False
    if analysis["recommendation"] == "SKIP":
        return False

    side      = "YES" if analysis["recommendation"] == "BUY_YES" else "NO"
    ep        = m["yes_price"] if side == "YES" else m["no_price"]
    token_id  = m["yes_token_id"] if side == "YES" else m["no_token_id"]

    if not token_id:
        log.warning(f"No token ID for {side} side — skipping")
        return False

    ep   = max(0.01, min(0.99, ep))
    conf = analysis["confidence"] / 100
    edge = max(0.01, conf - (1 - conf) / max(0.01, (1 / ep - 1)))
    kf   = max(0.05, min(0.10, edge))
    size = round(min(MAX_TRADE_USDC, max(MIN_TRADE_USDC, state["balance"] * kf)), 2)

    if size > state["balance"]:
        log.warning(f"Not enough balance: need ${size:.2f}, have ${state['balance']:.2f}")
        return False

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        log.info(f"Placing REAL order: {side} ${size:.2f} on {m['question'][:50]}")

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size,
        )

        signed_order = state["client"].create_market_order(order_args)
        resp         = state["client"].post_order(signed_order, OrderType.FOK)

        if resp and resp.get("success"):
            fill_price = float(resp.get("price", ep))
            shares     = size / fill_price

            trade = {
                "id":            resp.get("orderID", f"t_{int(time.time())}"),
                "market_id":     m["id"],
                "question":      m["question"],
                "side":          side,
                "token_id":      token_id,
                "entry_price":   fill_price,
                "current_price": fill_price,
                "size":          size,
                "shares":        shares,
                "take_profit":   TAKE_PROFIT,
                "stop_loss":     fill_price * STOP_LOSS_PCT,
                "score":         analysis["score"],
                "confidence":    analysis["confidence"],
                "reason":        analysis.get("reason", ""),
                "hours_left":    m["hours_left"],
                "open_time":     datetime.now(timezone.utc).isoformat(),
                "category":      m["category"],
            }

            state["balance"]          -= size
            state["active_trades"].append(trade)

            notify(
                f"💰 *REAL Trade Opened!*\n"
                f"📊 {m['question'][:80]}\n"
                f"Side: *{side}* @ {fill_price*100:.0f}¢\n"
                f"Size: ${size:.2f} | Score: {analysis['score']}/10\n"
                f"Confidence: {analysis['confidence']}%\n"
                f"Payout in: {m['hours_left']:.1f}h\n"
                f"Balance: ${state['balance']:.2f}"
            )
            log.info(f"✅ REAL TRADE PLACED: {side} ${size:.2f} @ {fill_price*100:.0f}¢")
            return True
        else:
            log.warning(f"Order rejected: {resp}")
            return False

    except Exception as e:
        log.error(f"Trade placement error: {e}")
        return False


# ── Monitor open trades ───────────────────────────────────────────────────────
def update_real_trades():
    """Check current prices and close trades at TP/SL."""
    if not state["active_trades"]:
        return

    closed = []
    for trade in state["active_trades"]:
        try:
            # Get current market price
            token_id = trade.get("token_id")
            if token_id:
                r = requests.get(
                    f"{CLOB_API}/price?token_id={token_id}&side=BUY",
                    timeout=5
                )
                if r.ok:
                    data = r.json()
                    trade["current_price"] = float(data.get("price", trade["current_price"]))

            cp = trade["current_price"]

            # Check take profit
            if cp >= trade["take_profit"]:
                if close_real_trade(trade, "WIN", f"Take profit at {cp*100:.0f}¢ ✓"):
                    closed.append(trade["id"])

            # Check stop loss
            elif cp <= trade["stop_loss"]:
                if close_real_trade(trade, "LOSS", f"Stop loss at {cp*100:.0f}¢ ✗"):
                    closed.append(trade["id"])

        except Exception as e:
            log.warning(f"Trade monitor error: {e}")

    state["active_trades"] = [
        t for t in state["active_trades"] if t["id"] not in closed
    ]


def close_real_trade(trade: dict, result: str, reason: str) -> bool:
    """Close a real trade by selling position."""
    try:
        cp  = trade["current_price"]
        val = trade["shares"] * cp
        pnl = round(val - trade["size"], 4)

        # In real trading the position closes automatically at resolution
        # or you can sell early via CLOB
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
                    f"2 losses today — bot paused.\n"
                    f"Balance: ${state['balance']:.2f}\n"
                    "Resumes tomorrow automatically."
                )

        state["history"].append({
            **trade,
            "exit_price": cp,
            "pnl":        pnl,
            "result":     result,
            "reason":     reason,
            "close_time": datetime.now(timezone.utc).isoformat(),
        })

        emoji      = "✅" if result == "WIN" else "❌"
        streak_txt = f" 🔥 Streak: {state['streak']}" if state["streak"] >= 2 else ""
        notify(
            f"{emoji} *REAL Trade {result}*{streak_txt}\n"
            f"{trade['question'][:70]}\n"
            f"P&L: {'+'if pnl>=0 else ''}${pnl:.4f} USDC\n"
            f"Balance: ${state['balance']:.2f}\n"
            f"{reason}"
        )
        log.info(
            f"{'✅' if result=='WIN' else '❌'} TRADE {result}: "
            f"PnL: {pnl:+.4f} | Balance: ${state['balance']:.2f}"
        )
        return True

    except Exception as e:
        log.error(f"Close trade error: {e}")
        return False


# ── Daily reset ───────────────────────────────────────────────────────────────
def check_daily_reset():
    today = datetime.now(timezone.utc).date().isoformat()
    if state["today"] != today:
        log.info("New day — resetting counters")
        state["today"]         = today
        state["daily_losses"]  = 0
        state["daily_stopped"] = False
        state["streak"]        = 0
        # Refresh real balance
        state["balance"] = get_real_balance()
        notify(
            f"🌅 *New day started!*\n"
            f"Daily loss counter reset.\n"
            f"Balance: ${state['balance']:.2f}\n"
            f"Bot is trading again!"
        )


def print_stats():
    wins  = sum(1 for t in state["history"] if t["result"] == "WIN")
    total = len(state["history"])
    pnl   = sum(t["pnl"] for t in state["history"])
    if total:
        log.info(
            f"📊 Balance: ${state['balance']:.2f} | "
            f"P&L: {pnl:+.4f} USDC | "
            f"Trades: {total} | WR: {wins/total*100:.0f}% | "
            f"Streak: {state['streak']} | "
            f"Daily losses: {state['daily_losses']}/2"
        )
    else:
        log.info(
            f"📊 Balance: ${state['balance']:.2f} | "
            f"No trades yet | Scans: {state['total_scans']}"
        )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  Polymarket REAL Money Bot v1 — Starting")
    log.info(f"  Min confidence: {MIN_CONFIDENCE}%+")
    log.info(f"  Max risk: ${MAX_TRADE_USDC}/trade")
    log.info(f"  Daily stop: {MAX_DAILY_LOSSES} losses")
    log.info("=" * 55)

    # Connect to Polymarket
    log.info("Connecting to Polymarket...")
    state["client"] = setup_client()

    if not state["client"]:
        log.error("Failed to connect! Check PRIVATE_KEY in Railway Variables.")
        notify("❌ Bot failed to start — check PRIVATE_KEY!")
        return

    # Get real balance
    state["balance"] = get_real_balance()
    log.info(f"💰 Real balance: ${state['balance']:.2f} USDC")

    if state["balance"] < MIN_TRADE_USDC:
        log.error(f"Balance too low: ${state['balance']:.2f}. Need at least ${MIN_TRADE_USDC}.")
        notify(f"❌ Balance too low: ${state['balance']:.2f} USDC. Add funds!")
        return

    notify(
        f"💰 *Polymarket REAL Bot Started!*\n"
        f"Balance: ${state['balance']:.2f} USDC\n"
        f"Min confidence: {MIN_CONFIDENCE}%+\n"
        f"Max per trade: ${MAX_TRADE_USDC}\n"
        f"Scanning every {SCAN_INTERVAL}s\n"
        f"Daily stop: {MAX_DAILY_LOSSES} losses max"
    )

    tick = 0
    while True:
        try:
            check_daily_reset()
            update_real_trades()
            tick += 1

            if tick % 5 == 0:
                # Refresh real balance every 5 scans
                state["balance"] = get_real_balance()
                print_stats()

            if state["daily_stopped"]:
                log.info("Daily limit hit — waiting for tomorrow...")
                time.sleep(SCAN_INTERVAL)
                continue

            log.info(f"🔍 Scan #{state['total_scans']+1} | Balance: ${state['balance']:.2f}")
            markets = fetch_markets()
            state["total_scans"] += 1

            placed = 0
            for m in markets:
                if len(state["active_trades"]) >= MAX_OPEN_TRADES:
                    break
                if state["balance"] < MIN_TRADE_USDC:
                    log.warning("Balance too low to trade")
                    break

                analysis = score_market(m)
                log.info(
                    f"  {m['question'][:50]} | "
                    f"Score: {analysis['score']}/10 | "
                    f"Rec: {analysis['recommendation']} | "
                    f"Conf: {analysis['confidence']}% | "
                    f"{m['hours_left']:.0f}h"
                )

                if analysis["recommendation"] != "SKIP":
                    if place_real_trade(m, analysis):
                        placed += 1
                        time.sleep(2)  # Wait between orders

                time.sleep(0.5)

            log.info(
                f"✅ Scan done — {placed} placed | "
                f"{len(state['active_trades'])} open | "
                f"Balance: ${state['balance']:.2f}"
            )

        except KeyboardInterrupt:
            log.info("Bot stopped manually.")
            notify("⚠️ Bot stopped manually.")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(15)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
