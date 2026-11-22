# Make your own strategy

This guide takes you from an idea to a paper trading strategy, step by step. Follow the steps in order. Each one protects you from a mistake that looks harmless and costs money later.

You will touch four files:

| File | What you do there |
| :--- | :--- |
| `hsw_bot/strategies/your_idea.py` | the rules |
| `hsw_bot/strategies/__init__.py` | one line, so everything knows about it |
| `config.toml` | the settings |
| `tests/synthetic.py` | one line, so the guard tests check it |

## Step 1. Write the rule in plain words

Before any code, write two or three sentences:

> "Buy when a stock closes at its highest close in 50 days while it is above its 100 day average. Sell when it closes below its 50 day average, or after 40 trading days."

That is the example that ships in `hsw_bot/strategies/my_strategy.py`. If you cannot write your idea this simply, it is not ready yet.

Pick the numbers now, from common sense or common practice. Write them down. You will not change them after you see a backtest. (Step 8 explains why.)

## Step 2. Copy the template

```bash
cp hsw_bot/strategies/my_strategy.py hsw_bot/strategies/your_idea.py
```

In the new file, rename three things:

```python
@dataclass(frozen=True)
class YourIdeaParams:  # was MyStrategyParams
    ...


class YourIdea(Strategy):  # was MyStrategy
    name = "your_idea"  # must match [strategies.your_idea] in config.toml
    title = "Your idea: one line about it"
    Params = YourIdeaParams
    params: YourIdeaParams
```

Use only the letters a to z, digits and `_` in the name, like `your_idea`. No spaces and no dashes: the name goes into every order id the bot sends (`hsw-your_idea-20261120-AAPL-buy`), and a dash would split it. The bot refuses other names when it reads `config.toml`, and says why.

## Step 3. Settings go in the Params class

Every number your rules use is a field here, with a type:

```python
@dataclass(frozen=True)
class YourIdeaParams:
    trend_days: int
    breakout_days: int
    stop_atr: float
```

The settings reader checks each value's type when it loads `config.toml`. It also refuses unknown names, so a typo like `stop_atrr` stops the program instead of being ignored. Add a `__post_init__` that refuses values that make no sense, with a message that says how to fix them.

## Step 4. The three methods

A strategy answers three questions every evening. The backtest and the live bot both call these same three methods.

### `indicators(bars)`: what numbers do I need?

`bars.close` is a table: one row per day, one column per symbol. Compute every number for every day and every symbol at once, and return them in a dict:

```python
def indicators(self, bars: Bars) -> dict[str, pd.DataFrame]:
    close = bars.close
    return {
        "trend": close.rolling(self.params.trend_days).mean(),
        "breakout": close > close.shift(1).rolling(self.params.breakout_days).max(),
    }
```

**The one rule you must never break: no peeking.** A value on day t may only use bars from day t and earlier.

- `.rolling(n)` looks back. Safe.
- `.shift(1)` moves yesterday's value to today. Safe.
- `.shift(-1)` moves TOMORROW's value to today. Never use it.

If you break this rule by accident, the no lookahead guard test fails. That is what it is for.

### `exits(day, positions)`: what do I sell at the next open?

```python
def exits(self, day: Day, positions: Mapping[str, Position]) -> list[Exit]:
    exits = []
    for symbol, position in positions.items():
        close = day.value("close", symbol)
        if not math.isfinite(close):
            continue  # no price today: decide tomorrow
        if close < day.value("trend", symbol):
            exits.append(Exit(symbol, "close fell below the trend line"))
    return exits
```

The `day` object only shows you today and the past:

| Call | What it gives you |
| :--- | :--- |
| `day.today("close")` | today's value for every symbol |
| `day.value("trend", "AAPL")` | today's value for one symbol (NaN when there is none) |
| `day.on("trend", position.signal_date, "AAPL")` | a value from an earlier day; a later day raises an error |
| `day.bars_since(position.entry_date)` | trading days held, counting the entry day |

Each `Position` knows its `qty`, `entry_price`, `entry_date` (the day the buy filled) and `signal_date` (the evening you decided to buy).

### `entries(day, positions)`: what do I buy at the next open, best first?

```python
def entries(self, day: Day, positions: Mapping[str, Position]) -> list[Entry]:
    wanted = []
    for symbol, triggered in day.today("breakout").items():
        if bool(triggered) and symbol not in positions:
            stop = self.params.stop_atr * day.value("atr", str(symbol))
            wanted.append(Entry(str(symbol), score=..., reason="breakout", stop_distance=stop))
    return sort_entries(wanted)
```

- `score`: higher is better. When there are more wishes than free slots, the best scores win.
- `stop_distance`: dollars per share between today's close and your stop. The planner sizes the trade so that hitting the stop costs about `risk_per_trade` of the account.
- or `weight`: a share of the account, for example `weight=0.25`.

You never decide how many shares to buy. The planner does that the same way for every strategy, and it never puts more than `max_position_pct` of the account into one stock.

## Step 5. Register it

In `hsw_bot/strategies/__init__.py`, import your class and add it to `STRATEGIES`:

```python
from hsw_bot.strategies.your_idea import YourIdea

STRATEGIES = {cls.name: cls for cls in (Steady, Swing, MyStrategy, YourIdea)}
```

## Step 6. Add your settings to `config.toml`

```toml
[strategies.your_idea]
universe = "sp100"            # a list from [universes]
trend_days = 100
breakout_days = 50
stop_atr = 2.0
max_positions = 5             # these three are for the planner
max_position_pct = 0.20
risk_per_trade = 0.01
```

## Step 7. Run the guard tests

Open `tests/synthetic.py`. Add a small builder for your strategy and its sizing rules to `STRATEGY_BUILDERS` and `STRATEGY_RULES`. The made up market there is about 800 days long, so use shorter windows (for example 60 days instead of 200) if your strategy would otherwise never trade.

```bash
pytest
```

Every guard now runs on your strategy too: no NaN, costs, no lookahead, reconciliation. A test called `test_every_registered_strategy_is_guarded` fails until you add it, as a reminder.

## Step 8. Backtest it, honestly

```bash
hsw-bot backtest --strategy your_idea
hsw-bot backtest --strategy your_idea --cost-mult 2
```

Look at three things before anything else:

1. Does it beat SPY buy and hold, AND an equal weight portfolio of its own list?
2. Does it still make money with double costs?
3. Is its worst drop something you could live through without selling?

Now the hard part. **Do not tune the numbers to make this look better.** If you try ten versions and keep the best one, you have not found a better strategy. You have found the version that fit the past best, by luck. It will very likely do worse from now on. If you must try variants, decide before you look how you will judge them, and judge the winner on years you did not use to pick it.

## Step 9. The full stress test

Add your strategy's name to `[report] strategies` in `config.toml`, then:

```bash
hsw-bot report
```

[RESULTS.md](RESULTS.md) now has your strategy's full tables: both cost levels, in sample against out of sample (from 2015), recent windows, year by year, the worst drops, and the random entry test. Read [How we trade](HOW_WE_TRADE.md) for what each one means. A p value well above 0.05 means random entries did about as well as your rules.

## Step 10. Paper trade it

Give it a share of the account in `config.toml`:

```toml
[bot]
sleeves = { steady = 0.5, your_idea = 0.5 }
```

Two strategies may not trade the same symbol in one account; the bot refuses to start if they do. Then run on paper for several weeks (see [SETUP_ALPACA.md](SETUP_ALPACA.md)). Read the journal. Compare what happened with what the backtest says.

## Common mistakes

| Mistake | What happens | The fix |
| :--- | :--- | :--- |
| `.shift(-1)` or any future data | Amazing backtest, real losses | The no lookahead guard catches it |
| Ignoring missing prices | NaN spreads into sizes and equity | Check `math.isfinite(...)` before using a value |
| Many settings, tuned to the past | A strategy that fits history and nothing else | Few settings, chosen before the first test |
| Judging only against SPY | A rising list makes any rule look smart | Compare with the "same list" baseline too |
| Skipping paper trading | Surprises with real money | Weeks on paper, journal read every day |
