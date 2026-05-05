# 05 — SDK Capabilities Spot-Check (April 2026)

Two narrow research questions, the answers we need before wiring them into the
blueprint, and a one-line GO / NO-GO / WORKAROUND verdict at the bottom of each
section.

---

## Question 1 — `alpaca-py` Level 3 multi-leg options

### Current version

- **`alpaca-py` 0.43.2** (released 2025-11-04) is the latest on PyPI as of
  April 2026. Prior series points: `0.43.0` (2025-10-18), `0.43.1`
  (2025-10-28). No 0.44.x line yet.
- Package: `pip install "alpaca-py>=0.43.2"`. License is Apache 2.0; the
  GitHub repo is `alpacahq/alpaca-py`.

### Multi-leg support — yes, single API call

Alpaca-py supports Level 3 multi-leg options orders **in a single submit call**
via the `mleg` order class. You do **not** need to leg in manually. Verticals,
straddles/strangles, iron condors, iron butterflies, and zero-DTE spreads are
all expressible this way. Hard cap: **4 legs per order**, and the leg
`ratio_qty` values must be reduced so their GCD is 1.

The mechanism is the `legs=[OptionLegRequest(...), ...]` kwarg on a parent
`MarketOrderRequest` or `LimitOrderRequest`, with `order_class=OrderClass.MLEG`.
`OptionLegRequest` carries `symbol`, `side` (`OrderSide.BUY` / `SELL`),
`ratio_qty`, and an optional `position_intent` (`PositionIntent.BTO` /
`BTC` / `STO` / `STC`). The parent order carries `qty` (the multiplier
applied to every leg's ratio), `time_in_force`, and — for limits — `limit_price`
representing the **net debit/credit for the package**, not per leg.

### Minimal example (short put vertical, drawn from Alpaca's own notebook)

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, OptionLegRequest,
)
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

trade_client = TradingClient(API_KEY, SECRET, paper=True)

order_legs = [
    OptionLegRequest(symbol=short_put["symbol"],
                     side=OrderSide.SELL, ratio_qty=1),
    OptionLegRequest(symbol=long_put["symbol"],
                     side=OrderSide.BUY,  ratio_qty=1),
]

req = MarketOrderRequest(
    qty=1,
    order_class=OrderClass.MLEG,
    time_in_force=TimeInForce.DAY,
    legs=order_legs,
)
trade_client.submit_order(req)
```

Replace `MarketOrderRequest` with `LimitOrderRequest(..., limit_price=net_credit)`
for limit orders. The full iron-condor example (4 legs) lives at
`alpaca-py/examples/options/options-iron-condor.ipynb`; companion notebooks
exist for iron butterfly and zero-DTE spreads under `examples/options/`.
Note: the older top-level `examples/options-trading-mleg.ipynb` redirect is
scheduled for removal after 2026-03-01 — point any internal links at
`examples/options/`.

### 2026 caveats

- **Paper supports MLEG.** Multi-leg / Level 3 has been live in paper since the
  2024 changelog; all paper accounts get Level 3 by default. Live accounts
  still require the explicit Level 3 approval flow.
- **No bracket/OTO on MLEG.** You cannot wrap a multi-leg parent order in a
  bracket or OTO. To attach a stop or target, you have to manage that
  client-side — submit follow-on single-leg or MLEG orders after fill.
- **Liquidating spreads is per-leg.** A long-standing community gripe (still
  present per the Alpaca forum thread "Unable to liquidate option spread") is
  that `close_position` on a spread won't unwind it as a package; closing
  legs individually is the documented workaround.
- **GCD = 1 rule.** Pre-reduce ratios. `[2, 2]` will be rejected; submit
  `[1, 1]` and bump parent `qty` to 2.
- **4-leg cap.** Ratio butterflies / condor variants beyond 4 legs need to be
  split into two MLEG orders.

### Verdict

> **GO.** `alpaca-py>=0.43.2` natively does single-call MLEG. Use
> `OrderClass.MLEG` + `legs=[OptionLegRequest(...)]`. Plan client-side stop
> management (no bracket on MLEG) and per-leg unwind for early exits.

---

## Question 2 — Anthropic Batch API tool use

### Tool use is supported

The `/v1/messages/batches` endpoint accepts **the full Messages API request
shape** inside each batch entry's `params` field. That includes `tools`,
`tool_choice`, `system`, multimodal content, extended thinking, and
`cache_control` blocks. The model will return `tool_use` content blocks in
the `result.message.content` array exactly as it does for synchronous
`/v1/messages` calls.

Important nuance: the batch is **fire-and-forget**. There is no inner agentic
loop — the API does not call your tools. You get back the model's first
turn (which may include `tool_use` blocks), and if you want to continue the
conversation with `tool_result` blocks, you need to enqueue a new batch
request with the appended messages. Multi-turn agentic tool use is
expressible but expensive in latency: each turn = one batch round-trip
(up to 24h SLA, typically <1h).

### Request shape (raw JSON)

```json
POST https://api.anthropic.com/v1/messages/batches
{
  "requests": [
    {
      "custom_id": "weather-001",
      "params": {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "tools": [
          {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "input_schema": {
              "type": "object",
              "properties": {
                "location": {"type": "string"}
              },
              "required": ["location"]
            }
          }
        ],
        "messages": [
          {"role": "user", "content": "What's the weather in Boston?"}
        ]
      }
    }
  ]
}
```

### Python SDK equivalent

```python
import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

client = anthropic.Anthropic()

batch = client.messages.batches.create(
    requests=[
        Request(
            custom_id="weather-001",
            params=MessageCreateParamsNonStreaming(
                model="claude-opus-4-7",
                max_tokens=1024,
                tools=[{
                    "name": "get_weather",
                    "description": "Get current weather for a city.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                }],
                messages=[{"role": "user",
                           "content": "What's the weather in Boston?"}],
            ),
        ),
    ]
)
```

Always match results back to inputs by `custom_id` — order is not preserved.

### Limitations vs. synchronous Messages API

- **Latency.** Batches typically finish in well under an hour, but the SLA
  is 24h. Useless for low-latency trading paths; fine for end-of-day
  feature engineering, prompt evaluation, backtest narration, and report
  generation.
- **No streaming.** `MessageCreateParamsNonStreaming` only — you can't
  consume tokens incrementally.
- **Batch sizing.** Up to 10,000 requests / 256 MB per batch. Each batch
  request itself is subject to the same per-request token caps as the
  synchronous API.
- **Same tool schema rules.** Tool count, schema validation, and
  `tool_choice` semantics are identical to the Messages API.
- **Extended-output beta still applies.** With the
  `output-300k-2026-03-24` beta header, `max_tokens` can go to 300,000 on
  Opus 4.7 / Opus 4.6 / Sonnet 4.6 — and that beta is **batch-only**.

### Pricing & caching

- **Batch is 50% off** the standard input/output rates.
- **Prompt caching stacks with batch.** Cache reads inside a batch are still
  billed at the cached rate, and that rate has the 50% batch discount on top.
  For agent/tool workloads where you reuse the same `tools` block + system
  prompt across thousands of items, this is the cheapest combination
  Anthropic offers.
- Caveat: tools are part of the cached prefix (order: tools → system →
  messages). Any change to a tool definition invalidates the cache. Lock
  the tool schema before kicking off a large batch.
- Use **1-hour TTL** cache control on the tools/system block, since batches
  routinely outlive the default 5-minute cache window.
- From 2026-02-05, prompt caches are **workspace-isolated** rather than
  org-isolated — keep batch jobs in the same workspace as the live agent
  if you want them to share warm caches.

### Verdict

> **GO.** Batch API fully supports `tools`/`tool_use` in the request body
> and stacks with prompt caching (1-hour TTL recommended). Use it for
> non-realtime workloads only — single-turn or shallow agent loops where
> 24h SLA is acceptable. Lock tool schemas before launch to keep cache hits.

---

## Sources

- alpaca-py PyPI page — https://pypi.org/project/alpaca-py/
- alpaca-py GitHub — https://github.com/alpacahq/alpaca-py
- alpaca-py iron condor example — https://github.com/alpacahq/alpaca-py/blob/master/examples/options/options-iron-condor.ipynb
- alpaca-py iron butterfly example — https://github.com/alpacahq/alpaca-py/blob/master/examples/options/options-iron-butterfly.ipynb
- alpaca-py zero-DTE example — https://github.com/alpacahq/alpaca-py/blob/master/examples/options/options-zero-dte.ipynb
- alpaca-py requests reference — https://alpaca.markets/sdks/python/api_reference/trading/requests.html
- Alpaca Options Level 3 docs — https://docs.alpaca.markets/docs/options-level-3-trading
- Alpaca multi-leg in paper changelog — https://docs.alpaca.markets/changelog/multi-leg-level-3-options-trading-in-paper
- Alpaca multi-leg orders support — https://alpaca.markets/support/what-are-multi-leg-orders
- Alpaca options trading blog — https://alpaca.markets/blog/level-3-options-trading-now-available-with-alpacas-trading-api/
- Anthropic Batch processing docs — https://docs.claude.com/en/docs/build-with-claude/batch-processing
- Anthropic Create Message Batch API — https://docs.claude.com/en/api/creating-message-batches
- Anthropic Prompt caching docs — https://docs.claude.com/en/docs/build-with-claude/prompt-caching
- anthropic-sdk-python — https://github.com/anthropics/anthropic-sdk-python
- Anthropic batches resource source — https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/resources/beta/messages/batches.py
