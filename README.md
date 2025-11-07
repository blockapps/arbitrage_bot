# Multi-Pool Arbitrage Bot

A Python-based arbitrage bot for detecting and executing price differences between AMM pools and external markets on the Strato blockchain. Supports multiple pools simultaneously.

## Project Structure

```
arbitrage_bot/
├── core/              # Core utilities
│   ├── strato_client.py # Strato blockchain interaction
│   ├── oauth_client.py  # OAuth authentication
│   ├── constants.py     # Configuration constants (WEI_SCALE, BPS_DENOM, USDST_ADDRESS)
│   └── math_utils.py    # Mathematical calculations (wei-based, closed-form optimal input)
├── onchain/           # Blockchain contracts
│   ├── token.py       # Token wrapper
│   └── pool.py        # AMM pool wrapper (includes position tracking)
├── market/            # Market data
│   └── oracle.py      # Price oracle (Alchemy API)
├── engine/            # Arbitrage logic
│   ├── arb_executor.py # Opportunity detection and trade execution
│   └── helpers.py     # Helper functions (approvals, gas checks, PnL checks, profit tracking)
├── config.yaml        # Configuration (multi-pool support)
├── main.py            # Main application
├── requirements.txt   # Python dependencies
└── README.md          # Documentation
```

## Installation

1. Create and activate virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the project root (see `.env.example` for template):
   ```bash
   # Strato Blockchain Configuration
   STRATO_NODE_URL=https://your-strato-node-url.com
   USERNAME=your_username
   PASSWORD=your_password

   # OAuth Configuration
   OAUTH_CLIENT_ID=your_oauth_client_id
   OAUTH_CLIENT_SECRET=your_oauth_client_secret
   OAUTH_DISCOVERY_URL=https://your-oauth-discovery-url.com

   # Alchemy API Key for real-time token prices (REQUIRED)
   ALCHEMY_API_KEY=your_alchemy_api_key_here
   ```

   **Getting an Alchemy API Key:**
   - Sign up for free at https://www.alchemy.com/
   - Create a new app for "Ethereum" → "Mainnet"
   - Copy your API key from the dashboard
   - The oracle fetches real-time token prices (ETH, BTC, etc.) from Alchemy's Price API
   
   **Note:** 
   - Make sure `.env` is in `.gitignore` to avoid committing sensitive credentials.
   - `ALCHEMY_API_KEY` is required for the bot to fetch real market prices
   - Alchemy's non-enterprise API supports one symbol per call, so the bot makes individual requests for each token

4. Edit `config.yaml`:
   ```yaml
   # Configure multiple pools
   pools:
     - address: "0000000000000000000000000000000000001017"
       external_token_name: "ETH"  # Token name for Alchemy price lookup
     - address: "0000000000000000000000000000000000001019"
       external_token_name: "BTC"
   
   # Trading Parameters  
   trading:
     fee_bps: 30  # Pool fee in basis points (0.3%)
     min_profit: 0.01  # $0.01 minimum profit (in USDST, wei-scaled)
   
   # Oracle Configuration  
   oracle:
     timeout: 10  # API request timeout in seconds
   
   # Execution Settings
   execution:
     execution_interval: 60  # seconds between scans
   ```

## Usage

### Dry Run Mode (Default - Safe for Testing)
The bot runs in dry-run mode by default, which means it will scan for opportunities and log what it would do, but will not execute any trades.

```bash
python main.py
```

### Live Trading (BE CAREFUL!)
To execute actual trades, use the `--live` flag:

```bash
# Set all required environment variables (see Installation section)
python main.py --live
```

### Configuration File
You can specify a custom config file:

```bash
python main.py --config custom_config.yaml
```

## How It Works

1. **Multi-Pool Scanning**: The bot scans all configured pools sequentially, checking each for arbitrage opportunities.

2. **Price Discovery**: For each pool, the bot:
   - Fetches the current pool price (from on-chain reserves)
   - Fetches the oracle price (from Alchemy's Price API using the pool's `external_token_name`)
   - Compares the two prices to detect arbitrage opportunities

3. **Opportunity Detection**: When a price difference is detected, the bot:
   - Calculates the optimal trade size using a closed-form solution that maximizes profit
   - Automatically determines the most profitable direction (buy or sell)
   - Validates that the profit meets the minimum threshold

4. **Gas Management**: The bot automatically:
   - Reserves 0.01 USDST (or checks for vouchers ≥ 1 voucher = 1e18 wei) for gas fees
   - Adjusts token balances to account for gas reserves before calculating trade sizes
   - If trading USDST, the balance is reduced by 0.01 USDST to ensure gas availability

5. **Position Tracking**: For sell opportunities, the bot:
   - Checks the weighted-average cost basis from historical trades
   - Ensures the trade would be profitable (avoids selling at a loss)
   - Uses Cirrus search API to query historical swap data

6. **Execution**: If an opportunity meets all criteria:
   - Profit threshold met
   - Gas availability confirmed
   - Cost basis check passed (for sells)
   - The bot executes the trade and updates cumulative profit tracking

7. **Profit Tracking**: After each successful trade, the bot:
   - Updates cumulative profit in `profit.json`
   - Uses thread-safe file locking to prevent race conditions
   - Tracks both wei-scaled and USD values

## Features

- **Multi-Pool Support**: Monitor and trade across multiple pools simultaneously
- **Automatic Direction Selection**: Automatically determines whether to buy or sell based on which direction is more profitable
- **Closed-Form Optimal Input**: Uses mathematical optimization to calculate the exact trade size that maximizes profit
- **Gas-Aware Trading**: Automatically reserves gas fees and adjusts balances accordingly
- **Cost Basis Protection**: Prevents selling at a loss by tracking weighted-average cost basis
- **Thread-Safe Execution**: Uses locks to prevent concurrent execution
- **Dry-Run Mode**: Safe testing mode enabled by default
- **Cumulative Profit Tracking**: Tracks total profit across all trades in `profit.json`
- **Detailed Logging**: Comprehensive logging with reasons for each decision

## Configuration

### Pool Configuration
Each pool in the `pools` array requires:
- `address`: The pool contract address on Strato
- `external_token_name`: The token symbol for Alchemy price lookup (e.g., "ETH", "BTC")

### Trading Parameters
- `fee_bps`: Pool fee in basis points (e.g., 30 = 0.3%)
- `min_profit`: Minimum profit threshold in USD (e.g., 0.01 = $0.01)

### Execution Settings
- `execution_interval`: Seconds between scans (default: 60)

## Output Format

The bot provides detailed logging for each pool scan:

```
2025-11-07 14:14:19,004 - INFO - Pool price: 3418.012526 USDST per ETHST
2025-11-07 14:14:19,004 - INFO - Oracle price: 3422.734977 USDST per ETHST
2025-11-07 14:14:19,004 - INFO - Price diff: 4.722451 USDST (0.13%)
2025-11-07 14:14:19,004 - INFO - No arbitrage opportunity found - Profit too low for Y->X (profit=-0.000177, min_profit=0.01)

2025-11-07 14:14:19,004 - INFO - Pool price: 102541.701361 USDST per BTCST
2025-11-07 14:14:19,004 - INFO - Oracle price: 102489.875571 USDST per BTCST
2025-11-07 14:14:19,004 - INFO - Price diff: -51.825789 USDST (-0.06%)
2025-11-07 14:14:19,004 - INFO - No arbitrage opportunity found - No input available for X->Y (dx_opt=32959367722980, balance_x=0)
```

## Files Generated

- `profit.json`: Cumulative profit tracking file (created automatically)
  ```json
  {
    "cumulative_profit_wei": 10000000000000000,
    "cumulative_profit_usd": 0.01
  }
  ```

## Disclaimer

This software is for educational purposes only. Trading cryptocurrencies involves substantial risk of loss. Use at your own risk and never trade with more than you can afford to lose.
