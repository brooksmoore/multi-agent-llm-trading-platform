# Alpaca Paper Session — Final Snapshot
**Captured:** 2026-06-10 (transition to Robinhood dry-run)

---

## NAV Summary

| | Value |
|---|---|
| Start date | 2026-05-11 |
| End date | 2026-06-10 |
| Starting NAV | $101,790.38 |
| Ending NAV | $99,322.05 |
| Peak NAV | $103,856.12 (2026-06-02) |
| **Total return** | **−2.42%** |
| SPY over same period | −1.88% |
| **Excess vs SPY** | **−0.54%** |

30-day window. Bot underperformed SPY by 0.54%. Consistent with the honest assessment in `blueprint/01_HONEST_ASSESSMENT.md` — rules-only backtest showed strategies only beat SPY in 1/4 walk-forward windows; 30 days is too short to draw conclusions.

---

## Realized P&L by Agent

| Agent | Realized P&L | Closed lots |
|---|---|---|
| haiku | +$387.01 | 113 |
| sonnet | +$1,217.73 | 103 |
| opus | +$3.72 | 3 |
| **TOTAL** | **+$1,608.46** | 219 |

---

## Open Positions at Transition

### haiku (Faber GTAA trend-following)
| Symbol | Qty | Entry | Cost Basis |
|---|---|---|---|
| QQQ | 14.36 | $709.1x | ~$10,191 |
| EFA | 40.04 | $103.72 | ~$4,152 |
| EEM | 46.63 | $67.40 | ~$3,143 |
| IWM | 12.03 | $282.x | ~$3,393 |
| SPY | ~9.27 | $737.x | ~$6,837 |
| USO | 5.60 | $142.78 | ~$799 |
| BTCUSD/ETHUSD/SOLUSD | 0 | — | $0 |

### manager (SPY buffer / allocator)
| Symbol | Qty | Entry | Cost Basis |
|---|---|---|---|
| SPY | ~13.41 | $750.x | ~$10,061 |

### opus (GARP discretionary)
| Symbol | Qty | Entry | Cost Basis |
|---|---|---|---|
| COST | 1.42 | $1,005.47 | ~$1,427 |
| MSFT | 2.60 | $440.93 | ~$1,146 |
| AVGO | 1.51 | $424.71 | ~$641 |
| GOOGL | 1.74 | $365.87 | ~$638 |
| V | 2.63 | $328.6x | ~$864 |

### sonnet (12-1 price momentum)
| Symbol | Qty | Entry | Cost Basis |
|---|---|---|---|
| GS | 3.26 | $972.37 | ~$3,175 |
| GOOGL | 5.29 | $399.00 | ~$2,110 |
| MS | 12.22 | $203.x | ~$2,485 |
| AVGO | 4.72 | $422.79 | ~$1,995 |
| NVDA | 9.17 | $219.x | ~$2,015 |
| TSLA | 3.58 | $413.47 | ~$1,482 |
| CAT | 1.53 | $909.40 | ~$1,389 |
| NET | 4.02 | $192.93 | ~$775 |
| MDB | 3.07 | $337.x | ~$1,041 |
| DDOG | 2.86 | $231.x | ~$666 |

---

## Notes

- Positions carried forward into Robinhood dry-run session (lot ledger unbroken).
- Dashboard NAV curve is continuous — no reset at this boundary.
- Dashboard will be reset when `live_trading_enabled=True` is armed (clean Robinhood start).
- 30-day window is statistically insignificant. Thesis unresolved.
