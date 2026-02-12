#!/usr/bin/env python3
"""
Polymarket LP Rewards Bot
=========================
Provides liquidity 24/7, earns rewards, and avoids getting filled.
"""

import os
import sys
import json
import time
import math
import signal
import traceback

# Force print to flush immediately (needed for Railway/Docker logs)
import functools
print = functools.partial(print, flush=True)

print("🔧 Bot starting up...")

try:
    import requests
    print("  ✅ requests loaded")
except ImportError as e:
    print(f"  ❌ Failed to import requests: {e}")
    sys.exit(1)

from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv("keys.env")
    print("  ✅ dotenv loaded")
except ImportError:
    print("  ⚠ python-dotenv not found, using env vars only")

print(f"  PK set: {'YES' if os.getenv('PK') else 'NO'}")
print(f"  FUNDER set: {'YES' if os.getenv('FUNDER_ADDRESS') else 'NO'}")
print(f"  SIG_TYPE: {os.getenv('SIGNATURE_TYPE', 'not set')}")

# ─── Configuration ───────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Bot parameters (tweak these)
DEFAULT_SIZE_PER_MARKET = float(os.getenv("SIZE_PER_MARKET", "500"))   # $500 per market
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "10"))                      # Max simultaneous markets
MIN_DAYS_TO_RESOLUTION = int(os.getenv("MIN_DAYS_TO_RESOLUTION", "14"))# Min 2 weeks out
MIN_DAILY_REWARD = float(os.getenv("MIN_DAILY_REWARD", "1.0"))        # Min $1/day reward
MAX_COMPETITION_SCORE = float(os.getenv("MAX_COMPETITION_SCORE", "70"))# Lower = less competition
FILL_ALERT_THRESHOLD = float(os.getenv("FILL_ALERT_THRESHOLD", "0.02"))# Alert if price within 2c
SPREAD_SAFETY_MARGIN = float(os.getenv("SPREAD_SAFETY_MARGIN", "0.005"))# Extra 0.5c from midpoint
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "60"))            # Seconds between checks

# Telegram config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class RewardMarket:
    """A market eligible for LP rewards."""
    condition_id: str
    question: str
    slug: str
    token_id_yes: str
    token_id_no: str
    end_date: Optional[str]
    days_to_resolution: float
    daily_reward: float
    max_spread: float
    min_size: float
    midpoint: float
    best_bid: float
    best_ask: float
    spread: float
    volume_24h: float
    liquidity: float
    competition_score: float  # 0-100, lower = less competition
    risk_score: float         # 0-100, lower = safer
    reward_per_dollar: float  # Daily reward relative to required capital

@dataclass
class ActivePosition:
    """A position we currently have on a market."""
    market: RewardMarket
    order_id_yes: Optional[str] = None
    order_id_no: Optional[str] = None
    our_bid_price: float = 0.0
    our_ask_price: float = 0.0
    size: float = 0.0
    placed_at: str = ""
    fills_today: int = 0
    rewards_earned: float = 0.0
    risk_level: str = "LOW"


# ─── Telegram Notifications ─────────────────────────────────────────────────

def send_telegram(message: str):
    """Send a Telegram notification."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
    except Exception as e:
        print(f"  ⚠ Telegram error: {e}")


# ─── API Helpers ─────────────────────────────────────────────────────────────

def gamma_get(endpoint: str, params: dict = None) -> dict:
    """Make a GET request to the Gamma API."""
    url = f"{GAMMA_API}{endpoint}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def clob_get(endpoint: str, params: dict = None) -> dict:
    """Make a GET request to the CLOB API (public, no auth)."""
    url = f"{CLOB_API}{endpoint}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ─── Market Scanner ─────────────────────────────────────────────────────────

def fetch_reward_markets() -> list[RewardMarket]:
    """
    Fetch all markets with active LP rewards and score them.
    Returns markets sorted by attractiveness (best first).
    """
    print("\n🔍 Scanning eligible reward markets...")
    
    # Strategy: fetch markets directly from /markets endpoint
    # which includes reward fields (rewardsMinSize, rewardsMaxSpread, rewards[])
    all_markets = []
    offset = 0
    limit = 100
    
    while True:
        try:
            markets_raw = gamma_get("/markets", params={
                "active": "true",
                "closed": "false",
                "limit": str(limit),
                "offset": str(offset),
            })
        except Exception as e:
            print(f"  ⚠ Error fetching markets (offset={offset}): {e}")
            break
        
        if not markets_raw:
            break
        
        # Debug: print field names of first market to find reward fields
        if offset == 0 and markets_raw:
            first = markets_raw[0]
            reward_fields = [k for k in first.keys() if 'reward' in k.lower() or 'incentive' in k.lower() or 'spread' in k.lower()]
            print(f"  Reward-related fields found: {reward_fields}")
            # Also check for nested rewards
            if 'rewards' in first:
                print(f"  rewards field type: {type(first['rewards'])}")
                if first['rewards']:
                    print(f"  rewards sample: {first['rewards'][:1] if isinstance(first['rewards'], list) else first['rewards']}")
        
        for market in markets_raw:
            parsed = parse_market(market, {})
            if parsed:
                all_markets.append(parsed)
        
        if len(markets_raw) < limit:
            break
        offset += limit
        time.sleep(0.3)  # Rate limit
    
    # Filter for reward-eligible markets
    reward_markets = [m for m in all_markets if m.daily_reward > 0 and m.max_spread > 0]
    
    print(f"  Found {len(all_markets)} active markets, {len(reward_markets)} with rewards")
    
    # Apply our filters
    filtered = []
    for m in reward_markets:
        if m.days_to_resolution < MIN_DAYS_TO_RESOLUTION:
            continue
        if m.daily_reward < MIN_DAILY_REWARD:
            continue
        if m.competition_score > MAX_COMPETITION_SCORE:
            continue
        filtered.append(m)
    
    # Sort by risk-adjusted reward (best first)
    filtered.sort(key=lambda m: m.reward_per_dollar, reverse=True)
    
    print(f"  After filtering: {len(filtered)} opportunities match criteria")
    return filtered


def parse_market(market: dict, event: dict) -> Optional[RewardMarket]:
    """Parse a raw market dict into a RewardMarket."""
    try:
        # Extract token IDs
        clob_token_ids = market.get("clobTokenIds")
        if not clob_token_ids or len(clob_token_ids) < 2:
            return None
        
        token_id_yes = clob_token_ids[0]
        token_id_no = clob_token_ids[1]
        
        # ── Rewards info (try multiple field name patterns) ──
        rewards_daily = 0
        max_spread = 0
        min_size = 0
        
        # Pattern 1: Top-level camelCase (rewardsMinSize, rewardsMaxSpread)
        max_spread = float(market.get("rewardsMaxSpread", 0) or 0)
        min_size = float(market.get("rewardsMinSize", 0) or 0)
        
        # Pattern 2: Top-level snake_case
        if max_spread == 0:
            max_spread = float(market.get("rewards_max_spread", 0) or 0)
        if min_size == 0:
            min_size = float(market.get("rewards_min_size", 0) or 0)
        
        # Pattern 3: Incentive fields
        if max_spread == 0:
            max_spread = float(market.get("max_incentive_spread", 0) or 0)
        if min_size == 0:
            min_size = float(market.get("min_incentive_size", 0) or 0)
        
        # Pattern 4: Nested "rewards" array with dailyRate
        rewards_arr = market.get("rewards", [])
        if isinstance(rewards_arr, list) and rewards_arr:
            for r in rewards_arr:
                if isinstance(r, dict):
                    rewards_daily += float(r.get("rewardsDailyRate", 0) or r.get("dailyRate", 0) or r.get("rewards_daily_rate", 0) or 0)
        
        # Pattern 5: Top-level rewardsDailyRate
        if rewards_daily == 0:
            rewards_daily = float(market.get("rewardsDailyRate", 0) or 0)
        if rewards_daily == 0:
            rewards_daily = float(market.get("rewards_daily_rate", 0) or 0)
        
        # Pattern 6: "competitive" field can indicate reward activity
        competitive = float(market.get("competitive", 0) or 0)
        
        # Convert max_spread from cents to decimal if it looks like cents (> 1)
        if max_spread > 1:
            max_spread = max_spread / 100.0
        
        # ── End date / resolution ──
        end_date = market.get("endDate") or event.get("endDate") or market.get("end_date_iso")
        days_to_resolution = 365  # Default if no end date
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                days_to_resolution = max(0, (end_dt - now).total_seconds() / 86400)
            except:
                pass
        
        # ── Prices ──
        outcome_prices = market.get("outcomePrices", "[]")
        if isinstance(outcome_prices, str):
            try:
                prices = json.loads(outcome_prices)
            except:
                prices = [0.5, 0.5]
        else:
            prices = outcome_prices
        
        yes_price = float(prices[0]) if len(prices) > 0 else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else 0.5
        midpoint = yes_price
        
        # ── Volume and liquidity ──
        volume_24h = float(market.get("volume24hr", 0) or market.get("volume_num", 0) or 0)
        liquidity = float(market.get("liquidity", 0) or market.get("liquidityNum", 0) or 0)
        
        # ── Spread ──
        best_bid = float(market.get("bestBid", 0) or 0)
        best_ask = float(market.get("bestAsk", 0) or 0)
        if best_bid > 0 and best_ask > 0:
            spread = best_ask - best_bid
        else:
            spread = abs(yes_price - (1 - no_price)) if no_price else 0
            best_bid = yes_price - spread / 2
            best_ask = yes_price + spread / 2
        
        # ── Competition score ──
        if rewards_daily > 0:
            competition_score = min(100, (liquidity / (rewards_daily * 100)) * 10)
        else:
            competition_score = 100
        
        # ── Risk score ──
        time_risk = max(0, 50 - days_to_resolution) * 2
        price_risk = (1 - abs(midpoint - 0.5) * 2) * 50
        volume_risk = min(50, volume_24h / 1000 * 10)
        risk_score = min(100, (time_risk + price_risk + volume_risk) / 3)
        
        # ── Reward per dollar ──
        capital_needed = min_size * 2 if min_size > 0 else 100
        reward_per_dollar = rewards_daily / max(capital_needed, 1) if rewards_daily > 0 else 0
        
        return RewardMarket(
            condition_id=market.get("conditionId", market.get("condition_id", "")),
            question=market.get("question", "Unknown"),
            slug=market.get("slug", ""),
            token_id_yes=token_id_yes,
            token_id_no=token_id_no,
            end_date=end_date,
            days_to_resolution=days_to_resolution,
            daily_reward=rewards_daily,
            max_spread=max_spread,
            min_size=min_size,
            midpoint=midpoint,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            volume_24h=volume_24h,
            liquidity=liquidity,
            competition_score=competition_score,
            risk_score=risk_score,
            reward_per_dollar=reward_per_dollar,
        )
    except Exception as e:
        return None


# ─── Order Management (requires py-clob-client) ─────────────────────────────

def get_clob_client():
    """Initialize authenticated CLOB client."""
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("❌ py-clob-client not installed. Run: pip install py-clob-client")
        sys.exit(1)
    
    private_key = os.getenv("PK")
    funder = os.getenv("FUNDER_ADDRESS", "")
    sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))  # 1 for Magic/email
    
    if not private_key:
        print("❌ PK (private key) not set in keys.env")
        sys.exit(1)
    
    client = ClobClient(
        host=CLOB_API,
        key=private_key,
        chain_id=137,
        signature_type=sig_type,
        funder=funder if funder else None,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def place_lp_orders(client, market: RewardMarket, size: float) -> tuple:
    """
    Place two-sided limit orders for LP rewards.
    Strategy: place orders at the EDGE of the max spread to minimize fill risk.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    
    mid = market.midpoint
    max_sp = market.max_spread
    
    # Place orders at the outer edge of reward eligibility + safety margin
    # This maximizes reward eligibility while minimizing fill risk
    bid_price = round(mid - max_sp + SPREAD_SAFETY_MARGIN, 3)
    ask_price = round(mid + max_sp - SPREAD_SAFETY_MARGIN, 3)
    
    # Clamp prices to valid range
    bid_price = max(0.01, min(0.99, bid_price))
    ask_price = max(0.01, min(0.99, ask_price))
    
    # Calculate share sizes
    bid_shares = size / bid_price if bid_price > 0 else 0
    ask_shares = size / ask_price if ask_price > 0 else 0
    
    # Ensure we meet minimum size
    min_shares = market.min_size
    bid_shares = max(bid_shares, min_shares)
    ask_shares = max(ask_shares, min_shares)
    
    order_id_yes = None
    order_id_no = None
    
    try:
        # BUY YES at bid (we want to provide liquidity on the buy side)
        buy_order = OrderArgs(
            token_id=market.token_id_yes,
            price=bid_price,
            size=bid_shares,
            side=BUY,
        )
        signed_buy = client.create_order(buy_order)
        resp_buy = client.post_order(signed_buy, OrderType.GTC)
        order_id_yes = resp_buy.get("orderID") or resp_buy.get("id")
        print(f"  ✅ BUY YES @ {bid_price:.3f} x {bid_shares:.0f} shares")
    except Exception as e:
        print(f"  ❌ BUY YES failed: {e}")
    
    try:
        # BUY NO at (1 - ask_price) which is equivalent to SELL YES
        no_bid_price = round(1 - ask_price, 3)
        no_bid_price = max(0.01, min(0.99, no_bid_price))
        no_shares = size / no_bid_price if no_bid_price > 0 else 0
        no_shares = max(no_shares, min_shares)
        
        sell_order = OrderArgs(
            token_id=market.token_id_no,
            price=no_bid_price,
            size=no_shares,
            side=BUY,
        )
        signed_sell = client.create_order(sell_order)
        resp_sell = client.post_order(signed_sell, OrderType.GTC)
        order_id_no = resp_sell.get("orderID") or resp_sell.get("id")
        print(f"  ✅ BUY NO  @ {no_bid_price:.3f} x {no_shares:.0f} shares")
    except Exception as e:
        print(f"  ❌ BUY NO failed: {e}")
    
    return order_id_yes, order_id_no


def cancel_order(client, order_id: str):
    """Cancel an existing order."""
    try:
        client.cancel(order_id)
        print(f"  🗑 Cancelled order {order_id[:12]}...")
    except Exception as e:
        print(f"  ⚠ Cancel failed for {order_id[:12]}...: {e}")


def get_open_orders(client) -> list:
    """Get all open orders for our account."""
    try:
        from py_clob_client.clob_types import OpenOrderParams
        orders = client.get_orders(OpenOrderParams())
        return orders if orders else []
    except Exception as e:
        print(f"  ⚠ Error fetching orders: {e}")
        return []


# ─── Fill Monitoring ─────────────────────────────────────────────────────────

def check_fill_risk(positions: list[ActivePosition]) -> list[str]:
    """
    Check if any of our orders are at risk of being filled.
    Returns list of alert messages.
    """
    alerts = []
    
    for pos in positions:
        market = pos.market
        
        try:
            # Get current midpoint from CLOB
            mid_resp = clob_get(f"/midpoint", params={"token_id": market.token_id_yes})
            current_mid = float(mid_resp.get("mid", market.midpoint))
        except:
            current_mid = market.midpoint
        
        # Check if midpoint has moved toward our orders
        bid_distance = abs(current_mid - pos.our_bid_price)
        ask_distance = abs(current_mid - pos.our_ask_price)
        
        risk_level = "LOW"
        
        if bid_distance < FILL_ALERT_THRESHOLD or ask_distance < FILL_ALERT_THRESHOLD:
            risk_level = "🔴 CRITICAL"
            msg = (
                f"🚨 <b>FILL RISK - {market.question[:60]}</b>\n"
                f"Midpoint: {current_mid:.3f}\n"
                f"Our bid: {pos.our_bid_price:.3f} (dist: {bid_distance:.3f})\n"
                f"Our ask: {pos.our_ask_price:.3f} (dist: {ask_distance:.3f})\n"
                f"⚠️ Consider cancelling orders!"
            )
            alerts.append(msg)
            send_telegram(msg)
        elif bid_distance < FILL_ALERT_THRESHOLD * 2 or ask_distance < FILL_ALERT_THRESHOLD * 2:
            risk_level = "🟡 WARNING"
            msg = (
                f"⚠️ <b>APPROACHING FILL - {market.question[:60]}</b>\n"
                f"Midpoint: {current_mid:.3f} | "
                f"Bid dist: {bid_distance:.3f} | Ask dist: {ask_distance:.3f}"
            )
            alerts.append(msg)
            send_telegram(msg)
        elif bid_distance < FILL_ALERT_THRESHOLD * 3 or ask_distance < FILL_ALERT_THRESHOLD * 3:
            risk_level = "🟠 WATCH"
        else:
            risk_level = "🟢 SAFE"
        
        pos.risk_level = risk_level
    
    return alerts


# ─── Terminal Dashboard ──────────────────────────────────────────────────────

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_dashboard(positions: list[ActivePosition], scan_results: list[RewardMarket] = None):
    """Print a beautiful terminal dashboard."""
    clear_screen()
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print("╔══════════════════════════════════════════════════════════════════════════════╗")
    print("║              🏦  POLYMARKET LP REWARDS BOT  🏦                              ║")
    print(f"║  {now}                                                       ║")
    print("╠══════════════════════════════════════════════════════════════════════════════╣")
    
    if positions:
        # Sort by risk level (highest risk first)
        risk_order = {"🔴 CRITICAL": 0, "🟡 WARNING": 1, "🟠 WATCH": 2, "🟢 SAFE": 3, "LOW": 4}
        positions.sort(key=lambda p: risk_order.get(p.risk_level, 5))
        
        total_capital = sum(p.size for p in positions)
        total_rewards = sum(p.market.daily_reward for p in positions)
        
        print(f"║  💰 Capital Deployed: ${total_capital:,.0f}  |  📈 Est. Daily Rewards: ${total_rewards:,.2f}  ║")
        print(f"║  📊 Active Positions: {len(positions)}                                                   ║")
        print("╠══════════════════════════════════════════════════════════════════════════════╣")
        print("║  RISK  │ MARKET                                    │ MID  │ REWARD │ DAYS  ║")
        print("╟────────┼───────────────────────────────────────────┼──────┼────────┼───────╢")
        
        for pos in positions:
            m = pos.market
            q = m.question[:39].ljust(39)
            risk = pos.risk_level.ljust(12) if pos.risk_level else "LOW".ljust(12)
            print(f"║ {risk}│ {q} │ {m.midpoint:.2f} │ ${m.daily_reward:5.1f} │ {m.days_to_resolution:5.0f} ║")
    else:
        print("║  No active positions. Run 'python bot.py run' to start.                   ║")
    
    print("╠══════════════════════════════════════════════════════════════════════════════╣")
    
    if scan_results:
        print("║  🔍 TOP OPPORTUNITIES                                                     ║")
        print("╟────────┬───────────────────────────────────────────┬──────┬────────┬───────╢")
        print("║  RANK  │ MARKET                                    │ $/D  │ COMP.  │ DAYS  ║")
        print("╟────────┼───────────────────────────────────────────┼──────┼────────┼───────╢")
        
        for i, m in enumerate(scan_results[:10]):
            q = m.question[:39].ljust(39)
            print(f"║  #{i+1:<4} │ {q} │ ${m.daily_reward:4.1f} │ {m.competition_score:5.1f} │ {m.days_to_resolution:5.0f} ║")
    
    print("╚══════════════════════════════════════════════════════════════════════════════╝")
    print("\n  Press Ctrl+C to stop  |  Refreshes every", REFRESH_INTERVAL, "seconds")


def print_scan_results(markets: list[RewardMarket]):
    """Print scan results in a nice table."""
    print("\n" + "=" * 100)
    print(f"  🎯 TOP {min(20, len(markets))} LP REWARD OPPORTUNITIES")
    print("=" * 100)
    print(f"  {'#':<4} {'Market':<45} {'$/day':<8} {'Spread':<8} {'Comp.':<8} {'Days':<6} {'Risk':<6}")
    print("-" * 100)
    
    for i, m in enumerate(markets[:20]):
        q = m.question[:43]
        print(f"  {i+1:<4} {q:<45} ${m.daily_reward:<7.2f} {m.max_spread:<8.3f} {m.competition_score:<8.1f} {m.days_to_resolution:<6.0f} {m.risk_score:<6.1f}")
    
    print("-" * 100)
    print(f"\n  Filters: min {MIN_DAYS_TO_RESOLUTION}d to resolution | min ${MIN_DAILY_REWARD}/day reward | max {MAX_COMPETITION_SCORE} competition")
    print(f"  Config:  ${DEFAULT_SIZE_PER_MARKET}/market | max {MAX_MARKETS} markets\n")


# ─── Setup Command ───────────────────────────────────────────────────────────

def run_setup():
    """Guide the user through initial setup."""
    print("""
╔══════════════════════════════════════════════════════════════╗
║          🔧  POLYMARKET LP BOT - SETUP GUIDE  🔧            ║
╚══════════════════════════════════════════════════════════════╝

STEP 1: Export your Polymarket private key
──────────────────────────────────────────
  • If you signed up with EMAIL/GOOGLE:
    Go to: https://reveal.magic.link/polymarket
    → Copy your private key (64 character hex string)
    → Note the address shown (0x...) — this is NOT your funder
    
  • If you signed up with METAMASK:
    → Export your private key from MetaMask
    → Your funder is your Polymarket deposit address
    
  • Your FUNDER ADDRESS is your Polymarket profile address
    Go to polymarket.com → click your profile → the address in the URL

STEP 2: Create your keys.env file
──────────────────────────────────
  Copy keys.env.example to keys.env and fill in your values.

STEP 3: Generate API credentials
─────────────────────────────────
  Run: python bot.py setup
  (This will derive your API key, secret, and passphrase)

STEP 4 (Optional): Set up Telegram alerts
──────────────────────────────────────────
  • Message @BotFather on Telegram → /newbot → get your bot token
  • Message @userinfobot → get your chat ID
  • Add both to your keys.env

STEP 5: Start the bot!
───────────────────────
  python bot.py scan       # Preview opportunities first
  python bot.py run        # Start trading
""")
    
    # Try to generate API creds if PK is set
    pk = os.getenv("PK")
    if pk:
        print("  🔑 Private key found in keys.env. Generating API credentials...\n")
        try:
            client = get_clob_client()
            creds = client.create_or_derive_api_creds()
            print(f"  ✅ API Key:      {creds.api_key}")
            print(f"  ✅ API Secret:   {creds.api_secret}")
            print(f"  ✅ Passphrase:   {creds.api_passphrase}")
            print(f"\n  Add these to your keys.env file (optional, bot derives them automatically)")
        except Exception as e:
            print(f"  ❌ Error: {e}")
            print("  Make sure your private key is correct and py-clob-client is installed.")
    else:
        print("  ⚠ No private key found. Set PK in keys.env first.")


# ─── Main Bot Loop ───────────────────────────────────────────────────────────

def run_bot():
    """Main bot loop: scan → place orders → monitor → repeat."""
    print("\n🚀 Starting Polymarket LP Rewards Bot...\n")
    
    # Initialize CLOB client
    client = get_clob_client()
    print("  ✅ Connected to Polymarket CLOB\n")
    
    send_telegram("🚀 <b>LP Bot Started</b>\nScanning for opportunities...")
    
    positions: list[ActivePosition] = []
    running = True
    
    def handle_signal(sig, frame):
        nonlocal running
        print("\n\n  🛑 Shutting down... cancelling all orders...")
        for pos in positions:
            if pos.order_id_yes:
                cancel_order(client, pos.order_id_yes)
            if pos.order_id_no:
                cancel_order(client, pos.order_id_no)
        send_telegram("🛑 <b>LP Bot Stopped</b>\nAll orders cancelled.")
        running = False
        sys.exit(0)
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    last_scan = 0
    scan_interval = 3600  # Re-scan every hour
    
    while running:
        try:
            now = time.time()
            
            # Periodic scan for new opportunities
            if now - last_scan > scan_interval or not positions:
                markets = fetch_reward_markets()
                
                if markets:
                    # Cancel existing orders if we're refreshing
                    if positions:
                        print("\n  🔄 Refreshing positions...")
                        for pos in positions:
                            if pos.order_id_yes:
                                cancel_order(client, pos.order_id_yes)
                            if pos.order_id_no:
                                cancel_order(client, pos.order_id_no)
                        positions.clear()
                    
                    # Place orders on top markets
                    for market in markets[:MAX_MARKETS]:
                        print(f"\n  📌 {market.question[:60]}")
                        print(f"     Reward: ${market.daily_reward:.2f}/day | "
                              f"Spread: {market.max_spread:.3f} | "
                              f"Days: {market.days_to_resolution:.0f}")
                        
                        oid_yes, oid_no = place_lp_orders(client, market, DEFAULT_SIZE_PER_MARKET)
                        
                        mid = market.midpoint
                        max_sp = market.max_spread
                        
                        pos = ActivePosition(
                            market=market,
                            order_id_yes=oid_yes,
                            order_id_no=oid_no,
                            our_bid_price=round(mid - max_sp + SPREAD_SAFETY_MARGIN, 3),
                            our_ask_price=round(mid + max_sp - SPREAD_SAFETY_MARGIN, 3),
                            size=DEFAULT_SIZE_PER_MARKET,
                            placed_at=datetime.now(timezone.utc).isoformat(),
                        )
                        positions.append(pos)
                    
                    total_reward = sum(p.market.daily_reward for p in positions)
                    send_telegram(
                        f"📊 <b>Positions Updated</b>\n"
                        f"Markets: {len(positions)}\n"
                        f"Capital: ${DEFAULT_SIZE_PER_MARKET * len(positions):,.0f}\n"
                        f"Est. daily reward: ${total_reward:.2f}"
                    )
                
                last_scan = now
            
            # Monitor fill risk
            if positions:
                alerts = check_fill_risk(positions)
                
                # Display dashboard
                print_dashboard(positions)
                
                if alerts:
                    print(f"\n  ⚠ {len(alerts)} alert(s) sent to Telegram")
            
            time.sleep(REFRESH_INTERVAL)
            
        except KeyboardInterrupt:
            handle_signal(None, None)
        except Exception as e:
            print(f"\n  ❌ Error in main loop: {e}")
            send_telegram(f"❌ <b>Bot Error</b>\n{str(e)[:200]}")
            time.sleep(30)


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    
    command = sys.argv[1].lower()
    
    if command == "scan":
        markets = fetch_reward_markets()
        if markets:
            print_scan_results(markets)
        else:
            print("\n  No eligible markets found. Try adjusting filters in keys.env")
    
    elif command == "run":
        run_bot()
    
    elif command == "dashboard":
        # Just show current open orders (read-only)
        print("\n  Loading positions from Polymarket...")
        try:
            client = get_clob_client()
            orders = get_open_orders(client)
            if orders:
                print(f"\n  Found {len(orders)} open orders")
                # We'd need to match these to markets for a full dashboard
                # For now, just list them
                for o in orders[:20]:
                    print(f"  - {o}")
            else:
                print("  No open orders found.")
        except Exception as e:
            print(f"  ❌ Error: {e}")
            print("  Make sure keys.env is configured. Run 'python bot.py setup' first.")
    
    elif command == "setup":
        run_setup()
    
    else:
        print(f"  Unknown command: {command}")
        print(__doc__)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
