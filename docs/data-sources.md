# 数据源实测笔记

记录**在这台机器上实际验证过的**行为，不是文档里写的行为。
新增数据源或某个接口突然不работ时，先看这里。

最后验证：2026-08-10

---

## 各源的角色

| 源 | 角色 | 为什么 |
|---|---|---|
| **BaoStock** | 主源 | 唯一不靠爬网页的免费 A股源。自建服务器 + SDK 直连，量大也不会被判定成爬虫 |
| **AKShare** | 元数据 + 备源 | 覆盖最全，但底层爬网页。**只用它拉低频元数据**，不用它拉全量日K |
| **Mootdx** | 第三备胎 | 直连通达信。无复权因子、无停牌/ST 标志、回溯深度有限。只适合补最近几天的洞 |
| Tushare | 未启用 | 新账号 0 积分，日线要 120 积分起且只给不复权 |

---

## 已验证的接口

### BaoStock — 可用

```python
bs.query_history_k_data_plus(
    "sh.600519",
    "date,open,high,low,close,volume,amount,tradestatus,isST",
    start_date="2026-07-20", end_date="2026-08-08",
    frequency="d", adjustflag="3")   # 1=后复权 2=前复权 3=不复权
```

- 字段名与文档一致
- `tradestatus`：`"1"` = 正常交易，`"0"` = 停牌
- `isST`：`"1"` = 是 ST
- `query_trade_dates` 返回 `calendar_date` / `is_trading_day`（`"1"`/`"0"`）
- **成交量单位是股**。实测对账：`4268859 股 × ~1311 元 ≈ amount 5.6e9` ✓

**坑 1 —— `bs.login()` 往 stdout 打 `"login success!"`。**
本项目的 stdout 是 JSONL 日志流，一行裸文本会让日志文件不再是合法 JSONL，
直接破坏 store 层的重建能力。`baostock_source._ensure_login` 用
`redirect_stdout` 吞掉它。

**坑 2 —— 全局会话，非线程安全。**
它用一个模块级 socket，多线程并发查询会让响应交错、数据互相串台
**且不报错**。所以 `_LOCK` 把所有查询串行化，`01_ingest.py` 默认不开并发。
真正省时间的是增量缓存，不是并发。

**坑 3 —— 不给复权因子。**
`query_history_k_data_plus` 只能给某一种复权口径的价格。
`query_adjust_factor` 给的是除权除息事件，要自己前向填充，边界很多。
本项目改用：拉一次不复权 + 拉一次后复权，`factor = hfq_close / raw_close`。
多一次查询，换来一个自洽、可校验的因子。实测茅台 factor ≈ 7.669（常数，
窗口内无除权事件时不变）。

### AKShare — 部分可用

| 接口 | 状态 | 用途 |
|---|---|---|
| `stock_zh_a_hist` | ✅ 可用 | 备源日线。走 `push2his.eastmoney.com` |
| `stock_info_a_code_name` | ✅ 可用 | 全市场代码名称表（5538 条）。走交易所官网 |
| `index_stock_cons_csindex` | ✅ 可用 | 沪深300 成分（300 条）。走中证指数官网 |
| `sw_index_first_info` | ✅ 可用 | 申万一级行业列表（31 个） |
| `index_component_sw` | ✅ 可用 | 申万行业成分股 |
| `tool_trade_date_hist_sina` | ✅ 可用 | 交易日历备源 |
| `stock_board_industry_name_em` | ❌ **不可达** | 东财行业 |
| `stock_zh_a_spot_em` | ❌ **不可达** | 实时快照 |
| `stock_individual_info_em` | ❌ **不可达** | 个股信息 |

#### ⚠️ 东财 `push2*` 主机不可达

```
stock_board_industry_name_em → ProxyError: 17.push2.eastmoney.com
stock_zh_a_spot_em           → ProxyError: 82.push2.eastmoney.com
stock_individual_info_em     → ProxyError: push2.eastmoney.com
```

设 `NO_PROXY=eastmoney.com` 后错误从 `ProxyError` 变成
`ConnectionError: RemoteDisconnected`——说明**不是代理配置问题，是连接被
主动重置**（本机没有设 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量，可能是系统级
TUN 模式代理拦截，或东财封了这个出口 IP）。

而 `push2his.eastmoney.com`（历史行情）**正常**。

**设计后果**：本项目的任何路径都不得依赖东财的实时接口。
- 行业分类 → 走申万（`index_component_sw`），已实现
- 股票池 → 走中证（`index_stock_cons_csindex`），已实现
- 实时快照 → **没有替代方案**。所幸半自动模式盘后跑，不需要实时价

**P7 风险**：TradingAgents 复核层要用的 `stock_news_em` / `stock_notice_report`
走的是别的东财主机（`search-api-web.eastmoney.com`），**尚未验证**。
到 P7 时先探测，不可用的话情绪面和新闻面要另找源。

#### ⚠️ 成交量单位是"手"，不是"股"

```
BaoStock  600519  volume=4268859 股 × ~1311 元  ≈ amount 5.6e9    ✓
AKShare   600519  volume=106151 手 × 100 × ~1309 ≈ amount 1.39e10  ✓
```

同一只票在两个源之间**成交量差 100 倍，且不会报任何错**。任何量价因子
（换手率、量比、成交额加权）都会被静默污染。`akshare_source` 统一乘 100
转成股。

对账办法：`amount / volume` 应当落在当日 `close` 附近（实测三只票比值
0.989–0.999）。这个不变式值得在任何新增数据源时都跑一遍。

#### ⚠️ 股票名称写法不一致

```
stock_info_a_code_name    →  '万  科Ａ'   ← 两个半角空格 + 全角 Ａ
index_stock_cons_csindex  →  '万科A'      ← 无空格 + 半角 A
```

直接字符串相等比对，这只票**永远查不到**。`meta.norm_name()` 做
NFKC 折叠（全角→半角）+ 去所有空白（含 U+3000 全角空格）+ 大写。

**这是 P5 的关键路径**：手机 APP 持仓页只显示中文名不显示代码，
名字对不上持仓就进不了系统。

#### tqdm 进度条

`stock_info_a_code_name` 会打进度条。它走 **stderr**，不污染 stdout 的
JSONL 日志（已实测：179 行日志，0 行非法 JSON）。终端里看着乱，但日志文件
是干净的。

---

## 复权口径

| 用途 | 口径 |
|---|---|
| 模型训练、回测 | **后复权**（`cache.hfq(df)`）—— 价格连续，除权日不会出现假跌 |
| 下单清单的限价和金额 | **不复权**（`df` 本身）—— 与 APP 里看到的一致 |
| qlib bin | 因子归一到**最后一天 = 1**（qlib CN 约定），于是 `$close` 在最后一天等于真实成交价 |

parquet 里存的是**原始价 + 后复权因子**（qlib 的存储约定），不存两套价格：
因子本身可校验（单调不减、恒正），存两套价格反而容易出现两边不一致却发现
不了。

各源的后复权锚点不同，同一只票的 factor 数值可以不一样（AKShare 和 BaoStock
就不同）。这不影响正确性——factor 对某只票是常数倍，收益率序列完全一样。

### ⚠️ BaoStock 的后复权因子会出现伪造下降

**后复权因子只会向上跳**（分红送股不断累积），它存在的唯一目的就是抵消
原始价的除权缺口。所以**因子在原始价正常波动的日子发生变化，这个变化必定
是假的**。

实测（2026-08-10，沪深300 全量 304 只）有 4 只中招：

| 代码 | 日期 | 因子 | 伪造单日收益 | 形态 |
|---|---|---|---|---|
| 000001.SZ 平安银行 | 2020-12-31 | 119.96 → 99.79 | **+16.94%** | 持久平移 |
| 000002.SZ 万科A | 2020-11-19 | 115.09 → 112.15 | +2.57% | 一日凹陷 |
| 600372.SH | 2020-11-19 | — | +0.41% | 持久平移 |
| 601607.SH | 2020-11-27 | — | +0.78% | 持久平移 |

平安银行 2020-12-31 原始价 19.20 → 19.34（**+0.73%**），复权后却变成
**−16.21%** —— 一根凭空造出来的假阴线。不处理的话它直接进模型训练集。

**换 `query_adjust_factor` 解决不了**：BaoStock 自己的权威因子表里就带着
这个下降（已实测确认），不是我们用 `hfq_close / raw_close` 推导出来的假象。
用独立的窄区间查询交叉验证，返回同样的值。

**处理**：`cache.repair_factor()`，两种故障两种修法——

* **一日凹陷**（次日原样恢复，如万科）：把那天的异常值换成前一天的值。
  没有任何公司行为是"掉一天又弹回来"的形状。
* **持久平移**（掉下去不回来，如平安银行）：多半是数据源在某个时点换了
  复权基准，把两段不同锚点的序列拼在了一起。把**断点之后的整段**按比例
  抬回断点之前的水平——段内相对变化完全不变（因子是常数倍，收益率不受
  影响），断点当天的比值变成 1.0，伪造收益随之消失。
  *不能简单钳住*：那会把好几年的分红累积压平成一个平台，然后再冒出一个
  假跳升。

`cache.read()` **默认修复**（没有任何下游想要一个错的因子），
`read(repair=False)` 取原样。**落盘的永远是源的原样**，修复只发生在读出来
之后——这样源的问题始终可见、可审计，不会被我们的修复悄悄掩盖。

因此 `01_ingest.py` 里的自洽校验必须用 `cache.read(code, repair=False)`，
否则这条告警永远不会触发。

---

## 数据自洽校验

`cache.verify()` 抓的是**不抛异常但会毁掉下游**的那类错误：

- `date` 非递增 / 有重复 → 合并逻辑出问题
- `factor` 非正 / 递减 → 后复权因子只会因分红送股累积上升，递减说明源的口径中途变了
- 非停牌日出现非正价格 → 停牌日的 0 价没被正确标记
- `high < low` → 源的数据本身有问题

`01_ingest.py` 每只票拉完都跑一遍，问题进日志也进终端摘要。

---

## 环境

```
conda env qbg (python 3.11.15)
baostock 0.9.3 · akshare 1.18.84 · mootdx 0.11.7
pandas 2.3.3 · pyarrow 25.0.0 · numpy 2.4.6
```

BaoStock 日K 收盘后约 17:30 发布，复权因子约 18:00。
所以 `run_daily.bat` 应排在 **17:45 之后**。
