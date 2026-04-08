"""
Polymarket REAL Money Trading Bot v2
Ultra-fast payouts only — 20 to 60 minute markets
Real order placement via py-clob-client
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
PRIVATE_KEY      = os.environ.get("PRIVATE_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Strategy — FAST PAYOUT ONLY ───────────────────────────────────────────────
MIN_CONFIDENCE    = 65        # Only trade 65%+ odds
MAX_TRADE_USDC    = 1.50      # Max $ per trade
MIN_TRADE_USDC    = 0.50      # Min $ per trade
MAX_OPEN_TRADES   = 3         # Max trades at once
MAX_DAILY_LOSSES  = 2         # Stop after 2 losses
MIN_VOLUME        = 5_000     # Min market volume $
SCAN_INTERVAL     = 30        # Scan every 30 seconds (faster for short markets)
TAKE_PROFIT       = 0.93      # Close at 93¢
STOP_LOSS_PCT     = 0.45      # Close if down 45%

# ── Fast payout window ────────────────────────────────────────────────────────
MAX_HOURS         = 1.0       # Only markets resolving within 1 hour
FALLBACK_HOURS    = 6.0       # If no 1h markets, look up to 6 hours
MIN_HOURS         = 0.25      # Minimum 15 minutes left (avoid expired)

# ── APIs ──────────────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
CHAIN_ID  = 137

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


# ── Polymarket client setup ───────────────────────────────────────────────────
def setup_client():
    try:
        from py_clob_client.client import ClobClient
        if not PRIVATE_KEY:
            log.error("No PRIVATE_KEY set in Railway Variables!")
            return None
        client = ClobClient(
            host=CLOB_API,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        log.info("✅ Polymarket client connected!")
        return client
    except ImportError:
        log.error("py-clob-client not installed!")
        return None
    except Exception as e:
        log.error(f"Client setup failed: {e}")
        return None


def get_real_balance() -> float:
    try:
        if not state["client"]:
            return state["balance"]
        bal = state["client"].get_usdc_balance()
        return round(float(bal), 4)
    except Exception as e:
        log.warning(f"Balance error: {e}")
        return state["balance"]


# ── Market fetching ───────────────────────────────────────────────────────────
def hours_until(end_date: str) -> float:
    if not end_date:
        return 9999.0
    try:
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        diff   = (end_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, diff / 3600)
    except Exception:
        return 9999.0


def parse_prices(op) -> tuple[float, float]:
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except Exception:
            return 0.5, 0.5
    if isinstance(op, list) and len(op) >= 2:
        return float(op[0]), float(op[1])
    return 0.5, 0.5


def fetch_all_markets() -> list[dict]:
    """Fetch markets sorted by soonest ending first."""
    markets = []
    seen    = set()
    urls    = [
        f"{GAMMA_API}/markets?active=true&closed=false&limit=100&order=endDate&ascending=true",
        f"{GAMMA_API}/markets?active=true&closed=false&limit=50&order=volume&ascending=false",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            for m in r.json():
                mid = m.get("id")
                if mid in seen:
                    continue
                seen.add(mid)

                yp, np_ = parse_prices(m.get("outcomePrices"))
                hl      = hours_until(m.get("endDate"))
                vol     = float(m.get("volume") or 0)

                if hl < MIN_HOURS or vol < MIN_VOLUME:
                    continue

                tokens    = m.get("tokens", [])
                yes_token = next((t.get("token_id") for t in tokens if t.get("outcome","").upper() == "YES"), None)
                no_token  = next((t.get("token_id") for t in tokens if t.get("outcome","").upper() == "NO"),  None)

                markets.append({
                    "id":           mid,
                    "question":     m.get("question") or m.get("title", "Unknown"),
                    "yes_price":    yp,
                    "no_price":     np_,
                    "volume":       vol,
                    "hours_left":   hl,
                    "minutes_left": round(hl * 60),
                    "category":     m.get("category", "Market"),
                    "yes_token_id": yes_token,
                    "no_token_id":  no_token,
                })
        except Exception as e:
            log.warning(f"Fetch error: {e}")

    # Sort soonest first
    markets.sort(key=lambda m: m["hours_left"])
    return markets


def get_fast_markets(all_markets: list[dict]) -> list[dict]:
    """
    Priority 1: Markets resolving in < 1 hour (ideal 20-60 min)
    Priority 2: If none, markets resolving in < 6 hours
    """
    ultra_fast = [m for m in all_markets if m["hours_left"] <= MAX_HOURS]
    if ultra_fast:
        log.info(f"⚡ Found {len(ultra_fast)} ultra-fast markets (< {int(MAX_HOURS*60)} min)")
        return ultra_fast[:10]

    fast = [m for m in all_markets if m["hours_left"] <= FALLBACK_HOURS]
    if fast:
        log.info(f"🕐 No ultra-fast markets — using {len(fast)} fast markets (< {FALLBACK_HOURS:.0f}h)")
        return fast[:10]

    log.info("⏳ No fast markets right now — will retry next scan")
    return []


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_market(m: dict) -> dict:
    yp       = m["yes_price"]
    np_      = m["no_price"]
    dominant = max(yp, np_)
    side     = "BUY_YES" if yp > np_ else "BUY_NO"
    mins     = m["minutes_left"]

    # Base score from odds
    if dominant >= 0.88:   score = 10
    elif dominant >= 0.82: score = 9
    elif dominant >= 0.75: score = 8
    elif dominant >= 0.70: score = 7
    elif dominant >= 0.65: score = 6
    elif dominant >= 0.60: score = 5
    else:                  score = 2

    # Big bonus for ultra-fast resolution
    if mins <= 30:    score = min(10, score + 3)
    elif mins <= 60:  score = min(10, score + 2)
    elif mins <= 120: score = min(10, score + 1)

    # Volume bonus
    if m["volume"] >= 200_000: score = min(10, score + 1)

    conf = int(dominant * 100)
    rec  = side if conf >= MIN_CONFIDENCE else "SKIP"

    # Time label
    if mins < 60:
        time_label = f"{mins}min"
    else:
        time_label = f"{m['hours_left']:.1f}h"

    return {
        "score":          score,
        "recommendation": rec,
        "confidence":     conf,
        "reason":         f"{conf}% confidence · pays out in {time_label}",
        "edge":           f"${m['volume']/1000:.0f}k volume",
        "time_label":     time_label,
    }


# ── Real trade placement ──────────────────────────────────────────────────────
def place_real_trade(m: dict, analysis: dict) -> bool:
    if not state["client"]:
        log.error("No client!")
        return False
    if state["daily_stopped"]:
        return False
    if len(state["active_trades"]) >= MAX_OPEN_TRADES:
        return False
    if any(t["market_id"] == m["id"] for t in state["active_trades"]):
        return False
    if analysis["recommendation"] == "SKIP":
        return False

    side     = "YES" if analysis["recommendation"] == "BUY_YES" else "NO"
    ep       = m["yes_price"] if side == "YES" else m["no_price"]
    token_id = m["yes_token_id"] if side == "YES" else m["no_token_id"]

    if not token_id:
        log.warning("No token ID — skipping")
        return False

    ep   = max(0.01, min(0.99, ep))
    conf = analysis["confidence"] / 100
    edge = max(0.01, conf - (1 - conf) / max(0.01, (1 / ep - 1)))
    kf   = max(0.05, min(0.10, edge))
    size = round(min(MAX_TRADE_USDC, max(MIN_TRADE_USDC, state["balance"] * kf)), 2)

    if size > state["balance"]:
        log.warning(f"Not enough balance: ${state['balance']:.2f}")
        return False

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        order_args   = MarketOrderArgs(token_id=token_id, amount=size)
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
                "hours_left":    m["hours_left"],
                "minutes_left":  m["minutes_left"],
                "open_time":     datetime.now(timezone.utc).isoformat(),
                "category":      m["category"],
            }

            state["balance"]          -= size
            state["balance"]           = round(state["balance"], 4)
            state["active_trades"].append(trade)

            notify(
                f"💰 *REAL Trade Opened!*\n"
                f"📊 {m['question'][:80]}\n"
                f"Side: *{side}* @ {fill_price*100:.0f}¢\n"
                f"Size: ${size:.2f} | Score: {analysis['score']}/10\n"
                f"⏱ Pays out in: *{analysis['time_label']}*\n"
                f"Confidence: {analysis['confidence']}%\n"
                f"Balance left: ${state['balance']:.2f}"
            )
            log.info(
                f"💰 REAL TRADE: {side} ${size:.2f} @ {fill_price*100:.0f}¢ "
                f"| Payout in {analysis['time_label']} "
                f"| Score {analysis['score']}/10"
            )
            return True
        else:
            log.warning(f"Order rejected: {resp}")
            return False

    except Exception as e:
        log.error(f"Trade error: {e}")
        return False


# ── Monitor trades ────────────────────────────────────────────────────────────
def update_trades():
    if not state["active_trades"]:
        return

    closed = []
    for trade in state["active_trades"]:
        try:
            token_id = trade.get("token_id")
            if token_id:
                r = requests.get(
                    f"{CLOB_API}/price?token_id={token_id}&side=BUY",
                    timeout=5
                )
                if r.ok:
                    trade["current_price"] = float(r.json().get("price", trade["current_price"]))

            cp       = trade["current_price"]
            mins     = trade.get("minutes_left", 60)
            time_txt = f"{mins}min" if mins < 60 else f"{trade['hours_left']:.1f}h"

            if cp >= trade["take_profit"]:
                close_trade(trade, "WIN", f"✓ Take profit @ {cp*100:.0f}¢ | was {time_txt} market")
                closed.append(trade["id"])
            elif cp <= trade["stop_loss"]:
                close_trade(trade, "LOSS", f"✗ Stop loss @ {cp*100:.0f}¢")
                closed.append(trade["id"])

        except Exception as e:
            log.warning(f"Monitor error: {e}")

    state["active_trades"] = [t for t in state["active_trades"] if t["id"] not in closed]


def close_trade(trade: dict, result: str, reason: str):
    cp  = trade["current_price"]
    val = trade["shares"] * cp
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
                f"2 losses today — paused to protect your money.\n"
                f"Balance: ${state['balance']:.2f} USDC\n"
                "Will auto-resume tomorrow."
            )

    state["history"].append({
        **trade,
        "exit_price": cp,
        "pnl":        pnl,
        "result":     result,
        "reason":     reason,
        "close_time": datetime.now(timezone.utc).isoformat(),
    })

    wins  = sum(1 for t in state["history"] if t["result"] == "WIN")
    total = len(state["history"])
    wr    = f"{wins/total*100:.0f}%" if total else "—"

    emoji      = "✅" if result == "WIN" else "❌"
    streak_txt = f"\n🔥 Win streak: {state['streak']}" if state["streak"] >= 2 else ""

    notify(
        f"{emoji} *REAL Trade {result}*{streak_txt}\n"
        f"{trade['question'][:70]}\n"
        f"P&L: {'+'if pnl>=0 else ''}${pnl:.4f} USDC\n"
        f"Balance: ${state['balance']:.2f}\n"
        f"Win rate: {wr} ({total} trades)\n"
        f"{reason}"
    )
    log.info(
        f"{'✅' if result=='WIN' else '❌'} {result}: "
        f"PnL {pnl:+.4f} | Balance ${state['balance']:.2f} | WR {wr}"
    )


# ── Daily reset ───────────────────────────────────────────────────────────────
def check_daily_reset():
    today = datetime.now(timezone.utc).date().isoformat()
    if state["today"] != today:
        state["today"]         = today
        state["daily_losses"]  = 0
        state["daily_stopped"] = False
        state["streak"]        = 0
        state["balance"]       = get_real_balance()
        notify(
            f"🌅 *New day — bot trading again!*\n"
            f"Balance: ${state['balance']:.2f} USDC"
        )


def print_stats():
    wins  = sum(1 for t in state["history"] if t["result"] == "WIN")
    total = len(state["history"])
    pnl   = sum(t["pnl"] for t in state["history"])
    log.info(
        f"📊 Balance: ${state['balance']:.2f} | P&L: {pnl:+.4f} | "
        f"Trades: {total} | WR: {wins/total*100:.0f}% | "
        f"Streak: {state['streak']} | Losses today: {state['daily_losses']}/2"
        if total else
        f"📊 Balance: ${state['balance']:.2f} | No trades yet | Scans: {state['total_scans']}"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("  Polymarket REAL Bot v2 — Ultra Fast Payouts")
    log.info(f"  Target: markets resolving in 20-60 minutes")
    log.info(f"  Fallback: up to {FALLBACK_HOURS:.0f} hours if no fast markets")
    log.info(f"  Max per trade: ${MAX_TRADE_USDC} | Stop: {MAX_DAILY_LOSSES} losses/day")
    log.info("=" * 60)

    log.info("Connecting to Polymarket...")
    state["client"] = setup_client()

    if not state["client"]:
        notify("❌ Bot failed — check PRIVATE_KEY in Railway Variables!")
        return

    state["balance"] = get_real_balance()
    log.info(f"💰 Real balance: ${state['balance']:.2f} USDC")

    if state["balance"] < MIN_TRADE_USDC:
        notify(f"❌ Balance too low: ${state['balance']:.2f}. Need at least ${MIN_TRADE_USDC}!")
        return

    notify(
        f"💰 *Polymarket REAL Bot v2 Started!*\n"
        f"Balance: ${state['balance']:.2f} USDC\n"
        f"⚡ Target: 20-60 minute payouts\n"
        f"Max per trade: ${MAX_TRADE_USDC}\n"
        f"Min confidence: {MIN_CONFIDENCE}%\n"
        f"Scanning every {SCAN_INTERVAL}s\n"
        f"Daily stop: after {MAX_DAILY_LOSSES} losses"
    )

    tick = 0
    while True:
        try:
            check_daily_reset()
            update_trades()
            tick += 1

            if tick % 10 == 0:
                state["balance"] = get_real_balance()
                print_stats()

            if state["daily_stopped"]:
                log.info("🛑 Daily limit — waiting for tomorrow...")
                time.sleep(SCAN_INTERVAL)
                continue

            log.info(
                f"🔍 Scan #{state['total_scans']+1} | "
                f"Balance: ${state['balance']:.2f} | "
                f"Open: {len(state['active_trades'])}/{MAX_OPEN_TRADES}"
            )

            all_markets  = fetch_all_markets()
            fast_markets = get_fast_markets(all_markets)
            state["total_scans"] += 1

            if not fast_markets:
                log.info("⏳ No fast markets found — waiting for next scan...")
                time.sleep(SCAN_INTERVAL)
                continue

            placed = 0
            for m in fast_markets:
                if len(state["active_trades"]) >= MAX_OPEN_TRADES:
                    log.info("Max trades open — waiting for positions to close")
                    break
                if state["balance"] < MIN_TRADE_USDC:
                    log.warning(f"Balance too low: ${state['balance']:.2f}")
                    break

                analysis = score_market(m)
                mins     = m["minutes_left"]
                time_txt = f"{mins}min" if mins < 60 else f"{m['hours_left']:.1f}h"

                log.info(
                    f"  [{time_txt}] {m['question'][:50]} | "
                    f"Score: {analysis['score']}/10 | "
                    f"Rec: {analysis['recommendation']} | "
                    f"Conf: {analysis['confidence']}%"
                )

                if analysis["recommendation"] != "SKIP":
                    if place_real_trade(m, analysis):
                        placed += 1
                        time.sleep(2)

                time.sleep(0.5)

            log.info(
                f"✅ Scan done — {placed} placed | "
                f"{len(state['active_trades'])} open | "
                f"Balance: ${state['balance']:.2f}"
            )

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            notify("⚠️ Bot stopped manually.")
            break
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
            time.sleep(15)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
