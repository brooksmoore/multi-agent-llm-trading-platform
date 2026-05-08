"""
Proxy benchmark for vectorbt-style backtesting workload.

vectorbt itself cannot be installed in this sandbox (PyPI blocked by the
session proxy). This benchmark uses numpy + pandas to model the same
workload. Treat it as a CONSERVATIVE proxy for vectorbt's hot path,
because vectorbt's core uses numba JIT (typically equal to or faster
than dense vectorized numpy for this kind of vector code).

Workload mirrors the user's actual harness:
  - 500 tickers x 5 years x ~252 daily bars (~630K rows)
  - Monthly rebalance, rank by trailing 12-1 momentum, top-N equal weight
  - Sweep 100 parameter combos (10 lookbacks x 10 hold sizes)
  - Sharpe / Sortino / max DD per strategy
  - Walk-forward CV cost projection (24 monthly windows)
"""
import gc, time, resource, json
import numpy as np
import pandas as pd

def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

class Timer:
    def __init__(self, name): self.name = name
    def __enter__(self):
        gc.collect()
        self.t0 = time.perf_counter()
        return self
    def __exit__(self, *a):
        self.dt = time.perf_counter() - self.t0
        print(f"  [{self.name}] {self.dt*1000:.1f} ms   peak RSS={peak_rss_mb():.0f} MB")

# 1. Generate synthetic OHLCV
N_TICKERS = 500
N_YEARS = 5
N_BARS = 252 * N_YEARS  # 1260

print(f"Universe: {N_TICKERS} tickers x {N_BARS} bars = {N_TICKERS*N_BARS:,} rows")

with Timer("data_gen") as t_gen:
    rng = np.random.default_rng(42)
    daily_drift = 0.08 / 252
    daily_vol   = 0.20 / np.sqrt(252)
    rets = rng.normal(daily_drift, daily_vol, size=(N_BARS, N_TICKERS)).astype(np.float64)
    rets += rng.normal(0, 0.03/252, size=N_TICKERS)
    close = 100.0 * np.exp(np.cumsum(rets, axis=0))
    intraday = rng.normal(0, daily_vol/2, size=(N_BARS, N_TICKERS))
    high  = close * (1 + np.abs(intraday))
    low   = close * (1 - np.abs(intraday))
    openp = close * (1 + rng.normal(0, daily_vol/3, size=(N_BARS, N_TICKERS)))
    vol   = rng.integers(1e5, 1e7, size=(N_BARS, N_TICKERS)).astype(np.float64)
    dates = pd.bdate_range("2020-01-01", periods=N_BARS)
t_data_load = t_gen.dt

ohlcv_mb = (close.nbytes + high.nbytes + low.nbytes + openp.nbytes + vol.nbytes) / 1024**2
print(f"  OHLCV array footprint: {ohlcv_mb:.0f} MB  (5 panels x float64)")

def monthly_rebal_idx(date_index):
    idx = pd.Series(np.arange(len(date_index)), index=date_index)
    return idx.groupby([date_index.year, date_index.month]).first().values

rebal_idx_full = monthly_rebal_idx(dates)
print(f"  Monthly rebalance points: {len(rebal_idx_full)}")

# 2. Single backtest
def run_backtest(close_arr, rebal_idx, lookback, hold_n, skip=21):
    n_bars, n_tk = close_arr.shape
    dret = np.empty_like(close_arr)
    dret[0] = 0.0
    dret[1:] = close_arr[1:] / close_arr[:-1] - 1.0
    w = np.zeros((n_bars, n_tk), dtype=np.float64)
    for i, rb in enumerate(rebal_idx):
        if rb < lookback + skip:
            continue
        mom = close_arr[rb - skip] / close_arr[rb - lookback] - 1.0
        top = np.argpartition(-mom, hold_n)[:hold_n]
        end = rebal_idx[i+1] if i+1 < len(rebal_idx) else n_bars
        w[rb:end, top] = 1.0 / hold_n
    return np.einsum('ij,ij->i', w, dret)

print("\n--- single backtest (lookback=252, hold_n=15) ---")
with Timer("single_backtest") as t_single:
    pr = run_backtest(close, rebal_idx_full, lookback=252, hold_n=15)
t_one = t_single.dt
print(f"  total return: {(np.prod(1+pr)-1)*100:.1f}%   bars with position: {(pr!=0).sum()}")

# 3. Metrics
def metrics(port_ret):
    mu = port_ret.mean() * 252
    sd = port_ret.std(ddof=1) * np.sqrt(252)
    downside = port_ret[port_ret < 0]
    sortino_sd = downside.std(ddof=1) * np.sqrt(252) if len(downside) > 1 else np.nan
    sharpe  = mu / sd if sd > 0 else np.nan
    sortino = mu / sortino_sd if sortino_sd and sortino_sd > 0 else np.nan
    eq = np.cumprod(1 + port_ret)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    return sharpe, sortino, dd.min()

print("\n--- metrics computation (single run) ---")
with Timer("metrics_single") as t_m:
    s, so, mdd = metrics(pr)
t_metric_one = t_m.dt
print(f"  Sharpe={s:.2f}  Sortino={so:.2f}  MaxDD={mdd*100:.1f}%")

# 4. Sweep
lookbacks = [63, 84, 126, 147, 168, 189, 210, 231, 252, 273]
hold_ns   = [5, 8, 10, 12, 15, 18, 20, 25, 30, 40]
combos = [(lb, hn) for lb in lookbacks for hn in hold_ns]
print(f"\n--- parameter sweep: {len(combos)} combos ---")

results = []
with Timer("full_sweep_100") as t_sweep:
    for lb, hn in combos:
        pr = run_backtest(close, rebal_idx_full, lookback=lb, hold_n=hn)
        s, so, mdd = metrics(pr)
        results.append((lb, hn, s, so, mdd))
t_sweep_total = t_sweep.dt
print(f"  per-combo avg: {t_sweep_total/len(combos)*1000:.1f} ms")

# 5. Walk-forward CV projection
N_WF = 24
print("\n--- walk-forward window sweep (subset) ---")
# Use a 36-month training window (~756 bars) for a realistic WF fold
wf_bars = min(N_BARS - 60, 756)
wf_close = close[:wf_bars]
wf_rebal = monthly_rebal_idx(dates[:wf_bars])
N_PROBE = 20
with Timer("wf_window_20combos") as t_wf:
    for lb, hn in combos[:N_PROBE]:
        pr = run_backtest(wf_close, wf_rebal, lookback=lb, hold_n=hn)
        metrics(pr)
per_combo_wf = t_wf.dt / N_PROBE
projected_wf_total = per_combo_wf * N_WF * len(combos)
print(f"  projected full WF (24 windows x 100 combos): {projected_wf_total:.2f} s")

summary = {
    "rows": N_TICKERS * N_BARS,
    "ohlcv_mem_mb": round(ohlcv_mb, 1),
    "data_gen_s": round(t_data_load, 3),
    "single_backtest_s": round(t_one, 4),
    "metrics_s": round(t_metric_one, 5),
    "sweep_100_s": round(t_sweep_total, 3),
    "per_combo_s": round(t_sweep_total/len(combos), 4),
    "wf_projection_s": round(projected_wf_total, 2),
    "peak_rss_mb": round(peak_rss_mb(), 0),
}
print("\n=== SUMMARY (JSON) ===")
print(json.dumps(summary, indent=2))
