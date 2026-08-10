# 尚需外部输入的在线验收

截至 2026-08-10，P3–P6 已完成本机真实数据验收；P5/P7/P8 的离线实现和安全
负向测试已完成，但以下步骤不能在没有用户材料或密钥时伪造为“已通过”。

1. **OCR 真实截图**：提供一张南京证券 APP 持仓页截图，并配置
   `QBG_VISION_API_KEY` / `QBG_VISION_MODEL`。先运行
   `python tools/ocr_positions.py screenshot.png --dry-run`，核对字段后再人工确认落盘。
2. **TradingAgents 真实评级**：配置 `DEEPSEEK_API_KEY` 及 `.env.example` 中的
   `TRADINGAGENTS_*`，对一只候选连续跑三次，检查中文五档评级、无数据措辞、token
   成本和稳定性；通过前保持 `QBG_AGENTS_ENABLED=0`。
3. **复盘 agent 在线层**：当前已具备零 LLM 事实诊断、参数白名单、八项回测闸、
   overlay 快照/回滚和写入熔断。Claude Agent SDK 的 MCP 工具运行层尚未移植；
   在完成并配置 Anthropic Messages API 前保持 `QBG_AGENT_ENABLED=0`。
4. **佣金实值**：`configs/fee_profile.yaml` 仍是行业常见的万 2.5 + 最低 5 元。
   获得南京证券实际费率后再更新，并重跑模型回测。

此外，`setup_schedule.bat` 只生成在仓库中，尚未自动执行；创建 Windows 计划任务
是外部系统变更，应由用户明确运行。
