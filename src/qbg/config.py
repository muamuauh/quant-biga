"""类型安全的配置读取。所有 env var 只在这里读一次，业务代码不碰 os.environ。

沿用 quant-trading 的写法：**每个参数都要写清"为什么是这个值"**。这些注释不是
装饰——半年后回来看，一个没有理由的魔数就等于一个不敢改的魔数。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # 运行模式
    # ------------------------------------------------------------------
    # ADVISORY = 只出下单清单，零券商接触（默认，也是本期唯一实现的模式）
    # PAPER    = 模拟盘
    # LIVE     = 实盘。需要三把锁同时开：qbg_mode=LIVE + i_confirm_real=1
    #            + risk_limits.yaml 的 allow_live_mode: true。
    #            单把锁误开不会导致下单，这是照搬 quant-trading 的设计。
    qbg_mode: str = "ADVISORY"
    i_confirm_real: int = 0

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------
    # 降级链顺序。baostock 放第一是因为它是唯一不靠爬网页的免费源；akshare 覆盖
    # 最全但底层爬东财，批量拉会限频/封 IP，所以只用它拉低频元数据和新闻财务。
    qbg_data_sources: str = "baostock,akshare,mootdx"
    # 并发拉取的 worker 数。这些源都没有官方并发上限文档，但 akshare 一旦被判定
    # 为爬虫就会返回验证页，所以保守取 4；真正省时间的是增量缓存不是并发。
    qbg_ingest_max_workers: int = 4
    # 行情历史起点。5 年足以覆盖一轮牛熊（含 2022 熊市），再往前的 A股市场结构
    # 差异太大（注册制前后、涨跌幅规则变更），训练价值反而下降。
    qbg_history_start: str = "2020-01-01"
    tushare_token: str = ""  # 预留：将来充值积分后可切主源

    # ------------------------------------------------------------------
    # 股票池
    # ------------------------------------------------------------------
    # 沪深300。选它而不是全市场：流动性好、停牌少、退市风险低，且 10 万账户
    # 本来也只能碰得动大盘股（见 qbg_top_k 的一手约束说明）。
    qbg_index_code: str = "000300"
    # 次新股过滤。上市首日及前几日涨跌幅规则特殊（创业板/科创板前 5 日不设涨跌
    # 幅），60 个自然日一刀切能规避这整类复杂性，代价是错过次新股行情——对
    # 一个跑因子模型的系统来说这个交换很划算。
    qbg_min_list_days: int = 60
    qbg_exclude_st: int = 1
    # 北交所：涨跌幅 ±30%、流动性差、开户还需 50 万+2 年经验。沪深300 本来也不
    # 含北交所，这个开关是防止将来换股票池时忘了排除。
    qbg_exclude_bse: int = 1

    # ------------------------------------------------------------------
    # 策略
    # ------------------------------------------------------------------
    # 持仓只数。10 万账户 → 单槽约 3 万 → 一手(100股)预算上限 300 元/股。
    # 再多一只槽位就小到买不起大部分票，再少则单票风险过高。
    qbg_top_k: int = 3
    # 迟滞：持仓股只要还在今日排名前 keep_rank 名内就保留，掉出去才换人。
    # quant-trading 实测（50 票、k=5）这一项把日均换手从 0.745 降到 0.524。
    # A股换手成本更高（印花税单边 5bp），所以这个值只会更重要，不会更不重要。
    qbg_keep_rank: int = 15
    # 调仓频率（交易日）。**这是小账户的头号参数。** quant-trading 的实测教训：
    # 3000 美元账户日频调仓，费用+滑点把年化打到 −30%，改月频才勉强打平。
    # A股往返成本约 10bp（佣金双边 + 印花税卖出单边 + 过户费双边），10 个交易日
    # ≈ 两周，是成本和信号衰减之间的折中。绝不要为了"看起来更活跃"调低它。
    qbg_rebalance_every_days: int = 10
    # 高现金触发：闲置现金超过这个比例就当天当作调仓日，避免风控清仓后大量现金
    # 一直躺到下个调仓日。自限：钱投出去后自动回到正常节奏。
    qbg_rebalance_cash_trigger: float = 0.50
    # 市场择时：沪深300 跌破 N 日均线则全部转现金。2026-08-10 用当前本地
    # 沪深300缓存做 2020–2026 model-free 压测：20/50/100/200 日 Sharpe 分别
    # 0.849/0.803/0.815/0.810，最大回撤 -14.32%/-14.32%/-16.36%/-15.01%。
    # 因此取 20；它同时给出最高 Sharpe 和并列最低回撤。结果有当前成分股的
    # 生存者偏差，积累历史成分股快照后必须复验。0 = 关闭择时。
    qbg_market_sma: int = 20
    # 多 seed 集成：单个 LightGBM 的 Rank IC 光靠 seed 就能摆动 ±0.007，平均掉
    # 这部分噪声（信号在 seed 间一致，噪声不一致）。quant-trading 实测 N=3 是
    # 甜点，N=5 会把本就集中的 top-K 过度平滑。
    qbg_ensemble_seeds: int = 3
    # 行业中性化：申万一级行业内 demean 后再选 top-K，避免一次押注单一行业。
    # 用真实行业分类而非 LLM 打标（A股有现成的申万分类），所以结果可复现。
    qbg_industry_neutral: int = 1

    # ------------------------------------------------------------------
    # 持仓来源
    # ------------------------------------------------------------------
    # ocr        = 读 tools/ocr_positions.py 产出的 CSV（默认）
    # manual     = 同一份 CSV，手工编辑
    # easytrader = P9a，直接读同花顺客户端，**只读**
    qbg_portfolio_source: str = "ocr"

    # ------------------------------------------------------------------
    # 同花顺客户端只读持仓（P9a）
    # ------------------------------------------------------------------
    # 只在 qbg_portfolio_source=easytrader 时生效，且**只读**：这条链路上
    # 没有任何下单代码。下单是 P9b，届时走 execution 层并需要三把锁。
    #
    # 本机实测（同花顺 9.60.61）路径；换机器改这里，别改代码。
    qbg_ths_exe: str = r"D:\tonghuashun\同花顺\xiadan.exe"
    # universal_client = 同花顺官网通用客户端（xiadan.exe 在同花顺安装目录下）
    # ths              = 券商自己发的同花顺改版包
    qbg_ths_client: str = "universal_client"
    # 取表策略。auto = 本项目自实现，做三件 easytrader 没做的事：
    # 按可见性消歧 grid、用 ddddocr 认反爬验证码、用哨兵值确认剪贴板真被更新。
    # easytrader 原生的 xls/copy/wmcopy 在同花顺 9.60.61 上**全部不可用**，
    # 而且 copy/wmcopy 是**静默**返回空列表（不抛异常），细节见 plan.md §2.2.5。
    qbg_ths_grid_strategy: str = "auto"
    # 单张表的重试次数。复制这一步在实测中不稳定（同一张表连跑有时成功有时
    # 纹丝不动）。有哨兵校验兜底，重试不会把陈旧剪贴板当成新数据。
    qbg_ths_retries: int = 3
    # 判定「这是模拟盘账户」的特征串，在客户端的资金账号标识里找它。
    # 本机同花顺模拟炒股显示为「模拟炒股-****」。
    #
    # 用途见 easytrader_adapter.assert_account_matches_mode：QBG_MODE 只是 .env
    # 里的一句声明，代码无从知道客户端登录的到底是模拟盘还是真钱账户。
    # 客户端的资金账号是唯一的独立信源，这个特征串就是判据。换券商改这里。
    qbg_paper_account_pattern: str = "模拟"

    # 单次运行最多提交几笔订单（P9b）。
    # 这是兜住**上游逻辑错误**的闸，不是策略参数：k=3 的账户一次调仓正常在
    # 6 笔以内（最多 3 卖 3 买），一次要下十几笔多半是选股或规划出了问题。
    # 超过就整批拒绝，而不是"下前 N 笔"——半批执行比不执行更难收拾。
    qbg_ths_max_orders: int = 8

    # 读失败时是否退回 ocr/manual 的 CSV。
    # 默认 1：持仓读不到不该让整条日流程停摆，CSV 是人可控的降级路径。
    # 但降级意味着**可能用过期持仓出清单**，所以日报必须显式标注来源和 asof
    # ——静默降级比读失败更危险。
    qbg_ths_fallback_to_csv: int = 1

    # ------------------------------------------------------------------
    # 第三方 OpenAI-compatible 中转站
    # ------------------------------------------------------------------
    # 统一三元组供 P7 逐票复核、P8 每日复盘和（未单独覆盖时）P5 OCR 共用。
    # base_url 必须包含服务商要求的 API 前缀，通常以 /v1 结尾；不在代码里
    # 写死供应商，避免换站时同时改三套调用链。
    qbg_llm_base_url: str = ""
    qbg_llm_api_key: str = ""
    # deep 用于最终判断/复盘，quick 用于高频分析与 OCR；模型 id 必须按中转站
    # 实际暴露的名称填写，不能假定它与官方 id 完全相同。
    qbg_llm_model: str = ""
    qbg_llm_model_quick: str = ""
    # 中转站偶发排队，90 秒比 SDK 默认值宽松，但仍能避免夜间任务无限挂起。
    qbg_llm_timeout_seconds: float = 90.0

    # ------------------------------------------------------------------
    # TradingAgents 逐票复核（P7）
    # ------------------------------------------------------------------
    # 当前部署已由用户决定启用；代码默认仍为 0，避免新克隆在未配置中转站时
    # 意外产生高额调用。实际部署通过 gitignored 的 .env 显式设为 1。
    qbg_agents_enabled: int = 0
    # 五档评级里保留的下限。10 万账户 k=3 时，如果卡在 Overweight 会经常只剩
    # 1 只票通过，renormalize 后撞上单票上限，导致大量现金闲置。Hold 是
    # quant-trading 实盘用的值，同样的理由在这里更强。
    qbg_agents_min_rating: str = "Hold"
    # 复核多少个候选。**必须显著大于 top_k**：复核会砍掉一部分，候选池太小就
    # 填不满槽位。9 ≈ 3×k，留出被砍 2/3 的余量。
    qbg_agents_candidates: int = 9
    # LLM 挂了/超时时放行（fail-open）。理由：复核是 qlib 信号之上的**过滤器**，
    # 不是信号本身。LLM 不可用时退回纯量化信号，比整天不交易更合理。
    qbg_agents_fail_open: int = 1
    # 复核砍掉一些票后，把幸存者的权重按比例放大回原定投资总额（并受单票上限
    # 截断），而不是让被砍的资金闲置。
    qbg_agents_renormalize: int = 1

    # ------------------------------------------------------------------
    # OCR 读图（P5）
    # ------------------------------------------------------------------
    # ⚠ 隐私：启用后账户持仓截图会上传到该 LLM 提供商。若不接受，
    # 用 qbg_portfolio_source=manual 手工维护 CSV。
    qbg_vision_base_url: str = ""
    qbg_vision_api_key: str = ""
    qbg_vision_model: str = ""

    # ------------------------------------------------------------------
    # 复盘调参 agent（P8）
    # ------------------------------------------------------------------
    qbg_agent_enabled: int = 0
    # 自动应用参数改动。默认 0 = agent 只能提议，人来批。改成 1 前请先确认
    # tuning 的回测把关确实能拦住坏改动（见 P8 的负向测试）。
    qbg_agent_autoapply: int = 0
    # 单次复盘最多输出约 3k token，足够一份中文日报；继续放大只会增加成本，
    # 不会增加事实，因为事实由本地确定性层先行提供。
    qbg_agent_max_tokens: int = 3072
    # 复盘需要稳定而非创意。部分推理模型会忽略 temperature，这是兼容性参数。
    qbg_agent_temperature: float = 0.1
    # 大多数 OpenAI-compatible 中转支持 json_object；若某站明确不支持可关闭，
    # 本地仍会严格解析和校验 JSON，解析失败则不写报告。
    qbg_agent_json_mode: int = 1

    # ------------------------------------------------------------------
    # 邮件通知（只出站）
    # ------------------------------------------------------------------
    # 邮件是运行结果的旁路通知，不属于交易路径。默认关闭；即使 SMTP 认证失败或
    # 网络不可用，也只记日志而不改变 hard_ok、订单清单或进程退出状态。
    notify_email_enabled: int = 0
    # 留空比写死某家邮箱更安全：不同服务商的 SMTP 域名不同，填错会让每晚任务
    # 固定等待网络超时。465/8465 使用隐式 TLS，其余端口使用 STARTTLS。
    smtp_host: str = ""
    smtp_port: int = 587
    # 发件人与应用专用密码只放 gitignored 的 .env。多数服务商不接受账户登录密码。
    smtp_user: str = ""
    smtp_password: str = ""
    # 可与发件人不同；留空时回退到 smtp_user，方便发给自己且减少必填项。
    notify_email_to: str = ""

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    parquet_dir: Path = PROJECT_ROOT / "data" / "parquet"
    qlib_provider_uri: Path = PROJECT_ROOT / "data" / "qlib_bin" / "cn_data"
    snapshot_dir: Path = PROJECT_ROOT / "data" / "snapshots"
    portfolio_dir: Path = PROJECT_ROOT / "data" / "portfolio"
    universe_file: Path = PROJECT_ROOT / "configs" / "universe_hs300.txt"
    risk_limits_yaml: Path = PROJECT_ROOT / "configs" / "risk_limits.yaml"
    fee_profile_yaml: Path = PROJECT_ROOT / "configs" / "fee_profile.yaml"
    workflow_yaml: Path = PROJECT_ROOT / "configs" / "workflow_cn_lgb.yaml"
    mlruns_dir: Path = PROJECT_ROOT / "mlruns"
    report_dir: Path = PROJECT_ROOT / "reports"
    log_dir: Path = PROJECT_ROOT / "logs"
    db_path: Path = PROJECT_ROOT / "data" / "runs.db"

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def data_sources(self) -> list[str]:
        """降级链，已去空白。"""
        return [s.strip() for s in self.qbg_data_sources.split(",") if s.strip()]

    @property
    def positions_csv(self) -> Path:
        return self.portfolio_dir / "positions.csv"

    @property
    def is_live(self) -> bool:
        """只判断 .env 的两把锁；第三把锁 (allow_live_mode) 在 risk/gates.py 里查。

        故意分开：配置层不该独自能得出"可以实盘"的结论。
        """
        return self.qbg_mode.upper() == "LIVE" and self.i_confirm_real == 1


def _env_pins() -> set[str]:
    pins = {key.lower() for key in os.environ}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                pins.add(stripped.split("=", 1)[0].strip().lower())
    return pins


def _with_tuned_overlay(base: Settings) -> Settings:
    """应用受限 settings overlay；人工环境变量和安全锁永远优先/冻结。"""
    path = PROJECT_ROOT / "configs" / "tuned_params.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, TypeError):
        return base
    frozen = {"qbg_mode", "i_confirm_real", "qbg_agent_enabled", "qbg_agent_autoapply",
              "qbg_portfolio_source"}
    pins = _env_pins()
    updates = {key: value for key, value in dict(raw.get("settings") or {}).items()
               if key not in frozen and key.lower() not in pins and hasattr(base, key)}
    return base.model_copy(update=updates)


settings = _with_tuned_overlay(Settings())


def load_universe() -> list[str]:
    """读 configs/universe_hs300.txt → ['600519.SH', '000858.SZ', ...]。

    文件不存在时返回空列表而不是抛异常：P1 之前它本来就不存在，
    上层脚本应该给出"先跑 01_ingest"的提示，而不是一个 FileNotFoundError。
    """
    if not settings.universe_file.exists():
        return []
    return [
        line.strip()
        for line in settings.universe_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
