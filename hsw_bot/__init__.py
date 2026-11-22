"""HSW Stock Bot: a small, honest swing trading bot you can read from top to bottom.

The package has one job split into small parts:

* ``config``      reads every setting from ``config.toml``.
* ``data``        downloads daily price bars and checks them.
* ``strategies``  holds the trading ideas (Steady, Swing and your own).
* ``planner``     turns a strategy's ideas into sized orders. The backtest
                  and the live bot both use it, so they can never disagree.
* ``engine``      replays history day by day (the backtest).
* ``stats``       measures results and runs the stress tests.
* ``broker``      talks to Alpaca, or to a fake broker for tests and dry runs.
* ``runner``      one hands off bot run, with safety checks that fail closed.
* ``report``      rebuilds every number, table and chart in the docs.
* ``cli``         the ``hsw-bot`` command.
"""

__version__ = "1.0.0"
