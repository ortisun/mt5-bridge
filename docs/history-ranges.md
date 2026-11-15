# Bars and ticks by date range

Bars and ticks are asked for by date range, never by quantity: bars by a
range and a timeframe, ticks by a range. The bridge cuts the range into
pages, so no single call asks the terminal for an answer of unbounded size.

| | REST | gRPC |
| --- | --- | --- |
| Bars | `GET /v1/bars` | `MarketData.ListBars` |
| Ticks | `GET /v1/ticks` | `MarketData.ListTicks` |

Both ranges start inclusive and end exclusive, in UTC: `from_epoch` and
`to_epoch` in seconds for bars, `from_time_msc` and `to_time_msc` in
milliseconds for ticks. They are required. A page holds at most 1000 items;
the size is the bridge's, not a parameter. `GET /v1/ticks/latest` is the
current quote of a symbol.

Order and deal history already take a range (`GET /v1/history/orders`,
`GET /v1/history/deals`) and are not paged.

## Bars: the page count is known from the dates

Two bars of one timeframe open at least one bar length apart. A time window
of 1000 bar lengths therefore holds at most 1000 bars, and the bridge cuts
the range into such windows:

```
total_pages = ceil((to_epoch - from_epoch) / (1000 × bar length))
```

`total_pages` is in every answer and is the same for every page, so a client
knows how many calls a range takes after the first one, and can read pages in
any order or in parallel.

```
GET /v1/bars?symbol=WINZ25&timeframe=M1&from_epoch=1763024400&to_epoch=1763629200&page=1
```

```json
{
  "symbol": "WINZ25",
  "timeframe": "M1",
  "from_epoch": 1763024400,
  "to_epoch": 1763629200,
  "page": 1,
  "total_pages": 11,
  "next_page": 2,
  "bars": [{ "time": 1763024400, "open": "128500", "...": "..." }]
}
```

Read page 1, then every page up to `total_pages`, or follow `next_page` until
it is null.

- A page is a window of time, not a fixed number of bars. Over a night, a
  weekend or a holiday it holds few bars or none. An empty page is not the
  end of the range; only `next_page: null` is.
- Only closed bars are returned. A range that reaches the present leaves the
  bar still forming out.
- For `MN1` the bar length is taken as 28 days, the shortest month.
- The terminal serves bars only as far back as its own history ("Max. bars
  in chart" in the terminal's options, and what the broker keeps). Older
  windows are empty pages, not an error.

## Ticks: follow the cursor

The number of ticks in a period is not known before reading them, so a tick
range has no page count. Each answer carries the cursor of the next page:

```
GET /v1/ticks?symbol=WINZ25&from_time_msc=1763024400000&to_time_msc=1763028000000
```

```json
{
  "symbol": "WINZ25",
  "from_time_msc": 1763024400000,
  "to_time_msc": 1763028000000,
  "next_cursor": "1763024467000.42",
  "ticks": [{ "time_msc": 1763024400012, "bid": "128500", "...": "..." }]
}
```

Repeat the request with `cursor=<next_cursor>` and the same `symbol`,
`from_time_msc` and `to_time_msc` until `next_cursor` is null.

- A page can hold fewer than 1000 ticks, or none, and still have a
  `next_cursor`.
- Treat the cursor as opaque text. It is a position in the terminal's tick
  history (a second and how many ticks of it were read), so it stays valid
  for as long as that history does not change; ticks of past sessions do not.
- Several ticks can share one `time_msc`. The cursor counts them, so none is
  lost or repeated at a page boundary.

## What has been tried

The paging runs in `core/history_paging.py` and is tested over the fake
terminal (`tests/core/test_history_paging.py`,
`tests/api/test_history_range.py`). Not tried: the real terminal's
`copy_rates_range`, and how long it takes to answer a full page of 1000
bars or ticks.
