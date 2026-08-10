# `ocr_positions.py` 调用说明

把一张或多张券商持仓截图发送到已配置的多模态模型，转成持仓结构，经过本地强校验
后写入 `data/portfolio/positions.csv`。该目录下的截图和输出 CSV 均被 Git 忽略。

## 隐私与前置条件

- 截图包含真实资产、持仓和盈亏，会上传到 `QBG_VISION_BASE_URL`；该项为空时复用
  `.env` 中的 `QBG_LLM_BASE_URL`。
- 必须先配置 API key 和支持图片输入的模型；可用
  `python scripts/24_llm_check.py --probe-vision` 做无隐私连通性测试。
- 建议把截图放在 `tools/screenshots/`。不要把截图移出被忽略目录或提交 Git。
- OCR 需要每只持仓至少两日本地行情来校验涨跌停。缺行情时先运行：

```powershell
python scripts/01_ingest.py --codes 600519.SH,000858.SZ --skip-meta --no-qlib
```

## 人工调用

始终先 dry-run；截图没有显示日期时显式传入 `--asof`：

```powershell
python tools/ocr_positions.py tools/screenshots/holding.jpg `
  --asof 2026-08-10 --dry-run
```

多张滚动截图可一次传入，模型会合并重叠行：

```powershell
python tools/ocr_positions.py tools/screenshots/part1.jpg tools/screenshots/part2.jpg `
  --asof 2026-08-10 --dry-run
```

人工核对输出无误后写入当前持仓和历史快照：

```powershell
python tools/ocr_positions.py tools/screenshots/holding.jpg --asof 2026-08-10 --yes
```

## 自动化工具调用

使用 `--json` 获得单个 JSON 对象。为避免机器调用意外停在确认提示，`--json` 必须
同时使用 `--dry-run` 或 `--yes`：

```powershell
python tools/ocr_positions.py tools/screenshots/holding.jpg `
  --asof 2026-08-10 --dry-run --json
```

成功结构：

```json
{
  "ok": true,
  "dry_run": true,
  "asof": "2026-08-10",
  "total_equity": 100000.0,
  "available_cash": 20000.0,
  "positions": [],
  "warnings": [],
  "written": false
}
```

退出码：

| 退出码 | 含义 | 是否写文件 |
|---:|---|---|
| `0` | OCR 与全部强校验通过 | dry-run 否；`--yes` 是 |
| `1` | 人工取消 | 否 |
| `2` | 参数错误或截图不存在 | 否 |
| `3` | OCR 结果未通过强校验 | 否 |

强校验包括名称/代码对应、数量与可卖数量、100 股整手提示、成本价、市值等式、
总资产等式、前收盘涨跌停范围和多图重复行一致性。任一 fatal issue 都拒绝落盘。
