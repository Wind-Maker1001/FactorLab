# MC-05-F04C-02 真实价格触及与执行案例

这里依次区分真实分钟 OHLC 的价格触及、冻结滑点下的可执行收盘报价、Paper 确认成交。C 不记账，极值与收盘资格均仅在订单生效至终结的区间内计算。完整分钟触价列表和逐笔账户路径保存在 [AUDIT.json](AUDIT.json) 的 `execution_cases`。

## I1（独立历史窗口）：[2026-05-15T00:00:00+00:00, 2026-05-22T00:00:00+00:00) UTC

最早分歧在 `2026-05-15T05:18:59.999999+00:00` UTC，对应 `2026-05-15T05:18:00+00:00` 分钟。共同订单限价为 233.05，数量为 0.10，提交时间为 `2026-05-14T23:59:59.999999+00:00`。该分钟 low=232.70000、high=233.42000、close=232.79000，成交笔数为 477；对应小时收盘为 230.23000。B 的事件为 `{"event_id":"paper:6","event_time":"2026-05-15T05:18:59.999999+00:00","fee":"0.009317186960000","fill_price":"232.929674000","filled_quantity":"0.10","journal_cursor":6,"reference_price":"232.79000","status":"FILLED"}`，A 对应事件为 `{"event_id":"paper:6","event_time":"2026-05-15T05:59:59.999999+00:00","fee":"0.009214725520000","fill_price":"230.368138000","filled_quantity":"0.10","journal_cursor":6,"reference_price":"230.23000","status":"FILLED"}`。两账户在分歧前的余额一致；B 确认后即时持有库存并支付费用，A 在该时刻仍挂单，等待小时收盘。

### 第一处分歧

以下事件给出最早差异的 UTC 时间、对应真实分钟与小时收盘，以及两账户当时的有效订单、资金和库存。分钟先达到可执行报价时，B 可以先确认成交；A 继续等待小时收盘。成交立即改变 Paper 余额和库存，反向提案与策略风险仍在既定小时决策处理。

```json
{
  "accounts": {
    "A": {
      "after": {
        "available_wallet_cash": "881.02242800000",
        "committed": "118.97757200000",
        "cumulative_fees": "0",
        "entry_price": "0",
        "equity": "1000.00000",
        "inventory": "0",
        "mark_price": "232.79000",
        "nav": "1.00000",
        "pending_reservations": "118.97757200000",
        "position_collateral": "0",
        "reserved_collateral": "118.97757200000",
        "time": "2026-05-15T05:18:59.999999+00:00",
        "unrealized": "0.00000",
        "wallet_cash": "1000",
        "working_orders": [
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "190.28",
            "order_id": "research-00000001",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "200.18",
            "order_id": "research-00000002",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "210.58",
            "order_id": "research-00000003",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "221.53",
            "order_id": "research-00000004",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "233.05",
            "order_id": "research-00000005",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          }
        ]
      },
      "before": {
        "available_wallet_cash": "881.02242800000",
        "committed": "118.97757200000",
        "cumulative_fees": "0",
        "entry_price": "0",
        "equity": "1000.00000",
        "inventory": "0",
        "mark_price": "232.79000",
        "nav": "1.00000",
        "pending_reservations": "118.97757200000",
        "position_collateral": "0",
        "reserved_collateral": "118.97757200000",
        "time": "2026-05-15T05:18:59.999999+00:00",
        "unrealized": "0.00000",
        "wallet_cash": "1000",
        "working_orders": [
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "190.28",
            "order_id": "research-00000001",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "200.18",
            "order_id": "research-00000002",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "210.58",
            "order_id": "research-00000003",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "221.53",
            "order_id": "research-00000004",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "233.05",
            "order_id": "research-00000005",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          }
        ]
      }
    },
    "B": {
      "after": {
        "available_wallet_cash": "881.515657813040000",
        "committed": "118.47502500000",
        "cumulative_fees": "0.009317186960000",
        "entry_price": "232.929674000",
        "equity": "999.976715413040000",
        "inventory": "0.10",
        "mark_price": "232.79000",
        "nav": "0.999976715413040",
        "pending_reservations": "95.18205760000",
        "position_collateral": "23.29296740000",
        "reserved_collateral": "118.47502500000",
        "time": "2026-05-15T05:18:59.999999+00:00",
        "unrealized": "-0.01396740000",
        "wallet_cash": "999.990682813040000",
        "working_orders": [
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "190.28",
            "order_id": "research-00000001",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "200.18",
            "order_id": "research-00000002",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "210.58",
            "order_id": "research-00000003",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "221.53",
            "order_id": "research-00000004",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          }
        ]
      },
      "before": {
        "available_wallet_cash": "881.02242800000",
        "committed": "118.97757200000",
        "cumulative_fees": "0",
        "entry_price": "0",
        "equity": "1000.00000",
        "inventory": "0",
        "mark_price": "232.79000",
        "nav": "1.00000",
        "pending_reservations": "118.97757200000",
        "position_collateral": "0",
        "reserved_collateral": "118.97757200000",
        "time": "2026-05-15T05:18:59.999999+00:00",
        "unrealized": "0.00000",
        "wallet_cash": "1000",
        "working_orders": [
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "190.28",
            "order_id": "research-00000001",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "200.18",
            "order_id": "research-00000002",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "210.58",
            "order_id": "research-00000003",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "221.53",
            "order_id": "research-00000004",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "233.05",
            "order_id": "research-00000005",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "237.86000",
            "side": "BUY",
            "status": "SUBMITTED"
          }
        ]
      }
    }
  },
  "cause": "B observes completed intrahour minute closes while A waits for the hour close; hourly strategy decisions and the hourly capacity envelope are unchanged",
  "event": {
    "event_time": "2026-05-15T05:18:59.999999+00:00",
    "hourly": {
      "event_id": "paper:6",
      "event_time": "2026-05-15T05:59:59.999999+00:00",
      "fee": "0.009214725520000",
      "fill_price": "230.368138000",
      "filled_quantity": "0.10",
      "journal_cursor": 6,
      "reference_price": "230.23000",
      "status": "FILLED"
    },
    "hourly_filled_quantity": "0.10",
    "hourly_final_status": "FILLED",
    "hourly_order_id": "research-00000005",
    "identity": {
      "grid_level": 4,
      "limit_price": "233.05",
      "order_type": "LIMIT",
      "ordinal": 1,
      "purpose": "GRID",
      "quantity": "0.10",
      "reduce_only": false,
      "side": "BUY",
      "submitted_at": "2026-05-14T23:59:59.999999+00:00"
    },
    "minute": {
      "event_id": "paper:6",
      "event_time": "2026-05-15T05:18:59.999999+00:00",
      "fee": "0.009317186960000",
      "fill_price": "232.929674000",
      "filled_quantity": "0.10",
      "journal_cursor": 6,
      "reference_price": "232.79000",
      "status": "FILLED"
    },
    "minute_filled_quantity": "0.10",
    "minute_final_status": "FILLED",
    "minute_order_id": "research-00000005"
  },
  "hour_bar": {
    "available_at": "2026-05-15T06:00:00+00:00",
    "close": "230.23000",
    "close_time": "2026-05-15T05:59:59.999000+00:00",
    "completed_bar": true,
    "high": "234.28000",
    "id": "1h-1778821200000",
    "low": "230.18000",
    "open": "234.19000",
    "open_time": "2026-05-15T05:00:00+00:00",
    "price": "230.23000",
    "quote_volume": "5378663.8942000",
    "taker_buy_quote_volume": "2298492.7241000",
    "taker_buy_volume": "9892.56",
    "time": "2026-05-15T05:00:00+00:00",
    "trade_count": 16949,
    "volume": "23159.01"
  },
  "minute_bar": {
    "available_at": "2026-05-15T05:19:00+00:00",
    "close": "232.79000",
    "close_time": "2026-05-15T05:18:59.999000+00:00",
    "completed_bar": true,
    "high": "233.42000",
    "id": "1m-1778822280000",
    "low": "232.70000",
    "open": "233.22000",
    "open_time": "2026-05-15T05:18:00+00:00",
    "price": "232.79000",
    "quote_volume": "113045.0975000",
    "taker_buy_quote_volume": "29765.4448000",
    "taker_buy_volume": "127.67",
    "time": "2026-05-15T05:18:00+00:00",
    "trade_count": 477,
    "volume": "485.11"
  },
  "snapshot_boundary": "before excludes all journal events at this timestamp; after includes all"
}
```

### A 订单与触价

```json
{
  "eligible_order_minutes": 34440,
  "filled_orders": 2,
  "model_price_eligible_quote_minutes": 2,
  "raw_close_touch_minutes": 167,
  "raw_ohlc_touch_minutes": 199,
  "slippage_close_touch_minutes": 152,
  "slippage_ohlc_touch_minutes": 177,
  "touched_orders": 2
}
```

| 订单 | 方向 | 数量 | 限价 | Paper 原始预留价 | 提交 UTC | 生效 UTC | 有效至 UTC | 首次原始极值触价 | 首次滑点极值触价 | 首次原始分钟收盘触价 | 首次滑点分钟收盘触价 | 最终状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| research-00000001 | BUY | 0.1 | 190.28 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-19T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000002 | BUY | 0.1 | 200.18 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-19T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000003 | BUY | 0.1 | 210.58 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-19T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000004 | BUY | 0.1 | 221.53 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-18T18:59:59.999999+00:00 | 2026-05-18T00:06:00+00:00；low=221.45000，high=222.52000，close=221.59000 | 2026-05-18T00:07:00+00:00；low=221.20000，high=222.00000，close=221.98000 | 2026-05-18T00:44:00+00:00；low=221.46000，high=221.81000，close=221.48000 | 2026-05-18T00:45:00+00:00；low=221.30000，high=221.51000，close=221.36000 | FILLED |
| research-00000005 | BUY | 0.1 | 233.05 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-15T05:59:59.999999+00:00 | 2026-05-15T05:09:00+00:00；low=232.93000，high=233.24000，close=233.03000 | 2026-05-15T05:11:00+00:00；low=232.79000，high=233.04000，close=232.94000 | 2026-05-15T05:09:00+00:00；low=232.93000，high=233.24000，close=233.03000 | 2026-05-15T05:18:00+00:00；low=232.70000，high=233.42000，close=232.79000 | FILLED |
| research-00000006 | SELL | 0.1 | 245.18 | 245.18 | 2026-05-15T05:59:59.999999+00:00 | 2026-05-15T06:00:00+00:00 | 2026-05-19T09:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000007 | SELL | 0.1 | 233.06 | 233.06 | 2026-05-18T18:59:59.999999+00:00 | 2026-05-18T19:00:00+00:00 | 2026-05-22T00:00:00+00:00 | 无 | 无 | 无 | 无 | SUBMITTED |

**research-00000004：确认成交、部分成交和终结事件**

| 时间 UTC | 状态 | 成交价格 | 本次数量 | 累计成交 | 本次费用 |
| --- | --- | --- | --- | --- | --- |
| 2026-05-14T23:59:59.999999+00:00 | SUBMITTED | 无 | 0 | 0 | 0 |
| 2026-05-18T18:59:59.999999+00:00 | FILLED | 219.941886 | 0.1 | 0.1 | 0.00879767544 |

模型报价资格：2026-05-18T18:59:00+00:00；low=219.61000，high=219.92000，close=219.81000

后续反向订单：[{"grid_level":4,"linkage":"opposite adjacent grid level submitted at the fill hour decision","order_id":"research-00000007","quantity":"0.10","side":"SELL","submitted_at":"2026-05-18T18:59:59.999999+00:00"}]

**research-00000005：确认成交、部分成交和终结事件**

| 时间 UTC | 状态 | 成交价格 | 本次数量 | 累计成交 | 本次费用 |
| --- | --- | --- | --- | --- | --- |
| 2026-05-14T23:59:59.999999+00:00 | SUBMITTED | 无 | 0 | 0 | 0 |
| 2026-05-15T05:59:59.999999+00:00 | FILLED | 230.368138 | 0.1 | 0.1 | 0.00921472552 |

模型报价资格：2026-05-15T05:59:00+00:00；low=230.18000，high=230.56000，close=230.23000

后续反向订单：[{"grid_level":5,"linkage":"opposite adjacent grid level submitted at the fill hour decision","order_id":"research-00000006","quantity":"0.10","side":"SELL","submitted_at":"2026-05-15T05:59:59.999999+00:00"}]

从首笔成交开始的逐笔 Paper 现金、库存和费用确认路径见 [AUDIT.json](AUDIT.json) 的 `I1.execution_cases.arms.A.first_fill_path`。同分钟成交笔数和主动买入数量来自已取得的真实分钟字段，不据此推断一分钟内完整成交顺序。

### B 订单与触价

```json
{
  "eligible_order_minutes": 34385,
  "filled_orders": 2,
  "model_price_eligible_quote_minutes": 2,
  "raw_close_touch_minutes": 6,
  "raw_ohlc_touch_minutes": 12,
  "slippage_close_touch_minutes": 2,
  "slippage_ohlc_touch_minutes": 6,
  "touched_orders": 2
}
```

| 订单 | 方向 | 数量 | 限价 | Paper 原始预留价 | 提交 UTC | 生效 UTC | 有效至 UTC | 首次原始极值触价 | 首次滑点极值触价 | 首次原始分钟收盘触价 | 首次滑点分钟收盘触价 | 最终状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| research-00000001 | BUY | 0.1 | 190.28 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-19T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000002 | BUY | 0.1 | 200.18 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-19T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000003 | BUY | 0.1 | 210.58 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-19T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000004 | BUY | 0.1 | 221.53 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-18T00:45:59.999999+00:00 | 2026-05-18T00:06:00+00:00；low=221.45000，high=222.52000，close=221.59000 | 2026-05-18T00:07:00+00:00；low=221.20000，high=222.00000，close=221.98000 | 2026-05-18T00:44:00+00:00；low=221.46000，high=221.81000，close=221.48000 | 2026-05-18T00:45:00+00:00；low=221.30000，high=221.51000，close=221.36000 | FILLED |
| research-00000005 | BUY | 0.1 | 233.05 | 237.86 | 2026-05-14T23:59:59.999999+00:00 | 2026-05-15T00:00:00+00:00 | 2026-05-15T05:18:59.999999+00:00 | 2026-05-15T05:09:00+00:00；low=232.93000，high=233.24000，close=233.03000 | 2026-05-15T05:11:00+00:00；low=232.79000，high=233.04000，close=232.94000 | 2026-05-15T05:09:00+00:00；low=232.93000，high=233.24000，close=233.03000 | 2026-05-15T05:18:00+00:00；low=232.70000，high=233.42000，close=232.79000 | FILLED |
| research-00000006 | SELL | 0.1 | 245.18 | 245.18 | 2026-05-15T05:59:59.999999+00:00 | 2026-05-15T06:00:00+00:00 | 2026-05-19T09:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000007 | SELL | 0.1 | 233.06 | 233.06 | 2026-05-18T00:59:59.999999+00:00 | 2026-05-18T01:00:00+00:00 | 2026-05-22T00:00:00+00:00 | 无 | 无 | 无 | 无 | SUBMITTED |

**research-00000004：确认成交、部分成交和终结事件**

| 时间 UTC | 状态 | 成交价格 | 本次数量 | 累计成交 | 本次费用 |
| --- | --- | --- | --- | --- | --- |
| 2026-05-14T23:59:59.999999+00:00 | SUBMITTED | 无 | 0 | 0 | 0 |
| 2026-05-18T00:45:59.999999+00:00 | FILLED | 221.492816 | 0.1 | 0.1 | 0.00885971264 |

模型报价资格：2026-05-18T00:45:00+00:00；low=221.30000，high=221.51000，close=221.36000

后续反向订单：[{"grid_level":4,"linkage":"opposite adjacent grid level submitted at the fill hour decision","order_id":"research-00000007","quantity":"0.10","side":"SELL","submitted_at":"2026-05-18T00:59:59.999999+00:00"}]

**research-00000005：确认成交、部分成交和终结事件**

| 时间 UTC | 状态 | 成交价格 | 本次数量 | 累计成交 | 本次费用 |
| --- | --- | --- | --- | --- | --- |
| 2026-05-14T23:59:59.999999+00:00 | SUBMITTED | 无 | 0 | 0 | 0 |
| 2026-05-15T05:18:59.999999+00:00 | FILLED | 232.929674 | 0.1 | 0.1 | 0.00931718696 |

模型报价资格：2026-05-15T05:18:00+00:00；low=232.70000，high=233.42000，close=232.79000

后续反向订单：[{"grid_level":5,"linkage":"opposite adjacent grid level submitted at the fill hour decision","order_id":"research-00000006","quantity":"0.10","side":"SELL","submitted_at":"2026-05-15T05:59:59.999999+00:00"}]

从首笔成交开始的逐笔 Paper 现金、库存和费用确认路径见 [AUDIT.json](AUDIT.json) 的 `I1.execution_cases.arms.B.first_fill_path`。同分钟成交笔数和主动买入数量来自已取得的真实分钟字段，不据此推断一分钟内完整成交顺序。

## I2（独立历史窗口）：[2026-05-22T00:00:00+00:00, 2026-05-29T00:00:00+00:00) UTC

最早分歧在 `2026-05-22T14:23:59.999999+00:00` UTC，对应 `2026-05-22T14:23:00+00:00` 分钟。共同订单限价为 215.62，数量为 0.10，提交时间为 `2026-05-21T23:59:59.999999+00:00`。该分钟 low=215.32000、high=215.73000、close=215.37000，成交笔数为 953；对应小时收盘为 217.30000。B 的事件为 `{"event_id":"paper:6","event_time":"2026-05-22T14:23:59.999999+00:00","fee":"0.008619968880000","fill_price":"215.499222000","filled_quantity":"0.10","journal_cursor":6,"reference_price":"215.37000","status":"FILLED"}`，A 对应事件为 `{"event_id":"paper:6","event_time":"2026-05-22T19:59:59.999999+00:00","fee":"0.008624771760000","fill_price":"215.619294000","filled_quantity":"0.10","journal_cursor":6,"reference_price":"215.49000","status":"FILLED"}`。两账户在分歧前的余额一致；B 确认后即时持有库存并支付费用，A 在该时刻仍挂单，等待小时收盘。

### 第一处分歧

以下事件给出最早差异的 UTC 时间、对应真实分钟与小时收盘，以及两账户当时的有效订单、资金和库存。分钟先达到可执行报价时，B 可以先确认成交；A 继续等待小时收盘。成交立即改变 Paper 余额和库存，反向提案与策略风险仍在既定小时决策处理。

```json
{
  "accounts": {
    "A": {
      "after": {
        "available_wallet_cash": "889.92098600000",
        "committed": "110.07901400000",
        "cumulative_fees": "0",
        "entry_price": "0",
        "equity": "1000.00000",
        "inventory": "0",
        "mark_price": "215.37000",
        "nav": "1.00000",
        "pending_reservations": "110.07901400000",
        "position_collateral": "0",
        "reserved_collateral": "110.07901400000",
        "time": "2026-05-22T14:23:59.999999+00:00",
        "unrealized": "0.00000",
        "wallet_cash": "1000",
        "working_orders": [
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "176.05",
            "order_id": "research-00000001",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "185.20",
            "order_id": "research-00000002",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "194.83",
            "order_id": "research-00000003",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "204.96",
            "order_id": "research-00000004",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "215.62",
            "order_id": "research-00000005",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          }
        ]
      },
      "before": {
        "available_wallet_cash": "889.92098600000",
        "committed": "110.07901400000",
        "cumulative_fees": "0",
        "entry_price": "0",
        "equity": "1000.00000",
        "inventory": "0",
        "mark_price": "215.37000",
        "nav": "1.00000",
        "pending_reservations": "110.07901400000",
        "position_collateral": "0",
        "reserved_collateral": "110.07901400000",
        "time": "2026-05-22T14:23:59.999999+00:00",
        "unrealized": "0.00000",
        "wallet_cash": "1000",
        "working_orders": [
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "176.05",
            "order_id": "research-00000001",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "185.20",
            "order_id": "research-00000002",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "194.83",
            "order_id": "research-00000003",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "204.96",
            "order_id": "research-00000004",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "215.62",
            "order_id": "research-00000005",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          }
        ]
      }
    },
    "B": {
      "after": {
        "available_wallet_cash": "890.378246631120000",
        "committed": "109.61313340000",
        "cumulative_fees": "0.008619968880000",
        "entry_price": "215.499222000",
        "equity": "999.978457831120000",
        "inventory": "0.10",
        "mark_price": "215.37000",
        "nav": "0.999978457831120",
        "pending_reservations": "88.06321120000",
        "position_collateral": "21.54992220000",
        "reserved_collateral": "109.61313340000",
        "time": "2026-05-22T14:23:59.999999+00:00",
        "unrealized": "-0.01292220000",
        "wallet_cash": "999.991380031120000",
        "working_orders": [
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "176.05",
            "order_id": "research-00000001",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "185.20",
            "order_id": "research-00000002",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "194.83",
            "order_id": "research-00000003",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "204.96",
            "order_id": "research-00000004",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          }
        ]
      },
      "before": {
        "available_wallet_cash": "889.92098600000",
        "committed": "110.07901400000",
        "cumulative_fees": "0",
        "entry_price": "0",
        "equity": "1000.00000",
        "inventory": "0",
        "mark_price": "215.37000",
        "nav": "1.00000",
        "pending_reservations": "110.07901400000",
        "position_collateral": "0",
        "reserved_collateral": "110.07901400000",
        "time": "2026-05-22T14:23:59.999999+00:00",
        "unrealized": "0.00000",
        "wallet_cash": "1000",
        "working_orders": [
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "176.05",
            "order_id": "research-00000001",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "185.20",
            "order_id": "research-00000002",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "194.83",
            "order_id": "research-00000003",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "204.96",
            "order_id": "research-00000004",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          },
          {
            "cumulative_filled_quantity": "0",
            "limit_price": "215.62",
            "order_id": "research-00000005",
            "quantity": "0.10",
            "remaining_quantity": "0.10",
            "reservation_price": "220.07000",
            "side": "BUY",
            "status": "SUBMITTED"
          }
        ]
      }
    }
  },
  "cause": "B observes completed intrahour minute closes while A waits for the hour close; hourly strategy decisions and the hourly capacity envelope are unchanged",
  "event": {
    "event_time": "2026-05-22T14:23:59.999999+00:00",
    "hourly": {
      "event_id": "paper:6",
      "event_time": "2026-05-22T19:59:59.999999+00:00",
      "fee": "0.008624771760000",
      "fill_price": "215.619294000",
      "filled_quantity": "0.10",
      "journal_cursor": 6,
      "reference_price": "215.49000",
      "status": "FILLED"
    },
    "hourly_filled_quantity": "0.10",
    "hourly_final_status": "FILLED",
    "hourly_order_id": "research-00000005",
    "identity": {
      "grid_level": 4,
      "limit_price": "215.62",
      "order_type": "LIMIT",
      "ordinal": 1,
      "purpose": "GRID",
      "quantity": "0.10",
      "reduce_only": false,
      "side": "BUY",
      "submitted_at": "2026-05-21T23:59:59.999999+00:00"
    },
    "minute": {
      "event_id": "paper:6",
      "event_time": "2026-05-22T14:23:59.999999+00:00",
      "fee": "0.008619968880000",
      "fill_price": "215.499222000",
      "filled_quantity": "0.10",
      "journal_cursor": 6,
      "reference_price": "215.37000",
      "status": "FILLED"
    },
    "minute_filled_quantity": "0.10",
    "minute_final_status": "FILLED",
    "minute_order_id": "research-00000005"
  },
  "hour_bar": {
    "available_at": "2026-05-22T15:00:00+00:00",
    "close": "217.30000",
    "close_time": "2026-05-22T14:59:59.999000+00:00",
    "completed_bar": true,
    "high": "218.17000",
    "id": "1h-1779458400000",
    "low": "215.32000",
    "open": "218.17000",
    "open_time": "2026-05-22T14:00:00+00:00",
    "price": "217.30000",
    "quote_volume": "24725534.7337000",
    "taker_buy_quote_volume": "13861218.2090000",
    "taker_buy_volume": "64094.75",
    "time": "2026-05-22T14:00:00+00:00",
    "trade_count": 31156,
    "volume": "114285.77"
  },
  "minute_bar": {
    "available_at": "2026-05-22T14:24:00+00:00",
    "close": "215.37000",
    "close_time": "2026-05-22T14:23:59.999000+00:00",
    "completed_bar": true,
    "high": "215.73000",
    "id": "1m-1779459780000",
    "low": "215.32000",
    "open": "215.72000",
    "open_time": "2026-05-22T14:23:00+00:00",
    "price": "215.37000",
    "quote_volume": "580157.7745000",
    "taker_buy_quote_volume": "216440.4355000",
    "taker_buy_volume": "1004.41",
    "time": "2026-05-22T14:23:00+00:00",
    "trade_count": 953,
    "volume": "2692.16"
  },
  "snapshot_boundary": "before excludes all journal events at this timestamp; after includes all"
}
```

### A 订单与触价

```json
{
  "eligible_order_minutes": 31200,
  "filled_orders": 1,
  "model_price_eligible_quote_minutes": 1,
  "raw_close_touch_minutes": 32,
  "raw_ohlc_touch_minutes": 39,
  "slippage_close_touch_minutes": 15,
  "slippage_ohlc_touch_minutes": 29,
  "touched_orders": 1
}
```

| 订单 | 方向 | 数量 | 限价 | Paper 原始预留价 | 提交 UTC | 生效 UTC | 有效至 UTC | 首次原始极值触价 | 首次滑点极值触价 | 首次原始分钟收盘触价 | 首次滑点分钟收盘触价 | 最终状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| research-00000001 | BUY | 0.1 | 176.05 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-26T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000002 | BUY | 0.1 | 185.2 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-26T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000003 | BUY | 0.1 | 194.83 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-26T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000004 | BUY | 0.1 | 204.96 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-26T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000005 | BUY | 0.1 | 215.62 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-22T19:59:59.999999+00:00 | 2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000 | 2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000 | 2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000 | 2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000 | FILLED |
| research-00000006 | SELL | 0.1 | 226.84 | 226.84 | 2026-05-22T19:59:59.999999+00:00 | 2026-05-22T20:00:00+00:00 | 2026-05-26T23:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |

**research-00000005：确认成交、部分成交和终结事件**

| 时间 UTC | 状态 | 成交价格 | 本次数量 | 累计成交 | 本次费用 |
| --- | --- | --- | --- | --- | --- |
| 2026-05-21T23:59:59.999999+00:00 | SUBMITTED | 无 | 0 | 0 | 0 |
| 2026-05-22T19:59:59.999999+00:00 | FILLED | 215.619294 | 0.1 | 0.1 | 0.00862477176 |

模型报价资格：2026-05-22T19:59:00+00:00；low=215.46000，high=215.70000，close=215.49000

后续反向订单：[{"grid_level":5,"linkage":"opposite adjacent grid level submitted at the fill hour decision","order_id":"research-00000006","quantity":"0.10","side":"SELL","submitted_at":"2026-05-22T19:59:59.999999+00:00"}]

从首笔成交开始的逐笔 Paper 现金、库存和费用确认路径见 [AUDIT.json](AUDIT.json) 的 `I2.execution_cases.arms.A.first_fill_path`。同分钟成交笔数和主动买入数量来自已取得的真实分钟字段，不据此推断一分钟内完整成交顺序。

### B 订单与触价

```json
{
  "eligible_order_minutes": 30864,
  "filled_orders": 1,
  "model_price_eligible_quote_minutes": 1,
  "raw_close_touch_minutes": 1,
  "raw_ohlc_touch_minutes": 1,
  "slippage_close_touch_minutes": 1,
  "slippage_ohlc_touch_minutes": 1,
  "touched_orders": 1
}
```

| 订单 | 方向 | 数量 | 限价 | Paper 原始预留价 | 提交 UTC | 生效 UTC | 有效至 UTC | 首次原始极值触价 | 首次滑点极值触价 | 首次原始分钟收盘触价 | 首次滑点分钟收盘触价 | 最终状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| research-00000001 | BUY | 0.1 | 176.05 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-26T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000002 | BUY | 0.1 | 185.2 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-26T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000003 | BUY | 0.1 | 194.83 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-26T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000004 | BUY | 0.1 | 204.96 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-26T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000005 | BUY | 0.1 | 215.62 | 220.07 | 2026-05-21T23:59:59.999999+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-22T14:23:59.999999+00:00 | 2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000 | 2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000 | 2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000 | 2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000 | FILLED |
| research-00000006 | SELL | 0.1 | 226.84 | 226.84 | 2026-05-22T14:59:59.999999+00:00 | 2026-05-22T15:00:00+00:00 | 2026-05-26T18:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |

**research-00000005：确认成交、部分成交和终结事件**

| 时间 UTC | 状态 | 成交价格 | 本次数量 | 累计成交 | 本次费用 |
| --- | --- | --- | --- | --- | --- |
| 2026-05-21T23:59:59.999999+00:00 | SUBMITTED | 无 | 0 | 0 | 0 |
| 2026-05-22T14:23:59.999999+00:00 | FILLED | 215.499222 | 0.1 | 0.1 | 0.00861996888 |

模型报价资格：2026-05-22T14:23:00+00:00；low=215.32000，high=215.73000，close=215.37000

后续反向订单：[{"grid_level":5,"linkage":"opposite adjacent grid level submitted at the fill hour decision","order_id":"research-00000006","quantity":"0.10","side":"SELL","submitted_at":"2026-05-22T14:59:59.999999+00:00"}]

从首笔成交开始的逐笔 Paper 现金、库存和费用确认路径见 [AUDIT.json](AUDIT.json) 的 `I2.execution_cases.arms.B.first_fill_path`。同分钟成交笔数和主动买入数量来自已取得的真实分钟字段，不据此推断一分钟内完整成交顺序。

## I3（独立历史窗口）：[2026-05-29T00:00:00+00:00, 2026-06-05T00:00:00+00:00) UTC

A/B 没有订单或经济状态的首处分歧。即使成交相同，非零账务案例仍完整保留。

### A 订单与触价

```json
{
  "eligible_order_minutes": 30000,
  "filled_orders": 0,
  "touched_orders": 0
}
```

| 订单 | 方向 | 数量 | 限价 | Paper 原始预留价 | 提交 UTC | 生效 UTC | 有效至 UTC | 首次原始极值触价 | 首次滑点极值触价 | 首次原始分钟收盘触价 | 首次滑点分钟收盘触价 | 最终状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| research-00000001 | BUY | 0.1 | 171.4 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000002 | BUY | 0.1 | 180.31 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000003 | BUY | 0.1 | 189.68 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000004 | BUY | 0.1 | 199.54 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000005 | BUY | 0.1 | 209.92 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |

从首笔成交开始的逐笔 Paper 现金、库存和费用确认路径见 [AUDIT.json](AUDIT.json) 的 `I3.execution_cases.arms.A.first_fill_path`。同分钟成交笔数和主动买入数量来自已取得的真实分钟字段，不据此推断一分钟内完整成交顺序。

### B 订单与触价

```json
{
  "eligible_order_minutes": 30000,
  "filled_orders": 0,
  "touched_orders": 0
}
```

| 订单 | 方向 | 数量 | 限价 | Paper 原始预留价 | 提交 UTC | 生效 UTC | 有效至 UTC | 首次原始极值触价 | 首次滑点极值触价 | 首次原始分钟收盘触价 | 首次滑点分钟收盘触价 | 最终状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| research-00000001 | BUY | 0.1 | 171.4 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000002 | BUY | 0.1 | 180.31 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000003 | BUY | 0.1 | 189.68 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000004 | BUY | 0.1 | 199.54 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |
| research-00000005 | BUY | 0.1 | 209.92 | 214.25 | 2026-05-28T23:59:59.999999+00:00 | 2026-05-29T00:00:00+00:00 | 2026-06-02T03:59:59.999999+00:00 | 无 | 无 | 无 | 无 | CANCELED |

从首笔成交开始的逐笔 Paper 现金、库存和费用确认路径见 [AUDIT.json](AUDIT.json) 的 `I3.execution_cases.arms.B.first_fill_path`。同分钟成交笔数和主动买入数量来自已取得的真实分钟字段，不据此推断一分钟内完整成交顺序。

交易所实际成交能力还需要对应历史时刻的最优买卖报价、可分配深度、逐笔顺序、订单到达与撤销确认、队列位置及实际费用和规则。公开逐笔记录可细化价格和数量证据，无法单独恢复本订单的排队位置。本轮的三类研究证据均保留 `REAL_FILL_NOT_VERIFIED`。
