"""
Headless Matchbook trading bot.
Runs Phase 1 (Directional Scalping) or Phase 2 (Market Making) based on bankroll.
Heavy inline comments for formulas and API logic.
"""

import logging
import time
from datetime import datetime
from typing import Optional

from matchbook_api import (
    MatchbookAPIError,
    MatchbookClient,
    add_ticks_to_odds,
    greening_up_lay_stake,
    lay_liability,
)
import db
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def get_phase(balance: float) -> int:
    """
    Determine active phase from bankroll.
    Phase 1: £25 <= balance < £200 (Back-only scalping)
    Phase 2: balance >= £200 (Market making both sides)
    """
    if config.PHASE1_MIN <= balance < config.PHASE1_MAX:
        return 1
    if balance >= config.PHASE2_MIN:
        return 2
    return 1  # Below £25 still use Phase 1 logic (conservative)


def get_stake(balance: float, phase: int) -> float:
    """Calculate stake size based on phase and bankroll."""
    if phase == 1:
        stake = balance * config.STAKE_PCT_PHASE1
        stake = max(config.MIN_STAKE, min(stake, config.MAX_STAKE_PHASE1))
    else:
        stake = balance * config.STAKE_PCT_PHASE2
        stake = max(config.MIN_STAKE, min(stake, config.MAX_STAKE_PHASE2))
    return round(stake, 2)


def get_best_prices(runner: dict) -> tuple[Optional[float], Optional[float]]:
    """
    Extract best Back and Lay prices from a runner's prices list.
    Returns (best_back, best_lay).
    """
    best_back = None
    best_lay = None
    for p in runner.get("prices", []):
        side = p.get("side", "").lower()
        odds = p.get("decimal-odds") or p.get("odds")
        if not odds:
            continue
        if side == "back":
            if best_back is None or odds > best_back:
                best_back = odds
        elif side == "lay":
            if best_lay is None or odds < best_lay:
                best_lay = odds
    return best_back, best_lay


def run_phase1(client: MatchbookClient, balance: float) -> None:
    """
    Phase 1: Directional Scalping ("Buy the Dip").
    - Place Back orders at discount (best_back + 2 ticks)
    - When Back is matched, immediately place Lay to green up
    - Formula 1: Lay_Stake = (Back_Stake * Back_Odds) / Lay_Odds
    """
    # Pre-check: need funds to trade. Deposit at least £25 to start.
    if balance < config.MIN_STAKE:
        logger.warning(
            "Insufficient funds (balance=£%.2f). Deposit at least £%.0f to start trading.",
            balance,
            config.PHASE1_MIN,
        )
        return

    stake = get_stake(balance, 1)
    account = client.get_account()
    free_funds = float(account.get("free-funds", 0) or 0)

    # For Back orders we need free_funds >= stake
    can_place_back = free_funds >= stake

    # Fetch events with prices (optionally filtered to single event)
    event_id_filter = db.get_event_id()
    try:
        events_data = client.get_events(
            include_prices=True,
            price_depth=3,
            states="open,suspended",
            per_page=10,
            ids=event_id_filter,
        )
    except MatchbookAPIError as e:
        logger.error("Failed to fetch events: %s", e)
        return

    events = events_data.get("events", [])
    if not events:
        logger.debug("No open events")
        return

    # Check existing offers - if any Back is matched, place greening Lay
    try:
        offers_data = client.get_offers(status="matched")
    except MatchbookAPIError as e:
        logger.error("Failed to fetch offers: %s", e)
        return

    for offer in offers_data.get("offers", []):
        if offer.get("side") != "back" or offer.get("status") != "matched":
            continue
        # We have a matched Back - need to green up with Lay
        runner_id = offer.get("runner-id")
        back_odds = offer.get("decimal-odds") or offer.get("odds", 0)
        back_stake = offer.get("stake", 0)
        event_id = offer.get("event-id")
        market_id = offer.get("market-id")

        # Find current best Lay price for this runner
        best_lay = None
        for ev in events:
            if ev.get("id") != event_id:
                continue
            for mkt in ev.get("markets", []):
                if mkt.get("id") != market_id:
                    continue
                for r in mkt.get("runners", []):
                    if r.get("id") == runner_id:
                        _, best_lay = get_best_prices(r)
                        break

        if best_lay is None or best_lay <= 0:
            logger.warning("No Lay price for runner %s, skipping green up", runner_id)
            continue

        # Formula 1: Lay_Stake = (Back_Stake * Back_Odds) / Lay_Odds
        lay_stake = greening_up_lay_stake(back_stake, back_odds, best_lay)
        if lay_stake < 0.5:
            continue

        # Lay liability check: need free_funds >= Lay_Stake * (Lay_Odds - 1)
        liability = lay_liability(lay_stake, best_lay)
        if free_funds < liability:
            logger.warning(
                "Insufficient funds for green-up Lay (need £%.2f, have £%.2f). Deposit funds.",
                liability,
                free_funds,
            )
            continue

        try:
            result = client.submit_offers(
                offers=[
                    {
                        "runner-id": runner_id,
                        "side": "lay",
                        "odds": best_lay,
                        "stake": round(lay_stake, 2),
                        "keep-in-play": False,
                    }
                ]
            )
            for o in result.get("offers", []):
                if o.get("status") == "matched":
                    logger.info("Greened up: Lay %.2f @ %.2f on runner %s", lay_stake, best_lay, runner_id)
                    db.record_trade(
                        event_id=event_id,
                        market_id=market_id,
                        runner_id=runner_id,
                        side="lay",
                        odds=best_lay,
                        stake=lay_stake,
                        matched_at=datetime.utcnow().isoformat(),
                        profit=None,
                        phase=1,
                    )
                elif o.get("status") == "failed":
                    logger.warning("Green up Lay failed: %s", o)
        except MatchbookAPIError as e:
            logger.error("Green up failed: %s", e)

    # Place new Back orders at discount (best_back + ticks)
    # Limit to one new Back per cycle to avoid over-exposure
    if not can_place_back:
        logger.warning(
            "Insufficient free funds (£%.2f) for stake £%.2f. Deposit funds to place new Back orders.",
            free_funds,
            stake,
        )
        return

    placed_this_cycle = False
    try:
        open_offers = client.get_offers(status="open")
        if len(open_offers.get("offers", [])) >= 2:
            logger.debug("Already have open offers, skipping new Back")
            return
    except MatchbookAPIError:
        pass

    for ev in events:
        if placed_this_cycle:
            break
        for mkt in ev.get("markets", []):
            if placed_this_cycle:
                break
            for r in mkt.get("runners", []):
                best_back, best_lay = get_best_prices(r)
                if best_back is None or best_lay is None:
                    continue
                # Avoid very short odds
                if best_back < 1.5 or best_back > 10.0:
                    continue

                # Place Back at discount: best_back + TICKS_DISCOUNT ticks
                back_odds = add_ticks_to_odds(best_back, config.TICKS_DISCOUNT, side="back")
                if back_odds <= best_back:
                    back_odds = add_ticks_to_odds(best_back, 1, side="back")

                try:
                    result = client.submit_offers(
                        offers=[
                            {
                                "runner-id": r["id"],
                                "side": "back",
                                "odds": back_odds,
                                "stake": stake,
                                "keep-in-play": False,
                            }
                        ]
                    )
                    for o in result.get("offers", []):
                        status = o.get("status")
                        if status in ("open", "matched", "delayed"):
                            logger.info(
                                "Phase 1 Back: %s @ %.2f stake %.2f (runner %s) - status %s",
                                "back",
                                back_odds,
                                stake,
                                r.get("name"),
                                status,
                            )
                            placed_this_cycle = True
                            break
                        elif status == "failed":
                            logger.warning("Back offer failed: %s", o)
                except MatchbookAPIError as e:
                    logger.error("Submit Back failed: %s", e)
                if placed_this_cycle:
                    break


def run_phase2(client: MatchbookClient, balance: float) -> None:
    """
    Phase 2: Market Making ("Trading the Spread").
    - Place Back at best Back, Lay at best Lay (edges of spread)
    - Formula 2: Lay_Liability = Lay_Stake * (Lay_Odds - 1)
    - Must verify free_funds >= Lay_Liability before placing Lay
    - If one side fills, cancel/adjust the other
    """
    # Pre-check: need sufficient funds for Phase 2 (Back + Lay liability)
    if balance < config.PHASE2_MIN:
        logger.warning(
            "Insufficient balance (£%.2f) for Phase 2. Need £%.0f+.",
            balance,
            config.PHASE2_MIN,
        )
        return

    stake = get_stake(balance, 2)
    account = client.get_account()
    free_funds = float(account.get("free-funds", 0) or 0)

    # Fetch events (optionally filtered to single event)
    event_id_filter = db.get_event_id()
    try:
        events_data = client.get_events(
            include_prices=True,
            price_depth=3,
            states="open,suspended",
            per_page=10,
            ids=event_id_filter,
        )
    except MatchbookAPIError as e:
        logger.error("Failed to fetch events: %s", e)
        return

    events = events_data.get("events", [])
    if not events:
        return

    # Check existing offers - if one side filled, cancel the other
    try:
        offers_data = client.get_offers(status="open,matched")
    except MatchbookAPIError as e:
        logger.error("Failed to fetch offers: %s", e)
        return

    offers = offers_data.get("offers", [])
    matched_offer_ids = [o["id"] for o in offers if o.get("status") == "matched"]
    open_offer_ids = [o["id"] for o in offers if o.get("status") == "open"]

    # If we have a matched offer, cancel any open counterpart
    if matched_offer_ids and open_offer_ids:
        try:
            client.cancel_offers(offer_ids=open_offer_ids)
            logger.info("Cancelled open offers after match: %s", open_offer_ids)
        except MatchbookAPIError as e:
            logger.error("Cancel failed: %s", e)

    # Don't place new two-sided quotes if we already have open offers
    if open_offer_ids or len(matched_offer_ids) >= 2:
        return

    # Find a market with a wide enough spread
    for ev in events:
        for mkt in ev.get("markets", []):
            for r in mkt.get("runners", []):
                best_back, best_lay = get_best_prices(r)
                if best_back is None or best_lay is None:
                    continue
                spread = best_lay - best_back
                if spread < 0.02:  # Need some spread to profit
                    continue
                if best_back < 1.2 or best_lay > 15.0:
                    continue

                # Formula 2: Lay_Liability = Lay_Stake * (Lay_Odds - 1)
                liability = lay_liability(stake, best_lay)
                if free_funds < liability:
                    logger.warning(
                        "Insufficient funds for Lay: need %.2f, have %.2f",
                        liability,
                        free_funds,
                    )
                    continue

                # Place both Back and Lay
                try:
                    result = client.submit_offers(
                        offers=[
                            {
                                "runner-id": r["id"],
                                "side": "back",
                                "odds": best_back,
                                "stake": stake,
                                "keep-in-play": False,
                            },
                            {
                                "runner-id": r["id"],
                                "side": "lay",
                                "odds": best_lay,
                                "stake": stake,
                                "keep-in-play": False,
                            },
                        ]
                    )
                    for o in result.get("offers", []):
                        logger.info(
                            "Phase 2: %s @ %.2f stake %.2f (runner %s) - status %s",
                            o.get("side"),
                            o.get("decimal-odds", o.get("odds")),
                            o.get("stake"),
                            r.get("name"),
                            o.get("status"),
                        )
                    return  # One market per cycle
                except MatchbookAPIError as e:
                    logger.error("Phase 2 submit failed: %s", e)
                    return


def main() -> None:
    """Main bot loop."""
    logger.info("Starting Matchbook trading bot")
    db.init_db()

    client = MatchbookClient()
    try:
        client.login()
    except MatchbookAPIError as e:
        logger.error("Login failed: %s", e)
        return

    last_balance_refresh = 0.0

    while True:
        try:
            now = time.time()
            if now - last_balance_refresh > config.BALANCE_REFRESH_INTERVAL_SEC:
                try:
                    client.refresh_account()
                    last_balance_refresh = now
                except MatchbookAPIError as e:
                    logger.warning("Balance refresh failed: %s", e)

            account = client.get_account()
            balance = float(account.get("balance", 0) or 0)
            exposure = float(account.get("exposure", 0) or 0)

            phase = get_phase(balance)

            # Daily ROI: (balance - start_of_day) / start_of_day
            daily_start = db.get_daily_start_balance()
            if daily_start and daily_start > 0:
                daily_roi = (balance - daily_start) / daily_start
            else:
                db.update_daily_start(datetime.utcnow().strftime("%Y-%m-%d"), balance)
                daily_roi = 0.0

            db.record_bankroll_snapshot(balance, phase, daily_roi)

            logger.info(
                "Balance=£%.2f Exposure=£%.2f Phase=%s DailyROI=%.2f%% Trading=%s",
                balance,
                exposure,
                phase,
                daily_roi * 100,
                "ON" if db.is_trading_enabled() else "OFF",
            )

            # Only place orders when trading is enabled via dashboard
            # Phase: respect Force Phase 1 setting, else use balance-based phase
            if db.is_trading_enabled():
                effective_phase = 1 if db.is_force_phase1() else phase
                if effective_phase == 1:
                    run_phase1(client, balance)
                else:
                    run_phase2(client, balance)
            else:
                logger.debug("Trading disabled - skipping order placement")

        except MatchbookAPIError as e:
            logger.error("Cycle error: %s", e)
        except Exception as e:
            logger.exception("Unexpected error: %s", e)

        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
