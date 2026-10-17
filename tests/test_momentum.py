from datetime import date, timedelta

import numpy as np

from upstocks import momentum


def _row(col, rank, above=True):
    return {"col": col, "rank": rank, "above_sma": above}


def test_decide_matches_the_worked_example():
    A, B, C, D, E, F, G, H = range(8)
    # The example's buy walk: #1 = A and #2 = E are held, #4 = F is below its average.
    ranking = [_row(A, 1), _row(E, 2), _row(B, 3), _row(F, 4, above=False), _row(G, 5), _row(H, 7),
               _row(D, 12, above=False), _row(C, 25)]
    sells, keep, buys = momentum.decide(ranking, [A, B, C, D, E], slots=5, exit_rank=20)
    assert {s["col"] for s in sells} == {C, D}
    assert sorted(keep) == [A, B, E]
    assert [b["col"] for b in buys] == [G, H]


def test_decide_sells_stocks_that_left_the_universe():
    sells, keep, buys = momentum.decide([_row(1, 1)], [7], slots=1, exit_rank=20)
    assert sells[0]["reason"] == "Left the universe" and keep == [] and [b["col"] for b in buys] == [1]


def _market(days=700, n=6, seed=1):
    rng = np.random.default_rng(seed)
    start = date(2020, 1, 1)
    dates = []
    d = start
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    drift = np.linspace(-0.001, 0.003, n)
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, (days, n)), axis=0))
    open_ = close * (1 + rng.normal(0, 0.002, (days, n)))
    volume = np.full((days, n), 1e5)
    return dates, [f"S{i}" for i in range(n)], open_, close, volume


def test_run_accounts_for_cash_costs_and_whole_shares():
    dates, symbols, open_, close, volume = _market()
    st = momentum.Settings(start=dates[300], end=dates[-1], capital=100_000, slots=2, universe=6, exit_rank=3)
    res = momentum.run(dates, symbols, open_, close, volume, st)
    assert res["months"], "expected month-end decisions"
    first = next(m for m in res["months"] if m.get("bought"))
    assert len([b for b in first["bought"] if "qty" in b]) == 2
    for b in first["bought"]:
        assert isinstance(b["qty"], int) and b["amount"] <= 50_000
    # Final value = cash + open holdings at the last close.
    assert abs(res["stats"]["final"] - (res["cash"] + sum(h["value"] for h in res["holdings"]))) < 0.05
    assert res["cash"] >= 0 and res["stats"]["costs"] > 0
    assert len(res["holdings"]) <= 2
    assert all(t["entry_time"] == "09:15:00" and t["exit_time"] == "09:15:00" for t in res["trades"])
    assert first["executed"].endswith(" 09:15:00") and first["bought"][0]["at"].endswith(" 09:15:00")
    ranking = res["latest"]["ranking"]
    assert [r["rank"] for r in ranking] == list(range(1, 7))
    assert all(a["score"] >= b["score"] for a, b in zip(ranking, ranking[1:]))
    assert ranking[-1]["symbol"] == "S0"  # the falling stock ranks last


def test_nothing_is_bought_without_a_year_of_history():
    dates, symbols, open_, close, volume = _market(days=200)
    st = momentum.Settings(start=dates[0], end=dates[-1], capital=100_000, slots=2, universe=6)
    res = momentum.run(dates, symbols, open_, close, volume, st)
    assert res["trades"] == [] and res["holdings"] == [] and res["stats"]["final"] == 100_000


def test_last_weekday_of_month():
    assert momentum.last_weekday_of_month(date(2026, 10, 30))  # Friday; 31st is a Saturday
    assert not momentum.last_weekday_of_month(date(2026, 10, 16))

