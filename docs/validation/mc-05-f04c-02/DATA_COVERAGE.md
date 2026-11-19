# MC-05-F04C-02 行情覆盖与准入

窗口选择先于分钟采集和账户回放。独立窗口按 UTC 时间顺序固定，专项案例只按预注册的初始网格、有效期与小时高低价筛查；选择记录不读取收益或回撤。候选全表保留未选中和数据拒绝的窗口。

| 候选 | 样本类型 | 开始 UTC | 结束 UTC（不含） | 锚点小时起点 UTC | 锚点收盘价 | 选中 | 数据状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| I1 | INDEPENDENT_HISTORICAL_WINDOW | 2026-05-15T00:00:00+00:00 | 2026-05-22T00:00:00+00:00 | 2026-05-14T23:00:00+00:00 | 237.86 | True | MARKET_DATA_ACCEPTED |
| I2 | INDEPENDENT_HISTORICAL_WINDOW | 2026-05-22T00:00:00+00:00 | 2026-05-29T00:00:00+00:00 | 2026-05-21T23:00:00+00:00 | 220.07 | True | MARKET_DATA_ACCEPTED |
| I3 | INDEPENDENT_HISTORICAL_WINDOW | 2026-05-29T00:00:00+00:00 | 2026-06-05T00:00:00+00:00 | 2026-05-28T23:00:00+00:00 | 214.25 | True | MARKET_DATA_ACCEPTED |
| C1 | CONDITIONALLY_SELECTED_EXECUTION_CASE | 2026-06-05T00:00:00+00:00 | 2026-06-12T00:00:00+00:00 | 2026-06-04T23:00:00+00:00 | 216.45 | True | MARKET_DATA_BLOCKED |
| C2 | CONDITIONALLY_SELECTED_EXECUTION_CASE | 2026-06-12T00:00:00+00:00 | 2026-06-19T00:00:00+00:00 | 2026-06-11T23:00:00+00:00 | 205.99 | False | NOT_SELECTED |
| C3 | CONDITIONALLY_SELECTED_EXECUTION_CASE | 2026-06-19T00:00:00+00:00 | 2026-06-26T00:00:00+00:00 | 2026-06-18T23:00:00+00:00 | 210.43 | False | NOT_SELECTED |
| C4 | CONDITIONALLY_SELECTED_EXECUTION_CASE | 2026-06-26T00:00:00+00:00 | 2026-07-03T00:00:00+00:00 | 2026-06-25T23:00:00+00:00 | 194.94 | False | NOT_SELECTED |
| C5 | CONDITIONALLY_SELECTED_EXECUTION_CASE | 2026-07-03T00:00:00+00:00 | 2026-07-10T00:00:00+00:00 | 2026-07-02T23:00:00+00:00 | 194.52 | False | NOT_SELECTED |
| C6 | CONDITIONALLY_SELECTED_EXECUTION_CASE | 2026-07-10T00:00:00+00:00 | 2026-07-17T00:00:00+00:00 | 2026-07-09T23:00:00+00:00 | 202.38 | False | NOT_SELECTED |
| C7 | CONDITIONALLY_SELECTED_EXECUTION_CASE | 2026-07-17T00:00:00+00:00 | 2026-07-24T00:00:00+00:00 | 2026-07-16T23:00:00+00:00 | 205.99 | False | NOT_SELECTED |
| C8 | CONDITIONALLY_SELECTED_EXECUTION_CASE | 2026-07-24T00:00:00+00:00 | 2026-07-31T00:00:00+00:00 | 2026-07-23T23:00:00+00:00 | 207.99 | False | NOT_SELECTED |

### C1 小时筛查

```json
{
  "anchor_close": "216.45000",
  "anchor_time": "2026-06-04T23:00:00+00:00",
  "first_potential_touch": {
    "grid_level": 4,
    "high": "215.15000",
    "hour": "2026-06-05T13:00:00+00:00",
    "limit_price": "212.07",
    "low": "211.96000",
    "order_id": "research-00000005",
    "quantity": "0.10",
    "side": "BUY"
  },
  "initial_orders": [
    {
      "grid_level": 0,
      "limit_price": "173.16",
      "order_id": "research-00000001",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 1,
      "limit_price": "182.16",
      "order_id": "research-00000002",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 2,
      "limit_price": "191.63",
      "order_id": "research-00000003",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 3,
      "limit_price": "201.59",
      "order_id": "research-00000004",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 4,
      "limit_price": "212.07",
      "order_id": "research-00000005",
      "quantity": "0.10",
      "side": "BUY"
    }
  ],
  "inspected_hours": 100,
  "potential_order_hours": 93
}
```

### C2 小时筛查

```json
{
  "anchor_close": "205.99000",
  "anchor_time": "2026-06-11T23:00:00+00:00",
  "first_potential_touch": null,
  "initial_orders": [
    {
      "grid_level": 0,
      "limit_price": "164.79",
      "order_id": "research-00000001",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 1,
      "limit_price": "173.35",
      "order_id": "research-00000002",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 2,
      "limit_price": "182.37",
      "order_id": "research-00000003",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 3,
      "limit_price": "191.85",
      "order_id": "research-00000004",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 4,
      "limit_price": "201.82",
      "order_id": "research-00000005",
      "quantity": "0.10",
      "side": "BUY"
    }
  ],
  "inspected_hours": 100,
  "potential_order_hours": 0
}
```

### C3 小时筛查

```json
{
  "anchor_close": "210.43000",
  "anchor_time": "2026-06-18T23:00:00+00:00",
  "first_potential_touch": {
    "grid_level": 4,
    "high": "206.55000",
    "hour": "2026-06-23T03:00:00+00:00",
    "limit_price": "206.17",
    "low": "205.35000",
    "order_id": "research-00000005",
    "quantity": "0.10",
    "side": "BUY"
  },
  "initial_orders": [
    {
      "grid_level": 0,
      "limit_price": "168.34",
      "order_id": "research-00000001",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 1,
      "limit_price": "177.09",
      "order_id": "research-00000002",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 2,
      "limit_price": "186.30",
      "order_id": "research-00000003",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 3,
      "limit_price": "195.98",
      "order_id": "research-00000004",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 4,
      "limit_price": "206.17",
      "order_id": "research-00000005",
      "quantity": "0.10",
      "side": "BUY"
    }
  ],
  "inspected_hours": 100,
  "potential_order_hours": 1
}
```

### C4 小时筛查

```json
{
  "anchor_close": "194.94000",
  "anchor_time": "2026-06-25T23:00:00+00:00",
  "first_potential_touch": {
    "grid_level": 4,
    "high": "195.05000",
    "hour": "2026-06-29T14:00:00+00:00",
    "limit_price": "191.00",
    "low": "189.96000",
    "order_id": "research-00000005",
    "quantity": "0.10",
    "side": "BUY"
  },
  "initial_orders": [
    {
      "grid_level": 0,
      "limit_price": "155.95",
      "order_id": "research-00000001",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 1,
      "limit_price": "164.05",
      "order_id": "research-00000002",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 2,
      "limit_price": "172.58",
      "order_id": "research-00000003",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 3,
      "limit_price": "181.56",
      "order_id": "research-00000004",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 4,
      "limit_price": "191.00",
      "order_id": "research-00000005",
      "quantity": "0.10",
      "side": "BUY"
    }
  ],
  "inspected_hours": 100,
  "potential_order_hours": 1
}
```

### C5 小时筛查

```json
{
  "anchor_close": "194.52000",
  "anchor_time": "2026-07-02T23:00:00+00:00",
  "first_potential_touch": null,
  "initial_orders": [
    {
      "grid_level": 0,
      "limit_price": "155.61",
      "order_id": "research-00000001",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 1,
      "limit_price": "163.70",
      "order_id": "research-00000002",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 2,
      "limit_price": "172.21",
      "order_id": "research-00000003",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 3,
      "limit_price": "181.17",
      "order_id": "research-00000004",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 4,
      "limit_price": "190.58",
      "order_id": "research-00000005",
      "quantity": "0.10",
      "side": "BUY"
    }
  ],
  "inspected_hours": 100,
  "potential_order_hours": 0
}
```

### C6 小时筛查

```json
{
  "anchor_close": "202.38000",
  "anchor_time": "2026-07-09T23:00:00+00:00",
  "first_potential_touch": null,
  "initial_orders": [
    {
      "grid_level": 0,
      "limit_price": "161.90",
      "order_id": "research-00000001",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 1,
      "limit_price": "170.32",
      "order_id": "research-00000002",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 2,
      "limit_price": "179.17",
      "order_id": "research-00000003",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 3,
      "limit_price": "188.49",
      "order_id": "research-00000004",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 4,
      "limit_price": "198.29",
      "order_id": "research-00000005",
      "quantity": "0.10",
      "side": "BUY"
    }
  ],
  "inspected_hours": 100,
  "potential_order_hours": 0
}
```

### C7 小时筛查

```json
{
  "anchor_close": "205.99000",
  "anchor_time": "2026-07-16T23:00:00+00:00",
  "first_potential_touch": {
    "grid_level": 4,
    "high": "202.77000",
    "hour": "2026-07-17T05:00:00+00:00",
    "limit_price": "201.82",
    "low": "201.53000",
    "order_id": "research-00000005",
    "quantity": "0.10",
    "side": "BUY"
  },
  "initial_orders": [
    {
      "grid_level": 0,
      "limit_price": "164.79",
      "order_id": "research-00000001",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 1,
      "limit_price": "173.35",
      "order_id": "research-00000002",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 2,
      "limit_price": "182.37",
      "order_id": "research-00000003",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 3,
      "limit_price": "191.85",
      "order_id": "research-00000004",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 4,
      "limit_price": "201.82",
      "order_id": "research-00000005",
      "quantity": "0.10",
      "side": "BUY"
    }
  ],
  "inspected_hours": 100,
  "potential_order_hours": 10
}
```

### C8 小时筛查

```json
{
  "anchor_close": "207.99000",
  "anchor_time": "2026-07-23T23:00:00+00:00",
  "first_potential_touch": {
    "grid_level": 4,
    "high": "209.05000",
    "hour": "2026-07-27T13:00:00+00:00",
    "limit_price": "203.78",
    "low": "201.40000",
    "order_id": "research-00000005",
    "quantity": "0.10",
    "side": "BUY"
  },
  "initial_orders": [
    {
      "grid_level": 0,
      "limit_price": "166.39",
      "order_id": "research-00000001",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 1,
      "limit_price": "175.04",
      "order_id": "research-00000002",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 2,
      "limit_price": "184.14",
      "order_id": "research-00000003",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 3,
      "limit_price": "193.71",
      "order_id": "research-00000004",
      "quantity": "0.10",
      "side": "BUY"
    },
    {
      "grid_level": 4,
      "limit_price": "203.78",
      "order_id": "research-00000005",
      "quantity": "0.10",
      "side": "BUY"
    }
  ],
  "inspected_hours": 100,
  "potential_order_hours": 15
}
```

## C1（触价专项案例）：[2026-06-05T00:00:00+00:00, 2026-06-12T00:00:00+00:00) UTC

数据拒绝原因：incomplete bounded acquisition receipts; no retry or replacement window: NVDAUSDT-1m-2026-06-07.zip, NVDAUSDT-1m-2026-06-07.zip.CHECKSUM, NVDAUSDT-1d-2026-06-07.zip, NVDAUSDT-1d-2026-06-07.zip.CHECKSUM, NVDAUSDT-1m-2026-06-08.zip, NVDAUSDT-1m-2026-06-08.zip.CHECKSUM, NVDAUSDT-1d-2026-06-08.zip, NVDAUSDT-1d-2026-06-08.zip.CHECKSUM, NVDAUSDT-1m-2026-06-09.zip, NVDAUSDT-1m-2026-06-09.zip.CHECKSUM, NVDAUSDT-1d-2026-06-09.zip, NVDAUSDT-1d-2026-06-09.zip.CHECKSUM, NVDAUSDT-1m-2026-06-10.zip, NVDAUSDT-1m-2026-06-10.zip.CHECKSUM, NVDAUSDT-1d-2026-06-10.zip, NVDAUSDT-1d-2026-06-10.zip.CHECKSUM, NVDAUSDT-1m-2026-06-11.zip, NVDAUSDT-1m-2026-06-11.zip.CHECKSUM, NVDAUSDT-1d-2026-06-11.zip, NVDAUSDT-1d-2026-06-11.zip.CHECKSUM

## I1（独立历史窗口）：[2026-05-15T00:00:00+00:00, 2026-05-22T00:00:00+00:00) UTC

共 10080 根分钟线、168 根研究小时线、7 根独立日线，另有前置小时 `2026-05-14T23:00:00+00:00`。锚点收盘为 237.86 USDT。

| 核对 | OHLCV 一致桶数 | 辅助字段差异 |
| --- | --- | --- |
| 60 分钟 → 认证小时 | 168 | {"quote_volume": 0, "taker_buy_quote_volume": 0, "taker_buy_volume": 0, "trade_count": 0} |
| 24 小时 → 独立日线 | 7 | {"quote_volume": 0, "taker_buy_quote_volume": 0, "taker_buy_volume": 0, "trade_count": 0} |

R04B data version：`352d960fad24e50f1423ff173d838a994a29603d2629652f46c7edc76e5e2a83`；bars SHA-256：`16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab`；取得时间：`2026-11-13T17:12:50.891167+00:00`。

实际引用的原始小时对象：

| 官方 URL | SHA-256 | 取得时间 UTC |
| --- | --- | --- |
| [小时 ZIP](https://data.binance.vision/data/futures/um/monthly/klines/NVDAUSDT/1h/NVDAUSDT-1h-2026-05.zip) | 4b9184b4b71163b2a9cc244b049313157fea17207b0a91375496de9b01c7d58a | 2026-11-13T17:12:49.603263+00:00 |

分钟和日线来源记录（包括官方 CHECKSUM）：

| 对象 | SHA-256 | 字节数 | 取得时间 UTC |
| --- | --- | --- | --- |
| [NVDAUSDT-1m-2026-05-15.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-15.zip) | c11b7e958214c3d29f37e4231f488b74b77966aa1955c025556f2b4860f65341 | 52539 | 2026-11-19T10:46:49.048715+00:00 |
| [NVDAUSDT-1m-2026-05-15.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-15.zip.CHECKSUM) | 8cbad43a2d0320c6a3ddee4ddf58a98a2eab6ee92f6df66881771afc2164b272 | 93 | 2026-11-19T10:46:49.274347+00:00 |
| [NVDAUSDT-1d-2026-05-15.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-15.zip) | 83a952918ebe2de0669ac945d5343817ff963cfc4187fb338b428e1add55621c | 352 | 2026-11-19T10:46:49.456530+00:00 |
| [NVDAUSDT-1d-2026-05-15.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-15.zip.CHECKSUM) | 419ce5d9a8637c2aaa3120ed9d20f5082361d790e361489806240091a3759898 | 93 | 2026-11-19T10:46:49.623711+00:00 |
| [NVDAUSDT-1m-2026-05-16.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-16.zip) | 5e2fd1e349e7e193a66d131e154ef03404d7b0d8610bd8249c2124019f6b3b56 | 44630 | 2026-11-19T10:46:50.891729+00:00 |
| [NVDAUSDT-1m-2026-05-16.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-16.zip.CHECKSUM) | e13f4fcacb6526ef121a7126efbf792304dfd71d75a1a29b7886c80f7e6c4c46 | 93 | 2026-11-19T10:46:51.418951+00:00 |
| [NVDAUSDT-1d-2026-05-16.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-16.zip) | 2ff5560f74a5cf0a191a154424f9a9c5cb89c110d20fc3a0939d457789e61e4d | 348 | 2026-11-19T10:46:52.596722+00:00 |
| [NVDAUSDT-1d-2026-05-16.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-16.zip.CHECKSUM) | 968e75aaa9e366edf8e631b3e0b46cdc1b964f2efdc38ea0eea000a94779e96d | 93 | 2026-11-19T10:46:53.843287+00:00 |
| [NVDAUSDT-1m-2026-05-17.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-17.zip) | c73cc45267106888f656e0fb9dc81b8855b8da5856d90f3d76b726ab0c0731da | 44254 | 2026-11-19T10:46:55.094040+00:00 |
| [NVDAUSDT-1m-2026-05-17.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-17.zip.CHECKSUM) | a305bef50dc481ac48b42d16b1545525ed9bf0152588b2aa92f6f60b57170503 | 93 | 2026-11-19T10:46:55.280511+00:00 |
| [NVDAUSDT-1d-2026-05-17.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-17.zip) | a20205915355fcb0439e3bc9a9baa7157cabbc5a4281f11483075ea7d5cc79d1 | 350 | 2026-11-19T10:46:55.552496+00:00 |
| [NVDAUSDT-1d-2026-05-17.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-17.zip.CHECKSUM) | 9e5aab31f67e3de068701eb048fa37c616cb8184bb4942c4358b49961ec09653 | 93 | 2026-11-19T10:46:56.710489+00:00 |
| [NVDAUSDT-1m-2026-05-18.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-18.zip) | 01ba1779770dac0029f1568a59a5ff2149b43e50a8a11e371c6fb8ecc21cdc24 | 51749 | 2026-11-19T10:46:56.991298+00:00 |
| [NVDAUSDT-1m-2026-05-18.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-18.zip.CHECKSUM) | 6775bc8afb88dfbb3cb003b84eb34fe200e014b9167d3f61bea75dad40bb1098 | 93 | 2026-11-19T10:46:58.330772+00:00 |
| [NVDAUSDT-1d-2026-05-18.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-18.zip) | dbe5891652779f9b2e4f714afeb98a311aaae89caee06dd6afbe0998b94a6ac6 | 354 | 2026-11-19T10:46:58.499632+00:00 |
| [NVDAUSDT-1d-2026-05-18.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-18.zip.CHECKSUM) | 4573cb8dea2ef9aa1fa954577c1438d49f546af4d17ccc5789babfdbe78fea46 | 93 | 2026-11-19T10:46:58.719794+00:00 |
| [NVDAUSDT-1m-2026-05-19.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-19.zip) | 7f0998f754636cb4a21537d4ad4bc74d132bafb35aa1abc9790bb4445597501e | 50911 | 2026-11-19T10:46:58.879894+00:00 |
| [NVDAUSDT-1m-2026-05-19.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-19.zip.CHECKSUM) | aaff34a6a40431dbaa4018f3f42ba2845cfa0e435812a3f05e9dc3a8cd3e0996 | 93 | 2026-11-19T10:46:59.132891+00:00 |
| [NVDAUSDT-1d-2026-05-19.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-19.zip) | 6725af53791e7cd1015e60dc40408936dfe7e67cf101fcba9f318932b8f0d305 | 351 | 2026-11-19T10:47:00.297753+00:00 |
| [NVDAUSDT-1d-2026-05-19.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-19.zip.CHECKSUM) | eec55976340ca08f8554920238f6336b51688420f87f39d4645b0c41c61242f8 | 93 | 2026-11-19T10:47:00.453905+00:00 |
| [NVDAUSDT-1m-2026-05-20.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-20.zip) | 121f993881021fcc45472bf5c1f9433013ef94e4aa9757c22f06f7b9f3473684 | 52692 | 2026-11-19T10:47:01.709073+00:00 |
| [NVDAUSDT-1m-2026-05-20.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-20.zip.CHECKSUM) | 053ff3e1deaeaf2742b72aeb819aa9801f2beebac92d3843bd639c87e82b5858 | 93 | 2026-11-19T10:47:03.059296+00:00 |
| [NVDAUSDT-1d-2026-05-20.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-20.zip) | 483c4a8cf7150971eb8b27d545ac347db992d6ac3420e72af2a13ef0bf1e9ba2 | 352 | 2026-11-19T10:47:03.294229+00:00 |
| [NVDAUSDT-1d-2026-05-20.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-20.zip.CHECKSUM) | f7b4d0f424ac388fe8685f2afdbc6ae45863d960108d2507e0527c775eabd54a | 93 | 2026-11-19T10:47:03.429561+00:00 |
| [NVDAUSDT-1m-2026-05-21.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-21.zip) | b6544ae73bc1118f861a042206adf45e218014e8f2f4ad00787490712f0fbc18 | 51693 | 2026-11-19T10:47:03.798464+00:00 |
| [NVDAUSDT-1m-2026-05-21.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-21.zip.CHECKSUM) | f45edc5b9b966df06c358fc09ab567835d44e796fb0745f0c6b0c5c06af4b000 | 93 | 2026-11-19T10:47:04.050350+00:00 |
| [NVDAUSDT-1d-2026-05-21.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-21.zip) | 5fb29f07c49248217034f901a5da5a1aaa388003366e095ac877872522f5c965 | 350 | 2026-11-19T10:47:04.220155+00:00 |
| [NVDAUSDT-1d-2026-05-21.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-21.zip.CHECKSUM) | 2027b21f379b28bcba180c416414f6f679bbe62a1545da4fa2ddba459a55ac6d | 93 | 2026-11-19T10:47:04.380405+00:00 |

## I2（独立历史窗口）：[2026-05-22T00:00:00+00:00, 2026-05-29T00:00:00+00:00) UTC

共 10080 根分钟线、168 根研究小时线、7 根独立日线，另有前置小时 `2026-05-21T23:00:00+00:00`。锚点收盘为 220.07 USDT。

| 核对 | OHLCV 一致桶数 | 辅助字段差异 |
| --- | --- | --- |
| 60 分钟 → 认证小时 | 168 | {"quote_volume": 0, "taker_buy_quote_volume": 0, "taker_buy_volume": 0, "trade_count": 0} |
| 24 小时 → 独立日线 | 7 | {"quote_volume": 0, "taker_buy_quote_volume": 0, "taker_buy_volume": 0, "trade_count": 0} |

R04B data version：`352d960fad24e50f1423ff173d838a994a29603d2629652f46c7edc76e5e2a83`；bars SHA-256：`16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab`；取得时间：`2026-11-13T17:12:50.891167+00:00`。

实际引用的原始小时对象：

| 官方 URL | SHA-256 | 取得时间 UTC |
| --- | --- | --- |
| [小时 ZIP](https://data.binance.vision/data/futures/um/monthly/klines/NVDAUSDT/1h/NVDAUSDT-1h-2026-05.zip) | 4b9184b4b71163b2a9cc244b049313157fea17207b0a91375496de9b01c7d58a | 2026-11-13T17:12:49.603263+00:00 |

分钟和日线来源记录（包括官方 CHECKSUM）：

| 对象 | SHA-256 | 字节数 | 取得时间 UTC |
| --- | --- | --- | --- |
| [NVDAUSDT-1m-2026-05-22.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-22.zip) | a81d0a5b51f0a5ff5f81fb3477661b3baecd9718045a3b749959c080c02ae178 | 49662 | 2026-11-19T10:47:04.607483+00:00 |
| [NVDAUSDT-1m-2026-05-22.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-22.zip.CHECKSUM) | b9a00e42d07f91812cd9345acc229ab38f5f9d1d8aeda6574d8a667f3ac579ab | 93 | 2026-11-19T10:47:05.858264+00:00 |
| [NVDAUSDT-1d-2026-05-22.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-22.zip) | 0649f77c5e3fadc22ac3d1a9c57775f130215e7383e6137a54d4cc12f79a9506 | 348 | 2026-11-19T10:47:06.009215+00:00 |
| [NVDAUSDT-1d-2026-05-22.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-22.zip.CHECKSUM) | eae05516a7aba70017eab0369fa7c9d2b4d9c38f458829a20bd9474f577218f7 | 93 | 2026-11-19T10:47:06.161585+00:00 |
| [NVDAUSDT-1m-2026-05-23.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-23.zip) | ad4694ded8cba8734a60225bbbc4140c5812ff03335609fe26a0f0c4fc856bca | 46628 | 2026-11-19T10:47:06.431855+00:00 |
| [NVDAUSDT-1m-2026-05-23.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-23.zip.CHECKSUM) | 13483e80f78e53edb3ebfa662f1a8e251fc712d49dcfe764942dfce050f24fa5 | 93 | 2026-11-19T10:47:06.694552+00:00 |
| [NVDAUSDT-1d-2026-05-23.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-23.zip) | 231bac1853b72ffdf3f63be73e34e6f05cb7bb622349072a9cb81eb49e7a744b | 351 | 2026-11-19T10:47:06.843925+00:00 |
| [NVDAUSDT-1d-2026-05-23.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-23.zip.CHECKSUM) | f74ccb64b8f845602e6e51242f1e8f315c1fa8d0e86b37f4ab85a009c898b661 | 93 | 2026-11-19T10:47:07.365754+00:00 |
| [NVDAUSDT-1m-2026-05-24.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-24.zip) | f664fd919da32e72978373c64b93fb9796a1edef567082b3b86a025c8660a599 | 45792 | 2026-11-19T10:47:07.540620+00:00 |
| [NVDAUSDT-1m-2026-05-24.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-24.zip.CHECKSUM) | 8847bca93795299a3a95152791fc9d9a71741a172157bf0fc92c1c11a86e1406 | 93 | 2026-11-19T10:47:07.865679+00:00 |
| [NVDAUSDT-1d-2026-05-24.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-24.zip) | e19793860ed3d84da2df2f7c90926ff69ab9c1217a1771e9a54d6ce621221713 | 348 | 2026-11-19T10:47:08.048041+00:00 |
| [NVDAUSDT-1d-2026-05-24.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-24.zip.CHECKSUM) | a313cff9f7df3c45c2543625a2216b61f5ffe3ab37978bed8b94f221d6371b9b | 93 | 2026-11-19T10:47:08.211327+00:00 |
| [NVDAUSDT-1m-2026-05-25.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-25.zip) | 32e22a2ccca441484781c3caad4d7816be1f3ae68c82791b443327d777b15e83 | 45147 | 2026-11-19T10:47:08.388472+00:00 |
| [NVDAUSDT-1m-2026-05-25.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-25.zip.CHECKSUM) | fba045b18728799f0c9fbd4144d8eb1c33b06c404f256a8718a692c92e2bb483 | 93 | 2026-11-19T10:47:08.666940+00:00 |
| [NVDAUSDT-1d-2026-05-25.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-25.zip) | 2bb1965b18efe3fa15092eece0b6d4a6e4faf1444b7dc84240ea5db07ece02f8 | 350 | 2026-11-19T10:47:09.851474+00:00 |
| [NVDAUSDT-1d-2026-05-25.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-25.zip.CHECKSUM) | 8e4831b8f0bab6b216452de4eef4be97a7418413c05f548bc0c69dcedae0eda1 | 93 | 2026-11-19T10:47:11.041198+00:00 |
| [NVDAUSDT-1m-2026-05-26.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-26.zip) | 9ae4009b28d08dd8f1e9f9ae756ff65ce88f42866d877a4b89e8b6c4417b13fa | 49041 | 2026-11-19T10:47:12.326375+00:00 |
| [NVDAUSDT-1m-2026-05-26.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-26.zip.CHECKSUM) | a2796f668f3724953fbd43d7ecc2f92d5da1cee21d7234af6d303c4267fcefaf | 93 | 2026-11-19T10:47:13.848543+00:00 |
| [NVDAUSDT-1d-2026-05-26.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-26.zip) | 6939ec32ac736b5911e02d66cb4a0fd3bcd516509b9ff1ef7fb7e52aaeb79b44 | 351 | 2026-11-19T10:47:14.010847+00:00 |
| [NVDAUSDT-1d-2026-05-26.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-26.zip.CHECKSUM) | 39b47456a5d1ae9ec85774f6808fb3d91bd042d97597542c91f743e03f0efab4 | 93 | 2026-11-19T10:47:15.279011+00:00 |
| [NVDAUSDT-1m-2026-05-27.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-27.zip) | a24c98e3ff119f2e42f335a4e171093a8ff5087f7bcd70e5ff15a065f7de0fa0 | 50141 | 2026-11-19T10:47:15.528803+00:00 |
| [NVDAUSDT-1m-2026-05-27.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-27.zip.CHECKSUM) | b6c79cd9ef1f7c2ad3dd84c306aad248dd0cf51dbd71f0ecba3a54128a2b8ecf | 93 | 2026-11-19T10:47:15.891769+00:00 |
| [NVDAUSDT-1d-2026-05-27.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-27.zip) | fae8a22aa16698d43650f0ea30599c18771c7d3db1883b737e5609401090d53b | 349 | 2026-11-19T10:47:16.195784+00:00 |
| [NVDAUSDT-1d-2026-05-27.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-27.zip.CHECKSUM) | d0bb37caafbf97b08db6744fdb479a134ecafbc20a6b91995ceb40c4f9ea858c | 93 | 2026-11-19T10:47:16.384678+00:00 |
| [NVDAUSDT-1m-2026-05-28.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-28.zip) | 866c69089fcb97ecb6f81107a0bc52a14e2719e092cfc3a3941c4976b5858f8a | 50047 | 2026-11-19T10:47:16.540099+00:00 |
| [NVDAUSDT-1m-2026-05-28.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-28.zip.CHECKSUM) | 72e9807ebdd881388e042cf72bf35d654df28ec49c46bbd396a613c65b6920af | 93 | 2026-11-19T10:47:16.712072+00:00 |
| [NVDAUSDT-1d-2026-05-28.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-28.zip) | 26ad4430e549e15cdb7383dce0538178ee76a3022c8fd35e8df880834e7d3ebb | 353 | 2026-11-19T10:47:17.884718+00:00 |
| [NVDAUSDT-1d-2026-05-28.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-28.zip.CHECKSUM) | ab51f94e20a45313f8fce20cf1867a811650a7fe76e2da0d11128e889e0fed8f | 93 | 2026-11-19T10:47:18.051034+00:00 |

## I3（独立历史窗口）：[2026-05-29T00:00:00+00:00, 2026-06-05T00:00:00+00:00) UTC

共 10080 根分钟线、168 根研究小时线、7 根独立日线，另有前置小时 `2026-05-28T23:00:00+00:00`。锚点收盘为 214.25 USDT。

| 核对 | OHLCV 一致桶数 | 辅助字段差异 |
| --- | --- | --- |
| 60 分钟 → 认证小时 | 168 | {"quote_volume": 0, "taker_buy_quote_volume": 0, "taker_buy_volume": 0, "trade_count": 0} |
| 24 小时 → 独立日线 | 7 | {"quote_volume": 0, "taker_buy_quote_volume": 0, "taker_buy_volume": 0, "trade_count": 0} |

R04B data version：`352d960fad24e50f1423ff173d838a994a29603d2629652f46c7edc76e5e2a83`；bars SHA-256：`16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab`；取得时间：`2026-11-13T17:12:50.891167+00:00`。

实际引用的原始小时对象：

| 官方 URL | SHA-256 | 取得时间 UTC |
| --- | --- | --- |
| [小时 ZIP](https://data.binance.vision/data/futures/um/monthly/klines/NVDAUSDT/1h/NVDAUSDT-1h-2026-05.zip) | 4b9184b4b71163b2a9cc244b049313157fea17207b0a91375496de9b01c7d58a | 2026-11-13T17:12:49.603263+00:00 |
| [小时 ZIP](https://data.binance.vision/data/futures/um/monthly/klines/NVDAUSDT/1h/NVDAUSDT-1h-2026-06.zip) | bf3abc351a5d186be3d126ecd080e8b21c8d1aeca785bef34ebb3f3432afb03c | 2026-11-13T17:12:49.994384+00:00 |

分钟和日线来源记录（包括官方 CHECKSUM）：

| 对象 | SHA-256 | 字节数 | 取得时间 UTC |
| --- | --- | --- | --- |
| [NVDAUSDT-1m-2026-05-29.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-29.zip) | e34a0565ebb9547b25cd8ba1ab209a3db64e5ec5ce267a9b57f8de9b0ac8f811 | 49254 | 2026-11-19T10:47:18.336422+00:00 |
| [NVDAUSDT-1m-2026-05-29.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-29.zip.CHECKSUM) | 2376399a9acac180097e79e29dc4b97bdad305e4fcc8b4823fec90bc68780756 | 93 | 2026-11-19T10:47:18.885757+00:00 |
| [NVDAUSDT-1d-2026-05-29.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-29.zip) | be18c291e3baeba1952a0f0e2bcce4289f3170a63f7b4cbfaff9de8ed5a6be6f | 352 | 2026-11-19T10:47:19.127914+00:00 |
| [NVDAUSDT-1d-2026-05-29.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-29.zip.CHECKSUM) | 0497f9cceeb01ebd3826fe2b78c8fd8b7836df32bc333f9fd4b4638d1e8c3c73 | 93 | 2026-11-19T10:47:20.596236+00:00 |
| [NVDAUSDT-1m-2026-05-30.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-30.zip) | 26267756d5856667370597425234cf2313f297d76526c64c083de63842436d71 | 42558 | 2026-11-19T10:47:21.752224+00:00 |
| [NVDAUSDT-1m-2026-05-30.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-30.zip.CHECKSUM) | b5ca8a74271f2a80b049399c7a1b11c2fbff3bff385174495a028bcd18507519 | 93 | 2026-11-19T10:47:22.179938+00:00 |
| [NVDAUSDT-1d-2026-05-30.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-30.zip) | 31236b9345b0026db2c8216f5ad94a9819e452ef3a85a784c58bb373143f3fcc | 347 | 2026-11-19T10:47:22.681326+00:00 |
| [NVDAUSDT-1d-2026-05-30.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-30.zip.CHECKSUM) | a52acc9d98778ffa1b9b09f494c21bc63f417477259bf8bfb71f8584483151e8 | 93 | 2026-11-19T10:47:22.935612+00:00 |
| [NVDAUSDT-1m-2026-05-31.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-31.zip) | 97a75dc3ae588dab2e558f9fb84f2cff0c4abb1b2751340ed47215c02fa3250b | 45929 | 2026-11-19T10:47:23.111247+00:00 |
| [NVDAUSDT-1m-2026-05-31.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-05-31.zip.CHECKSUM) | 63b8a0caa99d4480e764ba4faaca36463cec3216350f9269e6356eede80f02de | 93 | 2026-11-19T10:47:23.519242+00:00 |
| [NVDAUSDT-1d-2026-05-31.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-31.zip) | 7c99dbe76bfc9df17c9e1bd83c147bf2aba80f9cff797f8fd318e99bbb157468 | 349 | 2026-11-19T10:47:23.676141+00:00 |
| [NVDAUSDT-1d-2026-05-31.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-05-31.zip.CHECKSUM) | e97058e358c78b45815ac54e9550f112af118cbdc0360e61adda3e80acf7a4ac | 93 | 2026-11-19T10:47:23.832593+00:00 |
| [NVDAUSDT-1m-2026-06-01.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-06-01.zip) | f85016da4e3eb3e9e553d973a11ec0a78f6ff821ef156faeab9c9896d2007df7 | 52419 | 2026-11-19T10:47:24.230946+00:00 |
| [NVDAUSDT-1m-2026-06-01.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-06-01.zip.CHECKSUM) | be77769e5f4fe24bee1410a8bbc5ccaff6939831f96f5b4e175089cfe4441c47 | 93 | 2026-11-19T10:47:24.640337+00:00 |
| [NVDAUSDT-1d-2026-06-01.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-06-01.zip) | 2dc7b8a4095f15c454c8547d41d84ea02da78023bbb24f8ec6a1f392cd15c899 | 353 | 2026-11-19T10:47:24.824939+00:00 |
| [NVDAUSDT-1d-2026-06-01.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-06-01.zip.CHECKSUM) | 30a9123897bcdb9251fa00814f7f13356c4ccdd544b8feeb09af1b66ef9a0300 | 93 | 2026-11-19T10:47:25.080164+00:00 |
| [NVDAUSDT-1m-2026-06-02.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-06-02.zip) | 2133416f2efb3c683652e9a9b577e147670ad19f75df33e2e7ffac2fd8ffa816 | 53136 | 2026-11-19T10:47:25.228896+00:00 |
| [NVDAUSDT-1m-2026-06-02.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-06-02.zip.CHECKSUM) | c00d6fcb3e07eafc729115c225e8a5ee3b49c4408a96bc90a8884f8c76f6e7e8 | 93 | 2026-11-19T10:47:25.554957+00:00 |
| [NVDAUSDT-1d-2026-06-02.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-06-02.zip) | f01b7ffea02acd706e8a49a5c03d18477fb2019315eeb828879997937eab9868 | 354 | 2026-11-19T10:47:25.803447+00:00 |
| [NVDAUSDT-1d-2026-06-02.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-06-02.zip.CHECKSUM) | eebd578c6da4d6fcab42224541bc9eef86fed192d5861a8a847610d384127a03 | 93 | 2026-11-19T10:47:26.085176+00:00 |
| [NVDAUSDT-1m-2026-06-03.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-06-03.zip) | bdf853882765455e7c194c404bb5cc51f83d7943cf443e3b6dbf2767868e602b | 51596 | 2026-11-19T10:47:26.392841+00:00 |
| [NVDAUSDT-1m-2026-06-03.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-06-03.zip.CHECKSUM) | 6789b85f9f430845da9f9e8154305a279f993846ec5633e9815e71e1a488b23c | 93 | 2026-11-19T10:47:27.094326+00:00 |
| [NVDAUSDT-1d-2026-06-03.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-06-03.zip) | efaa3fff83594b44814766dfc9ad6ef6f467968d50b0c5404e0fe9ed736ee4e6 | 350 | 2026-11-19T10:47:27.317632+00:00 |
| [NVDAUSDT-1d-2026-06-03.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-06-03.zip.CHECKSUM) | 358de9d316ad9627000b5beab641ecf1ffe37cfd0724d6185ce3fff60d617d4f | 93 | 2026-11-19T10:47:27.721177+00:00 |
| [NVDAUSDT-1m-2026-06-04.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-06-04.zip) | 8bca8ba9993082317886d8b9d36e381fa07bc6b57f7111ebe4df367825aad03c | 52098 | 2026-11-19T10:47:27.950768+00:00 |
| [NVDAUSDT-1m-2026-06-04.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-06-04.zip.CHECKSUM) | c49a5723e85671d3621962e5aacb49bd962c06b186789afed1d6c8e999c9171b | 93 | 2026-11-19T10:47:28.378969+00:00 |
| [NVDAUSDT-1d-2026-06-04.zip](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-06-04.zip) | 5276d85ebd7829c04ced1e920352051112b3ecbfc9e0ce09c5efc284579eeddd | 352 | 2026-11-19T10:47:28.570077+00:00 |
| [NVDAUSDT-1d-2026-06-04.zip.CHECKSUM](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-06-04.zip.CHECKSUM) | 3b2a9fa65d9ada68f68a39305db7918e70eb50c51c9849883ac1bbe55bc40076 | 93 | 2026-11-19T10:47:28.816045+00:00 |

分钟数据按原生 UTC 顺序校验完整、闭合、无重复和缺桶；不补值。价格 OHLC 与 base volume 精确比较，quote volume、交易笔数及主动买入字段按归档语义记录差异。原始压缩行情直接读取，保留在仓库外；[AUDIT.json](AUDIT.json) 保存各对象 URL、摘要、取得时点、长度、服务器时间与数据版本。原 F04C 窗口和证据保持冻结。
