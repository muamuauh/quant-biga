# 云服务器部署方案

本机实测数据 + 部署步骤。**所有资源数字都是量出来的，不是估的**（测量方法附在末尾）。

最后更新：2026-08-11

---

## 一、实测资源占用

### 磁盘

| 项 | 大小 | 增长 |
|---|---:|---|
| conda 环境 `qbg`（573 个包） | **1.5 G** | 基本不变 |
| vendored `qlib` + `TradingAgents` | 26 M | 基本不变 |
| 代码 + 测试 + 文档 | 5 M | 慢 |
| `data/parquet`（304 只 × 5 年） | 21 M | **每天约几十 KB** |
| `data/qlib_bin` | 67 M | 每次重建覆盖 |
| `mlruns` | 21 M / 4 次训练 | **约 5 M/次训练** |
| `data/runs.db` | 1.2 M | 慢 |
| `reports` + `logs` | 1.3 M | 每天约几十 KB |
| **合计（首次部署后）** | **约 1.7 G** | |

**建议磁盘 40 G**。真正会持续增长的只有 `mlruns`（训练多了要清）和日志/报告，
按每天 100 KB 算一年也只有 36 M。40 G 是给系统盘、swap 和意外留的余量，
而且这个量级的云盘几乎不影响价格。

### 内存 —— 这是选型的关键，两个数差 30 倍

| 场景 | 峰值 RSS | 何时发生 |
|---|---:|---|
| **日常流程**（拉数→打分→选股→风控→清单→日报→复盘→邮件） | **约 140 M** | 每个交易日 |
| **模型训练**（Alpha158 × 299 只 × 1600 天 = 47 万行 × 158 因子） | **4,389 M** | 只在 `--retrain` 时 |

`daily_cycle` **默认不训练**（`src/qbg/orchestrator/daily_cycle.py:57` 的
`if retrain:`），所以 4.4 G 那个数**不是常驻需求**。三 seed 是串行训练，
峰值和单 seed 相近，不是 3 倍。

这直接给出两条路线：

* **A. 服务器上训练** → 需要 **4 C / 8 G**（4.4 G 峰值 + 系统 + 余量）
* **B. 本地训练，只把模型产物同步上去** → **2 C / 2 G 就够**，日常只吃 140 M

B 省一半以上的钱，代价是每次调参要在本地跑完再 `rsync mlruns/`。
考虑到重训是**低频**动作（换参数、补数据才需要），B 更划算。
折中做法：选 2 C / 4 G，加 4 G swap，偶尔在服务器上训练也扛得住（会慢，但不 OOM）。

### CPU / 耗时

| 步骤 | 本机耗时 |
|---|---|
| 首次全量拉取 304 只 × 5 年 | 约 35 分钟（45 万行） |
| 每日增量拉取 | **秒级** |
| 单 seed 训练 | 约 2 分钟 |
| 回测 | 秒级 |
| 日循环全流程（不含 LLM） | 秒到分钟级 |

日常负载极轻，CPU 不是瓶颈。训练时 LightGBM 会吃满核心。

---

## 二、服务器选型：**地域是最大的风险，不是配置**

### 数据源全部在中国境内

| 依赖 | 主机 | 海外服务器的风险 |
|---|---|---|
| BaoStock（**主数据源**） | `baostock.com`，自建 TCP | 慢；不稳定 |
| AKShare 元数据 | 中证指数、申万、新浪 | 部分站点限制境外 IP |
| AKShare 行情备源 | `push2his.eastmoney.com` | **本机实测东财 `push2*` 已不可达**（见 `docs/data-sources.md`），境外只会更差 |
| SMTP 通知 | `smtp.163.com:465` | 境外常被限流 |
| LLM（P7/P8） | DeepSeek / 第三方中转站 | 境内可达 |

> **结论：选中国大陆的云服务器**（阿里云 / 腾讯云 / 华为云）。
> 数据源、SMTP 都在境内，LLM 走的中转站境内也通。
> 退而求其次选**香港**——BaoStock 和中证还行，但东财和 163 SMTP 会明显变差。
> **不建议**美西/欧洲节点。

### 推荐规格

```
地域    中国大陆（离你近的可用区即可，本项目对延迟不敏感）
规格    2 vCPU / 4 GB / 40 GB SSD        ← 推荐，配 4G swap 可应急训练
        2 vCPU / 2 GB / 40 GB SSD        ← 若确定只在本地训练
        4 vCPU / 8 GB / 40 GB SSD        ← 若要在服务器上常规重训
系统    Ubuntu 22.04 LTS 或 Debian 12
带宽    1 Mbps 按量即可（日增量数据量极小）
```

大陆云服务器买之前注意：**ICP 备案只在你要对外提供 Web 服务时才需要**。
本项目没有对外端口（只有出站请求 + SSH），不涉及备案。

---

## 三、迁移到 Linux 需要改的东西

好消息：**Python 代码是干净的**——扫过 `src/` `scripts/` `tools/`，
没有硬编码的 Windows 路径或 API。只有两处需要处理：

1. `run_daily.bat` / `setup_schedule.bat` —— 换成 shell 脚本 + cron（下面给）
2. `src/qbg/model/train.py:115` 的 `.replace("\\", "/")` —— 在 Linux 上是 no-op，无需改

**时区必须设成 `Asia/Shanghai`**。交易日历、交易时段、`date.today()` 全依赖它，
云服务器默认常是 UTC，不设的话「今天」会算错 8 小时，直接导致跑错日期。

---

## 四、部署步骤

### 1. 系统准备

```bash
ssh root@<你的服务器>

timedatectl set-timezone Asia/Shanghai     # 必须，否则日期算错
apt update && apt install -y git curl build-essential

adduser --disabled-password --gecos "" qbg   # 不用 root 跑交易程序
usermod -aG sudo qbg
```

如果选了 2 G 内存又想偶尔训练，加 swap：

```bash
fallocate -l 4G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### 2. 装 miniconda 与环境

```bash
su - qbg
curl -fsSLO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/miniconda3
~/miniconda3/bin/conda init bash && exec bash

git clone https://github.com/muamuauh/quant-biga.git ~/quant-biga
cd ~/quant-biga
conda env create -f environment.yml       # 建 qbg 环境
conda activate qbg
pip install -e ".[data,model,llm,dev]"
```

### 3. vendored 依赖（P3 训练和 P7 复核需要）

```bash
git clone https://github.com/microsoft/qlib qlib && pip install -e qlib
git clone https://github.com/TauricResearch/TradingAgents TradingAgents && pip install -e TradingAgents
python scripts/patch_tradingagents.py      # 注册 ashare vendor，必跑
```

### 4. 配置

```bash
cp .env.example .env
chmod 600 .env        # 里面有 SMTP 密码和 LLM key
vi .env
```

**`.env` 绝不能进 git**（已在 `.gitignore`）。填的时候确认三把锁：

```bash
QBG_MODE=ADVISORY     # 保持顾问模式
I_CONFIRM_REAL=0
# configs/risk_limits.yaml 的 allow_live_mode 保持 false
```

### 5. 首次数据落地

```bash
python scripts/01_ingest.py               # 约 35 分钟，拉 304 只 × 5 年
python scripts/02_train.py --seeds 3      # 若在服务器训练；否则见下方"本地训练"
python scripts/06_backtest.py --scores model   # 冒烟验证
pytest -q                                 # 364 个离线测试应全绿
```

### 6. 定时任务（替代 setup_schedule.bat）

仓库里已经有 **`run_daily.sh`**（对应 Windows 的 `run_daily.bat`），直接用：

```bash
chmod +x run_daily.sh
./run_daily.sh --dry-run        # 先手动验一次
crontab -e
```

```cron
# BaoStock 日K 约 17:30 出、复权因子约 18:00，所以排在 18:15。
# 非交易日流程自己会安静跳过，cron 不必判断。
# 不重定向 stdout/stderr：脚本正常跑完是静默的，只有出问题才写 stderr，
# 配合下面的 MAILTO 就是"有信 = 有事"。完整流水账在 logs/run_daily.log。
MAILTO=你的邮箱@example.com
15 18 * * 1-5  /home/qbg/quant-biga/run_daily.sh
```

`run_daily.sh` 已经处理好这些坑：

| 处理 | 为什么 |
|---|---|
| 直接定位 env 的 python，不用 `conda activate` | cron 不加载 shell profile，conda 的 shell 函数根本不存在——这是 cron 部署最常见的失败点。可用 `QBG_PYTHON=` 覆盖 |
| `export TZ=Asia/Shanghai` + **启动时自检实际时区** | 交易日历和 `date.today()` 全靠它。北京时 00:00–08:00 补跑时若时区是 UTC 会**差一天且不报错** |
| `flock` 互斥锁 | cron 和手动执行撞车会同时写 parquet/runs.db。拿不到锁退出 0，因为这不是错误 |
| 正常跑**不往 stderr 写东西** | cron 的约定是"有输出 = 有事"。天天发一封没事的信，十天后你就不看它了 |
| 原样传出退出码 0 / 2 / 其他 | 2 = 硬闸中止，需要人看；不能被脚本吞掉 |
| 找不到 Python 时吵闹退出 127 | Python 跑起来后崩溃有 `notify_failure` 兜底发信，但**解释器都找不到时通知链是断的**——表现就是"今天没收到邮件"，而这和周末长得一模一样 |

---

## 五、两个需要你决定的操作问题

### 1. 持仓怎么进服务器

当前 `QBG_PORTFOLIO_SOURCE=ocr`，靠你截图手机 APP 持仓、交给多模态 LLM 读。
**服务器是无头的，没法直接截图**，所以有三条路：

| 方案 | 做法 | 评价 |
|---|---|---|
| **A. 本地 OCR，同步 CSV** | 本地跑 `tools/ocr_positions.py`，`scp data/portfolio/positions.csv` 上去 | 推荐。截图不出本机，隐私最好 |
| B. 上传截图到服务器 | `scp 截图.png`，服务器上跑 OCR | 少一步，但账户截图要过网 |
| C. 改用 manual | SSH 上去直接编辑 `positions.csv` | 最简单，持仓少时够用 |

无论哪种，**持仓不更新会让清单基于过期持仓**。建议在日报里加一条持仓时效告警
（`positions.csv` 的 `asof` 距今超过 N 个交易日就警告）——这个当前**还没有**，
要的话我可以加。

### 2. 训练放哪

见 §一的路线 A/B。选 B（本地训练）的话，同步方式：

```bash
rsync -avz --delete mlruns/ qbg@<server>:~/quant-biga/mlruns/
rsync -avz --delete data/qlib_bin/ qbg@<server>:~/quant-biga/data/qlib_bin/
```

---

## 六、安全

* **`.env` 权限 600**，含 SMTP 密码和 LLM key
* **SSH 关闭密码登录**，只用密钥：`PasswordAuthentication no`
* **三把锁保持关闭**：`QBG_MODE=ADVISORY` + `I_CONFIRM_REAL=0` +
  `allow_live_mode: false`。任何自动化流程都不得改它们（`CLAUDE.md` 硬性禁令）
* **不要开放任何入站端口**，本项目只有出站请求
* `data/portfolio/`、`reports/`、`logs/` 含账户金额，已 gitignore；
  **服务器上也不要放进任何会被同步出去的目录**
* 本项目**没有**入站邮件命令通道，邮件回复不会进入交易路径

---

## 附：测量方法

```
磁盘   du -sh <各目录>
内存   后台跑目标脚本，用 1~1.5 秒间隔轮询进程 WorkingSet64 取最大值
       训练：scripts/02_train.py --seeds 1   → 峰值 4,389 MB
       日常：scripts/06_backtest.py --scores model → 峰值 140 MB
耗时   脚本自身打印的 elapsed_sec，以及 ingest 的 2109.6s 全量记录
```

测量环境：Windows 11 + conda `qbg` (Python 3.11.15)。Linux 上内存占用通常
略低（无 Windows 的进程开销），可按同量级规划。
