"""给第三方数据源的网络调用套一个超时。

## 为什么需要这个模块

2026-09-09 实测：日流程卡死 10 分钟没有任何进展，进程只有一条到
`127.0.0.1:7897`（Clash 混合端口）的 TCP 连接，CPU 累计 1.7 秒 —— 请求发出去
了，对端不回，而调用链上**一个超时都没有**。

`akshare` / `baostock` 都不把 `timeout` 暴露到函数签名上，所以没法一层层传下去。
而这件事的后果比"慢"严重得多：

  · `fetch_index_constituents` 已经写好了 fail-soft 分支（拿不到就返回 `[]`，
    `01_ingest.py` 退回本地 universe 文件）—— **但没有超时，那个分支永远
    走不到**。它只挡得住"立刻报错"，挡不住"永远不回"。
  · 计划任务的 `ExecutionTimeLimit` 是 2 小时，而 `MultipleInstances=IgnoreNew`
    意味着这 2 小时里任何后续触发都会被拒绝。一次挂起 = 一整天作废。

**没有超时的降级链是死代码。** 这个模块的唯一目的就是让它活过来。

## 为什么用 `socket.setdefaulttimeout` 这种钝器

因为没有别的选择：akshare 内部用 `requests`，而 `requests` 的 `timeout` 参数
在它的函数签名里根本不存在。`socket.setdefaulttimeout()` 会被 `requests` →
`urllib3` → `socket` 这条链继承，是唯一够得着的杠杆。

## 三条必须知道的限制

1. **它是进程全局的。** 所以只在 `with` 块里生效，退出时恢复原值 ——
   绝不在模块导入时设一个全局默认，那会影响到整个进程里所有不相干的网络代码。
2. **它是「单次 socket 操作」的超时，不是「整个请求」的总时限。** 一个每 29 秒
   吐一个字节的服务器仍然能把你挂住。要真正的总时限得上看门狗线程/子进程，
   那是另一个量级的复杂度，当前这个场景（对端完全不回）用不着。
3. **不是线程安全的。** 同一进程里若有别的线程在做网络 I/O，会被一起改。
   本项目的数据层是串行的（BaoStock 的会话本身就不是线程安全的，见
   `01_ingest.py` 的 `--workers` 注释），所以不构成问题。
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager

from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)


@contextmanager
def net_timeout(seconds: float | None = None) -> Iterator[float | None]:
    """在块内给所有 socket 操作设一个超时，退出时恢复原值。

    `seconds` 为 `None` 时取 `settings.qbg_net_timeout_sec`；`<= 0` 表示不设限
    （退化成原来的行为，留给"我就是要等"的场景）。

    超时到了，第三方库会抛 `socket.timeout` / `requests` 的 `ReadTimeout` ——
    **调用方原有的 `except Exception` fail-soft 分支因此才会被触发**，
    这正是加它的目的。这里不吞异常。
    """
    from qbg.config import settings

    if seconds is None:
        seconds = float(settings.qbg_net_timeout_sec)
    if seconds is None or seconds <= 0:
        yield None
        return

    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield seconds
    finally:
        # 必须在 finally 里恢复：块内抛异常时也不能把超时留给后面的代码，
        # 否则一次数据源失败会静默地改变整个进程后续的网络行为。
        socket.setdefaulttimeout(previous)


def guarded(fn, *args, default=None, what: str = "", **kwargs):
    """带超时地调用 `fn`，失败时记日志并返回 `default`。

    给那些"拿不到就降级"的元数据调用用。**不要**用在行情主链路上 ——
    那里的失败要让降级链自己看见并换源，而不是在这里被吃掉变成一个默认值。
    """
    try:
        with net_timeout():
            return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 —— 网络异常的类型不稳定
        log_event(log, "net.call_failed", what=what or getattr(fn, "__name__", "?"),
                  error=f"{type(exc).__name__}: {exc}"[:200])
        return default
