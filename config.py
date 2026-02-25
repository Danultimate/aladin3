"""
Shared configuration constants for the Matchbook trading system.
"""

# Phase thresholds
PHASE1_MIN = 25.0
PHASE1_MAX = 200.0
PHASE2_MIN = 200.0

# Target goal
TARGET_BANKROLL = 5000.0
TARGET_DAILY_ROI = 0.05  # 5%

# Phase 1: Directional Scalping
TICKS_DISCOUNT = 2  # Place Back at best_back + 2 ticks
STAKE_PCT_PHASE1 = 0.03  # 3% of bankroll per Back order
MIN_STAKE = 1.0
MAX_STAKE_PHASE1 = 5.0

# Phase 2: Market Making
STAKE_PCT_PHASE2 = 0.02  # 2% of bankroll per side
MAX_STAKE_PHASE2 = 20.0

# Risk management
STOP_LOSS_PCT = 0.10  # 10% adverse move triggers stop
POLL_INTERVAL_SEC = 45
BALANCE_REFRESH_INTERVAL_SEC = 300  # Re-login every 5 min to refresh balance

# Market filters (empty = trade all)
TAG_URL_NAMES = ""  # e.g. "politics" - discover via API
CATEGORY_IDS = ""  # Optional category filter
