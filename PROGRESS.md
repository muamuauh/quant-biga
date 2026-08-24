# 进度记录

> 接手前先读 `plan.md` 和 `CLAUDE.md`。最后更新：2026-08-21。

## 下次开工：先做这个

**第一次验证已于 2026-08-24 完成**（见下方「P9 第一次实盘化验证」）。
账户现在持有 **601398 工商银行 100 股**，当日买入，`可用余额=0`。

**下一个交易日**（可用余额会变成 100），跑：

```powershell
python toolsalidate_ths_p9.py --skip-build
```

这次卖单会被接受，验证**真正的卖出成交路径** —— 那是最后一块没验证的拼图。

两次都通过之后才做 **P9c**（`daily_cycle` 按 mode 选 adapter + 日报加
「计划 vs 实际委托」对账）。现在 `daily_cycle` 写死 `AdvisoryAdapter()`，
和 P9a 之前写死 `ManualSource()` 是同一类问题。

### 其他随时能跑的检查

```powershell
python tools\probe_ths.py --preflight-only   # 只做环境预检，不碰客户端
python tools\probe_ths.py                    # 只读四表 + 字段对照
python tools\probe_ths_order.py              # 下单表单输入路径（只填不交）
pytest -q                                     # 426 个离线测试，不需要同花顺
powershell -File scripts\preflight.ps1        # Clash 代理/TUN + qbg 环境预检
```

**同花顺升级后先跑前两条**：control_id 是硬编码的，重排控件会失效，
而且失效往往是静默的。

### 还欠着的两件事（与 P9 无关）

1. **行业中性化的结论翻了**，但有三个混淆因素（因子修复、universe 290→299、
   重训噪声），需要用 `tuning/` 的八项回测闸做受控评估。`QBG_INDUSTRY_NEUTRAL=1`
   保持未动。
2. **旧账户数据的残留**：`data/portfolio/` 和 `data/runs.db` 已按用户要求永久删除，
   但 `reports/`（2026-08-10 的日报/清单/复盘）和 `logs/qbg.jsonl`（3 行
   `cycle.completed` 带 account 字段）**没动**，`14_backfill_store.py --rebuild`
   能把旧数据重建回来。要彻底清需要一并处理。

---

## 一句话现状

P3–P6 已完成并用本机真实行情跑通：290 只股票训练的三 seed Rank IC 均为正，
顾问模式可一键生成下单清单、日报和可从 JSONL 完整重建的 SQLite 索引。P5 真实
截图与 P7/P8 中转在线验收均完成；P7 作为 fail-open 复核闸启用。P8 复盘模型无
文件、shell、数据库或下单权限；出站邮件通知与本机 SMTP 在线验收均已完成。

## 阶段进度

| 阶段 | 状态 | 结果 |
|---|---|---|
| P0 骨架 | ✅ | `qbg` 环境、配置、日志、三把锁 |
| P1 数据 | ✅ | BaoStock/AKShare 降级链、parquet、qlib dump；mootdx 因 httpx 冲突拆为独立可选源 |
| P2 规则/回测 | ✅ | T+1、整手、涨跌停、停牌、不对称费用 |
| P3 模型/选股 | ✅ | Alpha158 + LightGBM 三 seed；集成 Rank IC **+0.04234** |
| P4 风控/清单 | ✅ | 11 个闸结果（3 硬闸 + 订单闸）、SELL 保留、真实模型 dry-run |
| P5 OCR | ✅ | 真实南京证券截图 5 只持仓通过；当前/历史 CSV 已写入且哈希一致 |
| P6 编排/store | ✅ | 一键日循环；重建前后表计数一致 |
| P7 TA 复核 | ✅ | 全链路 9 候选在线通过；144 calls / 599,555 tokens，当前部署已启用且异常 fail-open |
| P8 复盘调参 | ✅ | ZenMux 真实 facts 复盘通过；报告/store 正常，未自动改参 |
| P9a 只读持仓 | ✅ | 已接入 daily_cycle；真实客户端联调通过，15 个离线测试 |
| P9b/P9c 下单与编排 | ⬜ | 待 P9a 稳定运行两周 |

## 2026-08-21 P9 选型与本机预检

- **easytrader vs THSAutoTrader 选定 easytrader**，理由见 `plan.md` §2.2.1。
  决定性一条：THSAutoTrader 的 `/xiadan` 没有价格参数（只有闪电买卖），
  与本项目"限价 + 涨跌停夹逼"冲突。
- 初版计划写的「easytrader 久未维护」**已证伪**：2026-02 仍在提交，
  PyPI 0.23.7（2025-04），10071 star。`plan.md` §2.2/§8.7 已更正。
- `easytrader 0.23.7 + pywinauto 0.6.6` 装入 `qbg`，**无依赖降级**。
- 新增 `tools/probe_ths.py`（只读探针，无下单代码路径）。
- 本机预检 **3 项未通过，测试未能完成**：
  1. `xiadan.exe` 以**管理员**运行而 Python 是普通权限 —— UIPI 放行读、禁止写
  2. `网上股票交易系统5.0` 窗口存在但 `visible=False`（未登录/最小化到托盘）
  3. `xiadan.exe` 32 位 vs Python 64 位（pywinauto 自己会警告，读未出问题）
- 实测确认：按 hwnd 连接可读控件（1012 资金余额读到真实值、323 个子控件），
  但 `connect(path=)` / `connect(process=)` 报 `ProcessNotFoundError`，
  模拟输入被拒。**能读 ≠ 能用**：easytrader 取表前必须用菜单导航，那是写操作。
- 用户 2026-08-21 确认：**暂不上 Linux 云服务器，一切以当前 Windows 机器为主**。
  `docs/deployment.md` 与 `run_daily.sh` 保留备用。

### 权限打通后的复测（同日下午，**未完成**）

用户已让 xiadan 以普通权限运行并退出精简模式，预检除位数外全绿。结果：

- ✅ `connect` / 菜单导航 / `balance`（走 control_id）稳定可用
- ❌ `position` / `today_entrusts` / `today_trades`（走 grid）**都取不到**
- 仅第一次成功过一次，读到 `today_entrusts` 2 行、12 列完整表头
- 三策略实测：`Xls` 0/9（`FileNotFoundError`，有次弹 `提示: Begin failed!`）；
  `Copy` 与 `WMCopy` 表面 9/9 但**是静默失败** —— 哨兵值证明 Ctrl+A/Ctrl+C
  根本没执行，返回的是把陈旧剪贴板解析出来的空列表
- **结论：取表的成功判据不能是"没抛异常"**，P9a 必须做肯定性校验

**根因已定位**（用户看到客户端反复弹框后指出）：同花顺对「拷贝数据」这个动作
本身有反爬验证码 —— `检测到您正在拷贝数据，为保护您的账号数据安全，请先输入验证码`，
模态框（`#32770`，图片 2405 / 输入框 2404 / 确定 1 / 取消 2）。
与表是否为空**无关**。我一度把 32/64 位当首要嫌疑，那是错的，已在 plan.md §2.2.4 更正。

- `Copy`/`WMCopy`：模态框挡着 → Ctrl+A/C 进不了 grid → 读陈旧剪贴板 → 静默空列表
- `Xls`：easytrader 把临时文件路径打进了验证码输入框（于是有 `验证码错误!!`），
  自己点掉取消 → 文件从未生成 → `FileNotFoundError`

**验证码识别是 P9a 的硬前置。** 选了 ddddocr（纯 pip、约 10 MB、无需装系统二进制）。

### 打通（同日傍晚）：四张表全部取到，字段映射全对上

光换 OCR 后端不够 —— easytrader 自带三个 grid 策略在同花顺 9.60.61 上**没有一个能用**
（`Xls` 零验证码处理；`Copy`/`WMCopy` 的 `title_re="验证码"` 走 `re.match` 锚定开头，
匹配不上「先输入验证码：」）。在 `tools/probe_ths.py` 里自实现 `CaptchaAwareCopy`
（`--grid auto`，默认），四个关键点见 plan.md §2.2.5：

1. 按**可见性**消歧 grid（本机 `1047` 有 6 个 `CVirtualGridCtrl`，只 1 个可见）
2. 找验证码框**不能用 `app.windows()`**（枚举不到），改裸 `EnumWindows` + 按控件结构认
3. 填验证码**必须 `type_keys`**，`set_edit_text` 走 WM_SETTEXT 不被认账；
   且两种方式 `window_text()` 回读都是空，**不能拿回读当校验**
4. **哨兵校验**剪贴板真被覆盖 —— 这是区分"表空"与"复制没发生"的唯一可靠判据

**实测结果**：ddddocr 抽查三张全对（`2603`/`9909`/`3169`）；`balance` / `position` /
`today_entrusts` / `today_trades` 全部取到；`Position` 需要的 7 个字段全部对上：

    证券代码→code  证券名称→name  股票余额→qty  可用余额→sellable_qty
    成本价→cost_price  市价→last_price  市值→market_value

注意是**「市值」不是「最新市值」**；**「可用余额」是相对截图 OCR 的真正增量**（T+1 闸要它）。

另两个坑：反爬验证码**异步**弹出（探针收尾轮询 6 秒清理）；同花顺**营销提示框**
也是模态的，按钮语义不安全（「立即重启」「前往普通下单」），清理一律 `WM_CLOSE`。

**还差一步**：当前是全新空仓模拟账户，只验证了**表头**映射；
取值格式与精度要等账户里真有持仓后再跑一次。

### P9a 落地（2026-08-21）

新增三个模块：`portfolio/ths_client.py`（Windows 取表层，第三方依赖全部**函数内
延迟导入**，Linux/CI 上 import 不会失败）、`portfolio/easytrader_source.py`
（表头→P5 payload→**复用 `validate_payload` 的六条校验**）、`portfolio/source.py`
（`load_portfolio()` 工厂 + 降级）。`tools/probe_ths.py` 改为 import 共享实现，
不再各存一份。

**顺带修掉一个既有的洞**：`daily_cycle` 此前**硬编码 `ManualSource()`**，
`QBG_PORTFOLIO_SOURCE` 存在但没人读；之前没暴露只因 ocr/manual 写同一份 CSV。

日报新增「持仓来源」段，降级时打醒目警告块 —— 静默降级比读失败更危险：
用过期持仓出的清单和正常清单长得一模一样。

真实客户端联调：`load_portfolio("easytrader")` 读到 18 列表头、7 个必需字段全对上、
校验通过。**待补**：账户里有持仓后验证取值格式与精度。

### P9b 写入路径探测（2026-08-21，未下单）

新增 `tools/probe_ths_order.py` —— **只填不交**，文件里没有任何点击提交按钮
（1006）的代码路径。判据用客户端副作用（代码填对后自动回填 `1036 证券名称`
和 `1018 可买(股)`），因为控件回读在这个客户端上恒为空、会给假阴性。

- **雷一引爆**：`set_edit_text`（easytrader 默认）客户端**完全无反应**；
  `type_keys` 回填出「贵州茅台 / 可买 100」。→ adapter 必须
  `enable_type_keys_for_editor()`，否则会「填」完然后照样提交，**下出错误的单**。
- **雷二证伪但换了形态**：`top_window()` 看得见弹窗（我先前推断它与
  `app.windows()` 同源，错了）。真实风险是**验证码框标题也是「提示」**，
  easytrader 会不填验证码直接点确定，并从任意文字里正则抠出**假委托编号**。
  → P9b 必须自己接管弹窗处理，按结构而非标题区分。

仍待实际下单验证（模拟账户，`QBG_MODE=PAPER` 不动三把锁）：
委托确认框的真实标题与结构、下单后 `today_entrusts` 的回读。

### 定时任务方案评审 + 两处修复（2026-08-24）

用户新增了 Windows 计划任务方案（`run_daily.ps1` / `scripts/preflight.ps1` /
`scripts/setup_schedule.ps1` / `docs/windows-schedule.md`）。评审后修掉两个真问题，
**都在既有代码里，不是用户脚本的问题**：

**一、数据新鲜度硬闸会每年误伤 6 天。** `data_freshness_guard` 数的是
`pd.bdate_range`（只排除周末）而非 A 股交易日历。把排程从「当天 18:15」改成
「次日 07:30」之后，最新数据必然是上一交易日，`max_stale_days=1` 的余量归零，
于是任何长于周末的假期都会翻车。2026 年实算误伤：春节 stale=7、清明 2、
劳动 4、端午 2、中秋 2、国庆 6 —— 全部 exit 2。

代价不只是「当天没清单」：exit 2 的语义是「风控拒绝交易，需要人看」，
一年 6 次为非问题拉最高级警报会让人以后不再认真看它。
改用 `calendar.trading_days_between`；日历缺失时退回工作日口径（只会高估，
硬闸宁可多拦），口径写进 `reason` 便于事后判断。12 个测试锁住，
其中一条**反向**断言这些日期在工作日口径下确实会失败 —— 否则测试可能只是
因为日期选得太近而恒过，锁不住任何东西。

**二、同花顺预检会永久误报。** `probe_ths.py --preflight-only` 的退出码在本机
恒为 1（位数检查永远不过），于是 `preflight.ps1` 每天打印「同花顺预检未通过 ——
持仓会降级到 CSV」。这话是假的：位数不匹配不影响取表，P9a/P9b 都是在这个
配置下跑通的。给预检加了 `ADVISORY_CHECKS`，位数降级为「提醒」，
退出码只反映**阻塞项**。端到端复测：`[READY] 预检全绿`。

评审中验过没问题的：三个 `.ps1` 的 UTF-8 BOM、`-Pause` 与 `--dry-run` 两种
顺序的参数绑定、`00_market_check.py` 的输出格式与 preflight 的正则匹配、
兄弟仓库任务（qtf_daily / qtf_preflight / qtagent_daily）原封未动。

还没做的小项（不阻塞）：preflight Step 2 无条件 sleep 10s 可以先探端口再决定；
`-RestartCount 2` 会重试 exit 2（硬闸中止需要人，不需要重试）；
Ctrl+C 中断时 `run_daily.log` 只有 START 没有收尾行。

### P9 第一次实盘化验证（2026-08-24 09:40，交易时段内）

**P9a 取值验证全部通过。** 601398 工商银行当日买入 100 股 @7.82 成交：

    qty=100  sellable_qty=0  cost_price=7.823  last_price=7.83  market_value=783.0

`sellable_qty=0` 是最想拿到的那个数 —— 当天买入当天不可卖，T+1 语义正确。
它是 easytrader 相对截图 OCR 的**真正增量**（截图里经常没有这一列）。

**修了一个我自己引入的回归（雷六）**：为解决买入/卖出页两套同 id 控件的歧义，
先前把 `child_window(control_id=...)` 换成了 `descendants(control_id=...)` ——
而 **`descendants()` 根本不认 `control_id`，会静默忽略**。实测
`descendants(control_id=1032)` 和 `descendants(control_id=1033)` 返回同样 9 个元素。
修法：按 class_name 取完自己比对 `ctrl.control_id()`。

**雷七 —— 本次最有说服力的发现：同花顺对不可卖的卖单是静默拒绝。**
C 段试卖当天买入的股票，实际发生的是：填单 → OCR 校验 → **委托确认框核对全过**
（框里写明「卖出价格 7.600 卖出数量 100 您是否确定以上卖出委托？」）→ 点「是」
→ 客户端只弹了个**无关的营销框** → **当日委托和当日成交里都没有这笔，
零报错、零提示**。逐控件 dump 过每个弹窗，客户端确实什么理由都没给。

> 订单凭空消失。这是「成功判据不能是没抛异常」的最强论据 ——
> 没有提交后回读校验，这笔单会被当成成功，而账户里根本没有它。
> adapter 正确报了失败并中止后续订单。

顺带修了错误信息：先前说「找不到新增记录」，读起来像「我们不知道发生了什么」；
改成如实描述「委托里没有出现这一笔，且客户端未给出理由（同花顺对这类拒绝是静默的）」
并列出常见原因。验证脚本的 C 段输出也改成区分三种结果（被接受 / 有理由的拒绝 /
静默拒绝 / 链路本身故障），先前它无条件打印「✅ 卖单被拒绝」，
把「不知道发生了什么」说成了「正确识别了拒绝」。

### 阶段一收尾：模拟账户实单验证通过（2026-08-21 21:31）

用户授权，在 `模拟炒股-****` 挂跌停价买单：**下出去 → 回读到 → 撤掉了**。
委托价格回读 `1213.97`（小数点完好），合同编号 `6216979694`，撤单后备注变
`全部撤单`、撤消数量 100。`today_entrusts` 12 个字段齐全，可用于 P9b 回读校验
（⚠️ 混有整行空白占位行，必须过滤）。

**雷四**（本轮新发现）：`user.main` 可能不是交易窗口 —— easytrader 的 `connect()`
末行是 `self._main = self._app.top_window()`，而客户端里有个 0x0 却算「可见」的
`Internet Explorer_Hidden` 会抢到 Z 序顶。报错只说「找不到 control_id」，
且**间歇性发作**。修法 `ths_client.bind_main_window()`，**已上线的 P9a 也一直带着这个雷**。

**雷五**（最危险）：**绝不能按 control id 认弹窗按钮**。实测同一 id 语义相反 ——
验证码框 id=1 是「确定」，下单提示框 id=1 是「立即重启」。探测脚本按 id 点中了
「立即重启」，只因它 visible=False 才没出事。撤单确认框里还有个 `1754 改单`
（= 撤单并以新价格委托），误点直接下出新单。修法 `ths_client.find_dialog_button()`：
只看可见按钮 + 去 `&` 助记符 + 白名单 + 危险按钮拉黑，9 个离线测试锁住。

### P9b 下单适配器落地（2026-08-21，**代码完成，未实盘/模拟联调**）

分两层，刻意不放进 `ths_client`，以保住那个模块「无下单路径」的承诺：

| 文件 | 职责 |
|---|---|
| `execution/ths_order_form.py` | 写入层：填单 → **提交前 OCR 回读校验** → 提交 → 弹窗分类处理 |
| `execution/easytrader_adapter.py` | 纪律层：三把锁、先卖后买、**提交后回读 `today_entrusts` 校验**、单次上限、一笔失败即停 |

阶段一那五颗雷逐条对应进了代码：`type_keys`、`{HOME}+{END}{DEL}`、
`force_foreground`、`bind_main_window`、`find_dialog_button`。

**三条设计决定，都源自实测**：

1. **成功判据永远不是「没抛异常」。** 提交前 OCR 比对三个框，提交后到券商的
   当日委托里按**代码/方向/数量/价格四项全对**找回执 —— 找不到就当没下成。
2. **致命提示不点「是」。** `小数部分应为 / 价格超出 / 涨跌幅 / 可用资金不足 /
   可用股份不足 / 非交易` 一律 WM_CLOSE 中止。OCR 看不见小数点，小数位错误
   只能靠客户端这句提示兜底；easytrader 默认发 Alt+Y 一路点「是」，正好相反。
3. **一笔失败停掉整批。** 控件漂移是系统性故障而非偶发，继续下只会把同一个
   错误重复施加到更多订单上。超上限也是整批拒绝，不做「下前 N 笔」——
   半批执行比不执行更难收拾。

另加一道 `_assert_side`：提交前核对提交按钮文字（买入/卖出），
防止菜单静默没切过去就**在买入页下出卖单的参数**。

新增 `qbg_ths_max_orders=8`（兜上游逻辑错误，k=3 正常≤6 笔）。
19 个离线测试（`tests/test_easytrader_adapter.py`），全程 mock，无需同花顺。

⚠️ **卖出页的控件 id 尚未逐个核对过**（只探过买入页），当前按同一组 id 编码，
靠 `_assert_side` 兜底。**P9c 编排接入要等 P9b 在模拟账户上联调通过之后。**

### P9b 联调（2026-08-21，第一轮 → 修了三个 bug）

**bug 1（我的）**：`enum_dialogs()` 的契约和 `_enum_dialogs` 的实现对不上 ——
前者声明 `(id, class, text, visible)` 四元组，后者只返回文本字符串列表。
联调炸在 `ValueError: too many values to unpack (expected 4)`。
`find_dialog_button` 的 9 个测试没能拦住它：那些测试拿的是**手工构造**的四元组，
从没验证过真实数据源产出同一形状。→ 新增 5 个**契约测试**，测「消费方吃不吃得下
生产方给的东西」。顺带修掉：`_enum_dialogs` 原本只收有文本的子控件，
而验证码图片控件 2405 文本为空，会被丢掉、导致 `_is_captcha` 永远认不出。

**bug 2（我的，更严重）**：`EasytraderAdapter._default_connect` **忘了设置取表策略**。
`UniversalClientTrader` 的类默认是 `grid_strategy = Xls`，而 Xls 零验证码处理、
在这个客户端上必然失败 —— 于是**每一次回读校验都会失败**，每笔单都被判成
「可能没下出去」而中止。用户观察到的「跳了验证码但没输入就结束」正是这个。

**bug 3（护栏缺失）**：`bind_main_window` 只检查窗口存在、不检查能不能用。
精简模式下窗口标题不变、也算可见，但缩成约 158x26 的小条，里面没有控件树。
→ 加最小可用尺寸检查，报错直接指向「切回专业模式」。

**收获：拿到了委托确认框的完整内容**（客户端自己声明它即将提交什么）：

    资金帐号：模拟炒股-****
    证券代码：600519(贵州茅台)
    买入价格：1209.260      买入数量：100      预估金额：120962.278
    您是否确定以上买入委托？

这比提交前的 OCR 预检强得多 —— OCR 是**猜**控件里有什么，这是**正式声明**。
已实现 `parse_confirm` / `check_confirm`：点「是」之前逐项核对代码/价格/数量，
对不上就点「否」中止。10 个离线测试用的就是这段实测原文（含 HTML 标签），
包括「读不出内容时必须报不符而不是当成通过」这条负向测试。

### P9b 联调通过（2026-08-21 22:55）

三道校验全部触发，端到端跑通并撤单：

    ths.order.submit      提交前 OCR 通过
    ths.order.confirm_ok  委托确认框与计划逐项吻合
    ths.order.verified    回读匹配，entrust_no=6217004445
    ExecutionResult: ok=True submitted=1
    撤单确认框 → 点 id=6（正确避开 id=1754「改单」）

**卖出页已核对**：提交按钮文字确为「卖出」，标签是「卖出价格/卖出数量/可用余额」，
1032/1033/1034 都在。

**但输出暴露了两个缺陷，已修**：

**缺陷 A — 四字段不足以唯一定位一笔委托。** 当日委托里出现了三行代码/方向/数量/
价格完全相同的记录（同一只票反复试单）。`find_entrust` 返回第一个匹配项，
这次对了纯属客户端按时间倒序给；换个顺序就会匹配到早已撤掉的那笔，
**报告成功并带回错误的合同编号**。
→ 改成**提交前快照合同编号，提交后只在新增编号里找**；建不起基线就拒绝下单。
新增 4 个测试，其中一个先证明「只比四字段确实会被旧记录骗到」。

**缺陷 B — 买入页/卖出页有两套同 id 控件。** 卖出页核对时 1032/1033/1034 全部
`ElementAmbiguousError`，和 grid 的 6 个 `1047` 同源。之前没炸是运气。
→ `_one_visible` 按可见性消歧，**恰好一个才返回**，多于一个抛错而不是随便挑。

### 复盘 agent 关闭（2026-08-21）

按用户要求把 `.env` 的 `QBG_AGENT_ENABLED` 由 `1` 改为 `0`，日流程 `_finish()`
不再调用 `daily_review`。这不是三把锁之一，且方向是关闭（降风险）。
`QBG_AGENTS_ENABLED`（P7 逐票复核）**未动**，仍为 1。

### 数据清理（2026-08-21，用户要求「全部重置 + 永久删除」）

已永久删除：`data/portfolio/positions.csv`、`history/positions_2026-08-10.csv`、
`last_run.json`、`last_rebalance.json`、探针输出、以及整个 `data/runs.db`。
目录结构保留。**未动** `reports/`（含 2026-08-10 日报/清单/复盘）和
`logs/qbg.jsonl`（其中 3 行 `cycle.completed` 带 account/positions 字段）。

## 2026-08-22 定时运行落地（每天 07:30 + 开机补跑）

新增 `scripts/preflight.ps1`、`run_daily.ps1`、`scripts/setup_schedule.ps1`，
`run_daily.bat` / `setup_schedule.bat` 改成壳。完整说明在
`docs/windows-schedule.md`，纪律在 `CLAUDE.md` §八。

**时间从 17:45 挪到 07:30**：盘前出清单、开盘执行，正好对上回测口径
（T-1 收盘出信号、T 日开盘价成交）。07:30 时 BaoStock 的 T-1 数据早已落库。

### 本机实测发现（2026-08-22）

| 发现 | 影响 |
|---|---|
| Clash Verge 在 `D:\Clash Verge\clash-verge.exe`，混合端口 7897，TUN 网卡叫 **Meta**（描述 `Meta Tunnel`，不是 Wintun） | 检测函数要认描述 `*Wintun*` **或** `*Meta Tunnel*`，只认 Wintun 会漏 |
| `verge.yaml` 里热键已绑好：`toggle_system_proxy=CTRL+ALT+SHIFT+P`、`toggle_tun_mode=CTRL+ALT+SHIFT+T` | 和 quant-trading 的 preflight 默认值一致，直接沿用 |
| `verge.yaml` 的 `enable_auto_launch: false` | **Clash 不会开机自启**。无桌面会话里起不了它（托盘 GUI 在 session 0 等于没起），要么开这个开关，要么走自动登录 |
| `verge.yaml` 的 `enable_external_controller: false` | 没有非交互入口能开 TUN。Background 模式下 TUN 是**开不了**的，只能开系统代理 |
| `pwsh.exe` 是 Store 安装，路径在 `WindowsApps` 下 | 那是应用执行别名，**session 0 解析不开**，计划任务会报「找不到文件」。宿主 shell 固定用 System32 的 `powershell.exe`，`.ps1` 因此必须带 UTF-8 BOM |
| 本机没开 AutoAdminLogon | 「开机免解锁 + 有桌面」两者兼得只有自动登录一条路，涉及密码落盘，**留给用户自己决定**，脚本不改 |

### 验证过的

```
scripts\preflight.ps1 -SkipMarketCheck   -> [READY] 全绿，代理/TUN 检测正确、未误触发热键
scripts\preflight.ps1                    -> 2026-08-22 是周六，交易日闸正确跳过（exit 0）
run_daily.ps1                            -> 预检 → daily_cycle → not_trading_day → exit 0
文件独占锁                                 -> 第二次开被拒，跨会话有效
PS 5.1 / PS 7 双版本语法解析               -> 三个 .ps1 都通过
```

**还没验证的**：交易日 07:30 的真实自动触发、Background(S4U) 模式下的实际运行。
下一个交易日看 `Get-ScheduledTaskInfo -TaskName quant_biga_daily` 的
`LastTaskResult` 和 `logs/run_daily.log`。


## 2026-08-10 实测

- qlib/LightGBM seeds 42/43/44 Rank IC：`+0.04208 / +0.04181 / +0.04184`；集成 `+0.04234`。
- 行业中性化：年化 `30.78% → 31.43%`，Sharpe `1.174 → 1.190`，换手 `0.196 → 0.191`。
- model-free SMA 压测选择 20 日：Sharpe `0.849`，最大回撤 `-14.32%`。
- 顾问清单 dry-run：3 笔订单，股数 `100/200/500`，现金闸后剩余 `28,527.08` 元。
- store 重建前后：runs `1`、scores `290`、plans `3`、executions `3`、gates `11`、events `789`，完全一致。
- 中转在线：文本探测、Vision 合成图、P8 合成事实均通过；P7 三次共 216,330 tokens，原始评级不稳定但复核闸结论一致。
- 全链路 dry-run：290 只打分 → 9 只 TA 复核全部通过 → 3 只目标 → 8 条订单建议 → 11 道风控全过 → P8 复盘 normal → 邮件发送成功；未提交订单。
- 邮件：163 SMTP 465 隐式 TLS 在线通过；完整账户日报已发送，主题明确标注“订单建议8笔·未执行”。
- 邮件模板按参考 PDF 改为单列摘要卡：概览、一句话结论、持仓、复核、订单意见、运行健康和自动复盘，并支持移动端表格。
- 实测修复：positions ETL 占位符错误（重建后 5 行、invalid=0）及 dry-run 邮件主题误判，均有回归测试。
- 全量离线测试：**315 passed**；ruff 全绿。

## 2026-08-10 修复：后复权因子伪造下降

沪深300 全量 304 只里有 4 只的 BaoStock 后复权因子会**下降**。后复权因子只会
随分红送股向上累积，下降必定是假的。最严重的是平安银行 2020-12-31：原始价
19.20→19.34（**+0.73%**），复权后变成 **−16.21%** —— 一根凭空造出来的假阴线，
不处理就直接进模型训练集。

换 `query_adjust_factor` 解决不了：BaoStock 自己的权威因子表里就带着这个下降
（已用独立窄区间查询交叉验证）。

`cache.repair_factor()` 按两种形态分别修：**一日凹陷**（次日原样恢复，如万科）
换成前一日值；**持久平移**（掉下去不回来，如平安银行，多半是源换了复权基准）
把断点之后整段按比例抬回去——段内相对变化不变，断点当天比值变成 1.0。
`read()` 默认修复，`read(repair=False)` 取原样；**落盘永远是源的原样**，
所以 `verify()` 仍能报出源的问题（`01_ingest.py` 已改用 `repair=False` 去验）。

验证：4 只全部恢复单调、全量扫描 0 只残留、平安银行那天复权收益回到 +0.73%；
新增 24 个离线单测，全量 **339 passed**，ruff 全绿。详见 `docs/data-sources.md`。

**重建 qlib bin（299 只）并重训三 seed**：集成 Rank IC `+0.042343 → +0.041358`
（`-0.000985`，小于 seed 间离散度 0.00196，属噪声范围；修复后的数字是在正确
数据上测出来的，应采信这一个）。三 seed 仍全正、方向一致。

⚠️ **行业中性化的结论翻转了**：现在原始分数在年化/Sharpe/回撤/换手/Rank IC
上全面优于中性化（36.37% vs 28.82%，Sharpe 1.37 vs 1.14），与旧结论相反。
本次同时变了三样东西（因子修复、股票池 290→299、重训），**不能归因于单一
原因**；行业表已排除损坏（299/299 有归属）。`QBG_INDUSTRY_NEUTRAL` 仍保持 1
未动 —— 翻一个已部署参数需要走 `tuning/` 的八项回测闸做受控评估，单点观测
不够。详见 `docs/p3-model-validation.md`。

上述收益数字有当前成分股生存者偏差，不是未来收益承诺。详见
`docs/p3-model-validation.md`。

## 常用命令

```bash
python scripts/01_ingest.py
python scripts/02_train.py --seeds 3
python scripts/06_backtest.py --scores model
python scripts/04_plan_orders.py --dry-run
run_daily.bat
python scripts/14_backfill_store.py --rebuild
python scripts/20_daily_review.py
python scripts/24_llm_check.py --probe
python scripts/22_params.py list
```

## 仍需用户输入

1. 南京证券实际佣金费率。

详细在线验收步骤见 `docs/remaining-online-validation.md`。
