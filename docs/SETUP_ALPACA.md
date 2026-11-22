# Setting up Alpaca

Alpaca is a broker in the USA with an API, which means a program can place orders for you. It charges no commission on stocks and funds, and it has a free paper account: pretend money, real prices. That is where this bot starts.

**Please read this first.** We wrote and tested the Alpaca part of this bot without any Alpaca keys. The tests use a fake broker that behaves like Alpaca, and they check our code against real `alpaca-py` objects. However the first real test of the whole chain is YOUR paper account. Treat the first weeks on paper as that test.

## 1. Make an account and get paper keys

1. Sign up at [alpaca.markets](https://alpaca.markets). The paper account comes with it.
2. Log in and switch to your **Paper** account.
3. Find the API keys panel and generate a new key. You get two strings: a key id and a secret. The secret is shown only once, so copy it now.

Paper keys and live keys are different. Alpaca's paper trading page says so too: "Your paper trading account will have a different API key from your live account."

## 2. Put the keys in `.env`

```bash
cp .env.example .env
```

Open `.env` in a text editor and paste your keys:

```text
ALPACA_API_KEY=your paper key id
ALPACA_SECRET_KEY=your paper secret
ALPACA_PAPER=true
```

Rules for keys, with no exceptions:

- `.env` stays on your computer. Git ignores it, so it never gets uploaded.
- Never paste keys into a chat, an email, a screenshot or a GitHub issue.
- The bot never prints them. If you ever see a key in a log, that is a bug. Tell me.
- If a key leaks, delete it in the Alpaca dashboard and make a new one.

## 3. Your first paper run

Run the bot in the evening, after 6 PM Eastern time. Yahoo needs a little time after the 4 PM close to publish the final daily bar. The evening is also the best time for the daily loss limit. It compares the account now with the account at the previous close. It takes that previous close from Alpaca's daily account history first, then from Alpaca's "previous close" value if its date is right, then from the bot's own run the evening before. It never uses a value from today. If none can be trusted, it makes no new buys that run and asks for a look.

```bash
hsw-bot run
```

It prints one status line, then the orders it sent and what Alpaca answered:

```text
PAPER | account ****1234 | kill switch off | positions: 3 managed, 0 ignored, 0 unmanaged
```

In the evening the answer is usually `accepted`: the order waits in the queue for the next open. That is not a fill yet. The next run checks what really filled.

The bot keeps its records in `state/paper/` (and `state/live/` for live trading). Each file belongs to one account. Do not copy them between accounts; the bot refuses records from another account.

Then look around:

```bash
hsw-bot status                 # equity, positions, open orders, safety switches
```

Then open `logs/journal.csv` in any spreadsheet. It has one row for every decision, order, fill, mismatch and safety stop.

If the bot refuses to run, it tells you why and exits with code 2. Nothing was sent. If it finishes but something needs a look, it exits with code 1 and lists what under "NEEDS A LOOK". The usual messages:

| Message | What to do |
| :--- | :--- |
| The bot's records are missing, but the account holds ... | The records were lost. Rebuild them: `hsw-bot rebuild-state --peak-now` (see below). |
| An older version of the bot left its records in state/bot_state.json | Look at that file and check which account it is from. Move it out of the `state` folder, then run `hsw-bot rebuild-state` with the `--peak` value the message suggests. |
| These records belong to account ****..., but the keys open account ****... | The keys in `.env` belong to another account than the records. Fix the keys. |
| The records hold positions for X, which is not in [bot] sleeves | You removed a strategy that still holds stocks. Put it back with a share of 0 (see below). |
| positions the bot did not open: ... | You hold stocks in this account by hand. List them in `bot.ignore_symbols`. |
| X: extra shares the bot did not buy | You bought more of a stock the bot holds. The bot only ever sells its own shares. |
| The market is open right now | Run it after the close. Orders sent during the day would fill at a different price than the backtest assumes. |
| The newest price bar is from ..., but the last trading day was ... | Yahoo has not published today's bar yet. Try again in an hour. |
| The market opens in ... minutes | Too close to the open. Run it in the evening instead. |
| Could not read the account ... | Check your keys and your internet connection. |
| no good price today for held ... (sells only run) | The data for a stock the bot holds is broken today. The bot leaves that stock alone, still sells the others on their rules, and buys nothing new. Check the stock by hand if it happens again. |
| no account value for the previous close (...) can be trusted | The daily loss limit could not be checked, so no new buys this run. Run the bot in the evening, as above. If it happens every evening, tell me: it means Alpaca changed how it reports the previous close. |
| the account fell ...% since the previous close | The daily loss limit stopped new buys for this run. If you took money out today, that explains it; buys start again tomorrow. |
| the sell of X did not go through (...) | The next run sends it again with a new order id. You can run the bot again the same evening. |
| WARNING: the sell of X has failed N times | That position has no working exit until the sell goes through. If the next try fails too, sell it by hand in the Alpaca dashboard. |
| could not read the deposits and withdrawals | No new buys this run, because the bot cannot tell a withdrawal from a loss. Usually a passing problem at Alpaca; run again later. |
| WARNING: the bot has failed closed N runs in a row | Open positions have no stop protection while the bot is stopped. Fix the cause, or manage them by hand in the Alpaca dashboard. |

## 4. Run it every day by itself

The bot is built to run once per trading day, after the close. Pick a time between 6 PM and midnight Eastern. Your computer must be on (and awake) at that time. A small computer that is always on, like a Raspberry Pi or a cheap cloud server, works well too.

### Linux and Mac: cron

```bash
crontab -e
```

Add one line. This one runs at 6:30 PM, Monday to Friday:

```text
30 18 * * 1-5 cd /full/path/to/hsw-stock-bot && .venv/bin/hsw-bot run >> logs/cron.log 2>&1
```

- cron uses your computer's time zone. If you are not on Eastern time, change `18` so it still runs after 6 PM Eastern. For example, on Pacific time `30 15 * * 1-5` runs at 3:30 PM Pacific, which is 6:30 PM Eastern.
- On a market holiday the newest trading day is the one before. The bot already handled it, so it sends nothing new.
- Check `logs/cron.log` now and then. A non zero exit code means it needs a look.

### Windows

Set up the folder once (in PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e . --no-deps
```

Then open **Task Scheduler** and choose **Create Basic Task**:

1. Name: `HSW Stock Bot`.
2. Trigger: **Weekly**, on Monday to Friday, at a time after 6 PM Eastern in your time zone.
3. Action: **Start a program**.
   - Program: `C:\full\path\to\hsw-stock-bot\.venv\Scripts\hsw-bot.exe`
   - Arguments: `run`
   - Start in: `C:\full\path\to\hsw-stock-bot`
4. Finish. Then open the task's properties and tick **Run whether user is logged on or not**, so it runs while you are away.

### Why there is no GitHub Actions schedule

GitHub can run code on a timer. I left that out on purpose. It would mean storing your keys as repo secrets, and a public repo that trades by itself is one setting away from trouble. The only workflow in this repo runs the tests, and only when code is pushed.

## Make the account unable to borrow

Alpaca accounts can be margin accounts, which means they can borrow. This bot never spends more than the cash it has. However an account that cannot borrow at all is one more safety net.

Alpaca's account configuration API has a setting called `max_margin_multiplier` (PATCH `/v2/account/configurations`; the values are "1", "2" or "4"; checked 2026-11-21). "1" means no borrowing. Set it with:

```bash
hsw-bot no-margin           # paper account; add --live for the live account
```

It sends only that one setting, so nothing else in your account changes. Then it reads every setting back and prints them all, so you can see the result. If the bot ever sees an account that can borrow, it writes a warning in the journal. Alpaca also says that accounts with less than $2,000 are "restricted to 1x buying power" (Margin and short selling page, updated 2026-09-17).

## If the records are lost

The records (`state/paper/bot_state.json`) say which strategy bought each stock and when. If the file is deleted or broken while the account holds stocks, the bot refuses to trade. Rebuild it from Alpaca's order history:

```bash
hsw-bot rebuild-state --peak-now               # best level = the account today
hsw-bot rebuild-state --peak 10500             # best level = a value you choose
hsw-bot rebuild-state --replace --peak-now     # over a broken file (the old one is kept)
hsw-bot rebuild-state --fresh-start --peak-now # treat every stock in the account as yours
```

Every order the bot sends has an id like `hsw-swing-20261120-AAPL-buy`: the strategy, the decision day, the stock and the side. Even so the rebuild is careful, because you may have traded the same stocks by hand:

- It replays every fill in each stock you hold, yours and the bot's, in time order. After each fill it caps the bot's shares at what the account really held, so shares you bought later are never the bot's.
- If you traded a stock by hand after the bot's last fill in it, the rebuild does not claim that stock at all. It cannot know whose shares you traded.
- It prints a table: each stock, the shares at Alpaca, the shares it gives the bot, and why. Stocks it did not claim are yours to decide: add them to `bot.ignore_symbols`, or close them by hand.

The best account level for the drawdown limit is your choice, because the rebuild cannot tell growth from deposits. `--peak-now` starts it from today. `--peak` takes a value, for example the one from an old records file. The rebuild also prints the highest daily account value since the bot's first fill, as information only.

## Deposits and withdrawals

You may add money to the account or take some out. Each run reads Alpaca's list of deposits, withdrawals and transfers, and moves the best level by the same factor as the account. So moving money never counts as a gain or a loss for the drawdown limit. Each one counts once.

- If that list cannot be read, the run makes no new buys and asks for a look.
- On the evening of a withdrawal bigger than 3% of the account, the daily loss limit sees a "fall" and stops new buys once. That is expected.
- Shares moved in or out (a transfer from another broker, for example) have no reliable dollar value in that list. The bot asks for a look instead of guessing.
- To set a new best level by hand: `hsw-bot reset-peak` (today's value) or `hsw-bot reset-peak --peak 9800`. Add `--live` for the live account. The change goes into the journal.

## Stocks you hold by hand

You can keep your own stocks in the same account. List them in `config.toml`:

```toml
ignore_symbols = ["AAPL", "MSFT"]
```

The bot never buys, sells or warns about them. A stock you hold that is NOT on the list makes every run ask for a look, until you add it. If you buy more of a stock the bot already holds, the bot sells only its own shares.

## Stopping and winding down

- **Stop new buys:** create a file named `stop` in the bot folder. Any case and any extension works: `STOP`, `stop.txt`, `Stop.md`. The bot still sells on its normal rules. Delete the file to start buying again.
- **Wind one strategy down:** set its share to 0 in `config.toml`, for example `sleeves = { steady = 0.5, swing = 0.0 }`. It then only sells. Do not delete its line while it still holds stocks; the bot refuses to start, because those stocks would have no exits.
- **Sell everything now:** first create the `stop` file, so the bot buys nothing new. Then sell by hand in the Alpaca dashboard. The next run sees that the positions are gone, drops them from its records, and says so in the journal.

## 5. How the orders work, and why

These limits come from Alpaca's own documentation. I read it on 2026-11-21. The rules can change, so check the current pages before you change how the bot sends orders.

- **Fractional shares** work for market, limit, stop and stop limit orders, with time in force **DAY** only. GTC, OPG, CLS, IOC and FOK are not allowed for fractional orders. (Alpaca: "Fractional trading", updated 2026-10-28, and "Placing orders", updated 2026-10-13.)
- An order can give a share count (`qty`) or a dollar amount (`notional`), never both. Dollar amounts only work for market DAY orders. (Alpaca API reference, POST /v2/orders, updated 2026-05-27.)
- A DAY order "submitted after the close ... is queued and submitted the following trading day". And "any market orders received before 9:28 will be filled at the Nasdaq Official Opening Price". (Alpaca: "Placing orders".)
- Bracket, OCO and trailing stop orders are not listed as supported for fractional quantities.

That is why the bot works the way it does:

1. It decides after the close, from the daily bar, exactly like the backtest.
2. It sends **market DAY** orders. Buys are dollar amounts. Sells are the full share count you hold.
3. Those orders wait in Alpaca's queue and fill at the next open, which is the price the backtest uses.
4. There is no stop order resting at Alpaca during the day. A day stop would have to be sent again every morning, and it would behave differently from the backtest. Instead the stop is checked on each close, and the sell goes out for the next open.

The price of that choice: if a stock gaps far below its stop overnight, the loss is bigger than planned. The backtest includes those gaps, because it sells at the next open too.

## Going live

Do not go live until all of this is true:

- The bot has run on paper for several weeks without a surprise.
- You have read the journal and understood every decision.
- What happened on paper looks like what the backtest says should have happened.
- You have read the [results](RESULTS.md) and you accept the worst drop shown there, and worse.

Then, and only then:

1. Make LIVE keys in the Alpaca dashboard. Put them in `.env`.
2. Set `ALPACA_PAPER=false` in `.env`.
3. Run with the `--live` flag: `hsw-bot run --live`.

Both switches are needed. With only one of them the bot refuses to start. That is on purpose: one typo must never move real money. Start with an amount you can afford to lose. Leave the loss limits in `config.toml` on.

To stop everything at once, create an empty file called `STOP` in the repo folder. The bot then makes no new buys. You can always sell by hand in the Alpaca dashboard.

## What paper trading does not show you

From Alpaca's paper trading page (updated 2026-07-07): paper orders fill against live quotes, about 10% of orders get a random partial fill, and the paper account does not model market impact, slippage from delays, or dividends. Paper only accounts also get prices from one exchange (IEX). So paper results will differ from real ones. That is fine. Paper trading is a test of the machinery, not a forecast.
