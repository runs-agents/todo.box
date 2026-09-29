# -*- coding: utf-8 -*-
"""
box_lock.py — 待办箱工具族共享锁（B0 · 2026-9-12）

【职责】写入器/整理器/扫描器共用的文件锁。三工具 import 同一份实现，严禁复制粘贴。

【设计依据】《待办箱写入器与整理器方案 v4》§5.2 —— kimi-k3 终审的完整判定真值表。

核心设计（血泪教训的产物）：
  ① **pid 活性为主、超时为辅** —— 但「pid 探活」五个字掩盖了三个语义不同的分支：
     · OpenProcess 失败 + ERROR_INVALID_PARAMETER(87) → pid 不存在 → 真死锁，可清
     · OpenProcess 失败 + ERROR_ACCESS_DENIED(5)     → **活着但没权限看** → 按活处理！
       （不区分错误码会把权限问题误判成死锁 → 删活锁 → 双写 → 后写赢、先写蒸发）
     · OpenProcess 成功 ≠ 活着 —— 进程退出了但句柄未关时 OpenProcess 照样成功，
       必须 GetExitCodeProcess == STILL_ACTIVE(259) 才是真活着
  ② **pid 复用防护** —— Windows pid 复用激进；用 GetProcessTimes 取进程创建时间，
     若「进程创建时间晚于锁创建时间」→ pid 已复用 → 原主已死
  ③ **心跳** —— 长任务每 60s touch 刷新 mtime；心跳线程是 ACCESS_DENIED 场景的
     **唯一逃生门**（那种场景下无法探活，只能靠心跳判断）。心跳线程必须
     try/except 全捕获 + 停摆标志位供主线程读取。
  ④ **释放校验** —— 释放前回读锁文件确认 pid 是自己才删（防删掉后来者的锁）。

诚实边界：**本锁是单机锁**——不防多设备并发（那由 OneDrive 冲突副本巡检兜底）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from box_config import (  # noqa: E402
    LOCK_FILE, LOCK_STALE_SECONDS, LOCK_HEARTBEAT_SECONDS,
    LOCK_RETRY_TIMES, LOCK_RETRY_INTERVAL,
)

# ── Windows API（ctypes 标准库调用，不引 psutil）─────────────
# 2026-09-17 前置修复（第4家审查C3）：windll 不带 use_last_error，OpenProcess 失败到
# 读错误码之间任何解释器层 Win32 调用都可能覆盖 last-error → 误判 DEAD → 清活锁 → 双写。
# 改 WinDLL(use_last_error=True) + ctypes.get_last_error()（kanban_window.py:57 同款姿势）。
_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_INVALID_PARAMETER = 87
ERROR_ACCESS_DENIED = 5


class LockState:
    """锁判定结果。"""
    ALIVE = "alive"          # 活锁 → 让路
    DEAD = "dead"            # 死锁 → 可清
    UNKNOWN = "unknown"      # 不明（无权限/内容损坏）→ 按活处理，超时报人工
    FREE = "free"            # 无锁


def _probe_pid(pid: int, lock_ts: float) -> str:
    """探活 pid。返回 LockState.ALIVE / DEAD / UNKNOWN。

    判定树（kimi-k3 真值表）：
      · OpenProcess 失败 + 87  → DEAD
      · OpenProcess 失败 + 5   → UNKNOWN（活着但看不见）
      · 成功 + 退出码≠STILL_ACTIVE → DEAD
      · 成功 + STILL_ACTIVE + 创建时间晚于锁 → DEAD（pid 复用）
      · 成功 + STILL_ACTIVE + 创建时间早于锁 → ALIVE
    """
    if _k32 is None:
        return LockState.UNKNOWN          # 非 Windows：无法探活，按不明处理

    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        err = ctypes.get_last_error()   # 2026-09-17 前置修复C3：配合 WinDLL(use_last_error=True)
        if err == ERROR_INVALID_PARAMETER:
            return LockState.DEAD
        if err == ERROR_ACCESS_DENIED:
            return LockState.UNKNOWN      # 关键：不能当死锁！
        return LockState.UNKNOWN

    try:
        code = ctypes.c_ulong(0)
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return LockState.UNKNOWN
        if code.value != STILL_ACTIVE:
            return LockState.DEAD

        # pid 复用防护：进程创建时间 vs 锁创建时间
        creation = ctypes.c_ulonglong(0)
        exit_t = ctypes.c_ulonglong(0)
        kern = ctypes.c_ulonglong(0)
        user = ctypes.c_ulonglong(0)
        ok = _k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                  ctypes.byref(kern), ctypes.byref(user))
        if ok and creation.value:
            # FILETIME（100ns since 1601）→ unix 秒
            proc_ts = creation.value / 1e7 - 11644473600
            if proc_ts > lock_ts + 1.0:    # 进程比锁还新 → pid 被复用，原主已死
                return LockState.DEAD
        return LockState.ALIVE
    finally:
        _k32.CloseHandle(h)


def _read_lock() -> Optional[tuple[int, float, str]]:
    """读锁文件 → (pid, ts, extra) 或 None（不存在/读不了）。"""
    if not os.path.exists(LOCK_FILE):
        return None
    try:
        content = open(LOCK_FILE, "r", encoding="utf-8").read().strip()
        pid_s, _, rest = content.partition(":")
        ts_s, _, extra = rest.partition(":")
        return int(pid_s), float(ts_s), extra
    except Exception:
        return None                        # 内容损坏（半写锁）→ 上层按 UNKNOWN 处理


def _lock_state() -> str:
    """当前锁的状态（含内容损坏分支）。"""
    parsed = _read_lock()
    if parsed is None:
        if os.path.exists(LOCK_FILE):
            # ── FIX-3（A包 C3）：半写/损坏锁的时间兜底也要在这里生效 ──
            # 半写态（如内容 "12345"）解析失败直接返回 UNKNOWN，会让下面那处
            # mtime 兜底**永远走不到**——锁永久 UNKNOWN，写入能力无声瘫痪。
            try:
                _age = time.time() - os.path.getmtime(LOCK_FILE)
            except Exception:
                _age = 0.0
            if _age > LOCK_STALE_SECONDS * 3:
                return LockState.DEAD      # 心跳早已停摆的坏锁不该永久卡住
            return LockState.UNKNOWN       # 文件在但解析失败 → 半写锁，不明
        return LockState.FREE
    pid, ts, _extra = parsed

    # 优先 pid 探活
    st = _probe_pid(pid, ts)

    # 辅以 mtime 超时（特别是 UNKNOWN 场景的唯一依据）
    try:
        age = time.time() - os.path.getmtime(LOCK_FILE)
    except Exception:
        age = time.time() - ts
    if st == LockState.ALIVE and age > LOCK_STALE_SECONDS * 3:
        # 心跳早已停摆，但 pid 仍活 —— 极度可疑：按不明处理（报人工，不自动清）
        # 注：正常长任务有 60s 心跳，mtime 会很新；mtime 老 = 心跳死了
        return LockState.UNKNOWN
    if st == LockState.UNKNOWN and age > LOCK_STALE_SECONDS:
        # ── FIX-3（A包 C3）：UNKNOWN 加时间兜底 ──
        # 半写/损坏锁 → 永远 UNKNOWN → 写入能力无声瘫痪（注释说「报人工」，但无调用方报警）。
        # 心跳早已停摆的坏锁不该永久卡住：mtime 超 LOCK_STALE_SECONDS*3（30 分钟）→ 按 DEAD 处理。
        if age > LOCK_STALE_SECONDS * 3:
            return LockState.DEAD
        return LockState.UNKNOWN           # 仍新鲜 → 保持不明（报警，不自动删）
    return st


class BoxLock:
    """上下文管理器：`with BoxLock('box_add') as lk:`。

    行为：
      · 抢锁成功 → 启动心跳线程 → yield → 停止心跳 → 释放锁（校验 pid）
      · 抢锁失败 → 重试 N 次 → 仍失败抛 LockBusy
    """

    def __init__(self, owner: str = "unknown", quiet: bool = False):
        self.owner = owner
        self.quiet = quiet
        self.held = False
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stalled = False           # 心跳停摆标志（供主线程读取入日志）
        self._pid = os.getpid()

    # ── 抢锁 ──
    def acquire(self) -> bool:
        for attempt in range(LOCK_RETRY_TIMES):
            if self._try_once():
                self.held = True
                self._start_heartbeat()
                return True
            if attempt < LOCK_RETRY_TIMES - 1:
                time.sleep(LOCK_RETRY_INTERVAL)
        raise LockBusy(f"抢锁失败（{LOCK_RETRY_TIMES} 次尝试）——锁被占用或状态不明：{LOCK_FILE}")

    def _try_once(self) -> bool:
        st = _lock_state()
        if st == LockState.FREE:
            return self._create()
        if st == LockState.DEAD:
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass
            return self._create()
        # ALIVE / UNKNOWN → 让路
        if not self.quiet:
            print(f"[lock] 锁被占用（{st}），等待中…")
        return False

    def _create(self) -> bool:
        try:
            # O_EXCL 语义：独占创建（方案 🟢-3）
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{self._pid}:{time.time()}:{self.owner}")
            return True
        except FileExistsError:
            return False                   # 竞态：别人刚建了
        except Exception:
            return False

    # ── 心跳 ──
    def _start_heartbeat(self):
        def _beat():
            try:
                while not self._hb_stop.wait(LOCK_HEARTBEAT_SECONDS):
                    try:
                        os.utime(LOCK_FILE, None)   # touch 刷新 mtime
                    except Exception:
                        pass                        # 单次失败不致命
            except Exception:
                self._hb_stalled = True             # 线程整体停摆 → 标志位

        self._hb_thread = threading.Thread(target=_beat, daemon=True,
                                           name=f"boxlock-hb-{self.owner}")
        self._hb_thread.start()

    # ── 释放 ──
    def release(self):
        if not self.held:
            return
        try:
            self._hb_stop.set()
        except Exception:
            pass
        # 释放前校验：锁是我的才删（方案 ④）
        parsed = _read_lock()
        if parsed and parsed[0] == self._pid:
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass
        self.held = False

    @property
    def heartbeat_stalled(self) -> bool:
        return self._hb_stalled

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class LockBusy(RuntimeError):
    pass


# ── 便捷函数 ──────────────────────────────────────────────
def status() -> dict:
    """查看当前锁状态（诊断用）。"""
    parsed = _read_lock()
    st = _lock_state() if os.path.exists(LOCK_FILE) else LockState.FREE
    return {
        "state": st,
        "pid": parsed[0] if parsed else None,
        "owner": parsed[2] if parsed else None,
        "age_s": (time.time() - os.path.getmtime(LOCK_FILE)) if os.path.exists(LOCK_FILE) else None,
    }


if __name__ == "__main__":
    print("=== box_lock 自检 ===")
    print("当前锁状态:", status())

    # 测试 1：抢锁 → 释放
    with BoxLock("selftest") as lk:
        print("  抢锁成功:", lk.held, "| 状态:", status()["state"])
        # 心跳线程活着
        time.sleep(0.2)
        print("  心跳线程:", lk._hb_thread.is_alive() if lk._hb_thread else None)
    print("  释放后状态:", status()["state"])

    # 测试 2：重入（同进程再抢应失败——因为锁被自己持有？不，已释放）
    print("  释放后应可再抢:", end=" ")
    with BoxLock("selftest2") as lk2:
        print(lk2.held)

    # 测试 3：僵尸锁清理（伪造一个死 pid 的锁）
    import box_config as bc
    with open(bc.LOCK_FILE, "w", encoding="utf-8") as f:
        f.write("999999:%.1f:fake" % (time.time() - 5000))   # 不存在的 pid + 老时间戳
    print("  伪造死锁后状态:", _lock_state())
    with BoxLock("selftest3") as lk3:
        print("  死锁被清理并抢锁成功:", lk3.held)

    print("✅ box_lock 自检完成")
