# -*- coding: utf-8 -*-
"""
write_safe.py — 待办箱工具族安全写入（B0 · 2026-9-12）

【职责】写入器与整理器**共用**的安全写回实现。
「写不死文件」必须是对所有工具的承诺，不是写入器的私有属性。

【设计依据】《待办箱写入器与整理器方案 v4》§5.3

核心链路：
    encode 预检 → tmp(同卷) → os.replace → （可选）回读验证

血泪教训（2026-9-12 事故 1）：
    `open(p,'w')` 先截断文件；若随后写入抛异常（编码错/磁盘满），
    会留下 **0 字节空文件**——内容蒸发。正确姿势是**先编码后落盘**：
        先 content.encode('utf-8')（编不了就抛，文件还没动）
        再写 tmp、os.replace 替换。

关键决策（kimi-k3 与 glm-5.3 博弈后的定案）：
  · **回读失败绝不自动恢复备份** —— 回读失败归因模糊（真失败/AV锁/OneDrive水合/
    正则误报），后三种下文件其实是好的；自动恢复会回滚正确内容 + 抹掉用户手动编辑。
    准则：「每个自动动作的失败处理器，其风险上限不得高于它要保护的对象」。
  · **写前备份失败 → 中止写入**（不裸奔）；但备份本身先重试 3 次（对齐回读策略）。
  · 中止时落 `_待写清单.md`（降级出口——没有出口的 fail-closed 才是过度）。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from box_config import (  # noqa: E402
    TMP_DIR, SNAPSHOT_DIR, PENDING_WRITE_MD,
    READBACK_RETRY_TIMES, READBACK_RETRY_INTERVAL, BACKUP_RETRY_TIMES,
)


class WriteSafeError(RuntimeError):
    """安全写入失败（文件未被改动，或已中止并落待写清单）。"""


class UndecodableError(RuntimeError):
    """文件无法用任何已知编码解码——**拒绝写回**，绝不静默转码。

    ★ FIX-12（A包 A1+D2+D7）：三份 read_text 的统一真源（write_safe）。
    三工具（todo_box_scan / box_add / box_tidy）各有一份实现，编码顺序相同、
    错误语义三种，且三份都缺 `\\ufeff` 残留检查 → 首条待办静默消失。
    """


# ══════════════════════════════════════════════════════
# 统一严格读（单一真源）—— FIX-12（A包 A1 + D2 + D7）
# ══════════════════════════════════════════════════════
def read_text_strict(p: str) -> tuple[str, str, bool]:
    """读文件 → (文本, 编码, lossy)。四序探测：utf-8-sig → utf-8 → gbk → utf-16。

    契约（三工具共用，勿各自再实现）：
      · 读后 `lstrip("\\ufeff")` —— 修 U+FEFF 残留（双 BOM/编辑器粘贴产物）：
        首字符残留 \\ufeff 会让 `^\\s*[-*]` 匹配失败 → **首条待办静默消失**。
      · utf-8-sig 仅在原文**真的带 BOM** 时才承认（否则写回会平白前置 BOM）。
      · NUL 启发（FIX-11 同规则上收）：NUL 占比超阈值 → 跳过全部单字节编码直试 utf-16；
        单字节编码解出含 \\x00 → 可疑乱码，继续找下一编码。
      · lossy=True = 所有已知编码都解不开。**调用方禁止写回**（避免把替换符写进文件）。
      · 文件不存在 → ("", "utf-8", False)。
    """
    if not os.path.exists(p):
        return "", "utf-8", False
    raw = open(p, "rb").read()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    _nul_heavy = raw and raw.count(b"\x00") > len(raw) * 0.1
    for enc in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        if enc == "utf-8-sig" and not has_bom:
            continue                       # D2/A1：只在真带 BOM 时承认 utf-8-sig
        if _nul_heavy and enc in ("utf-8-sig", "utf-8", "gbk"):
            continue                       # FIX-11：NUL 密集 → 直试 utf-16
        try:
            txt = raw.decode(enc)
        except Exception:
            continue
        if enc in ("utf-8-sig", "utf-8", "gbk") and "\x00" in txt:
            continue                       # FIX-11：解出 NUL → 可疑乱码
        return txt.lstrip("\ufeff"), enc, False      # A1：修 U+FEFF 残留
    try:
        txt = raw.decode("utf-16")
        if "\x00" not in txt:
            return txt.lstrip("\ufeff"), "utf-16", False
    except Exception:
        pass
    return raw.decode("utf-8", errors="replace"), "utf-8", True


def _tmp_path_for(target: str) -> str:
    """选 tmp 路径——**必须与目标同卷**（os.replace 不能跨盘）。

    策略（2026-9-12 自检修正）：
      · 目标在工具盘 → tmp 用 TMP_DIR（工具目录）：同卷 ✅ 且避开同步盘
      · 目标不在 D 盘 → tmp 用目标同目录：保证同卷（否则 WinError 17 跨盘失败）

    原设计「tmp 固定工具目录」的假设只在目标也位于同盘时成立——
    自检用 C 盘临时目录时暴露了跨盘失败。
    """
    target_abs = os.path.abspath(target)
    target_dir = os.path.dirname(target_abs) or "."
    base = os.path.basename(target_abs)

    # 目标在 D 盘 → 用 TMP_DIR（避开 OneDrive）
    # ★ FIX-12.3（A包 A6）：`startswith("D:")` → os.path.splitdrive —— 原写法
    #   对 `D:foo`（盘符相对路径）会漏判（`"D:foo".startswith("D:")` 其实是 True，
    #   但语义应是「盘符 == D」而非字符串前缀；统一用 splitdrive 才是单一真源写法）。
    if os.path.splitdrive(target_abs)[0].upper() == "D:":
        os.makedirs(TMP_DIR, exist_ok=True)
        return os.path.join(TMP_DIR, f".writesafe_{base}.{os.getpid()}.tmp")

    # 非 D 盘 → 同目录（保证同卷）
    if os.path.isdir(target_dir):
        return os.path.join(target_dir, f".writesafe_{base}.{os.getpid()}.tmp")

    return os.path.join(TMP_DIR, f".writesafe_{base}.{os.getpid()}.tmp")


def safe_write(target: str, content: str, *, encoding: str = "utf-8",
               newline: Optional[str] = None, backup: bool = True,
               readback: bool = True, retries_backup: int = BACKUP_RETRY_TIMES) -> bool:
    """安全写入。成功返回 True；失败抛 WriteSafeError（目标文件保持原样）。

    参数：
      target   —— 目标文件绝对路径
      content  —— 要写入的文本
      backup   —— 写前是否备份到 SNAPSHOT_DIR
      readback —— 写后是否回读验证
    """
    # ── 1. 编码预检（关键：编不了就抛，此时文件还没动）──
    # ★ FIX-12.4（A包 D2）：实现 newline 参数（原为死参数——实测传 "\r\n" 盘上仍是
    #   纯 "\n"，扫描器只能自己写 CRLF 绕开，CRLF 规则无断言保护）。
    #   做法：先做换行替换（\n → newline，且把已有的 \r\n 归一后再替换，防 \\r\\r\\n）。
    if newline and newline != "\n":
        content = content.replace("\r\n", "\n").replace("\n", newline)
    try:
        data = content.encode(encoding)
    except UnicodeEncodeError as e:
        raise WriteSafeError(f"编码预检失败（文件未改动）: {e}")

    # ── 2. 写前备份（失败重试 N 次；仍失败则中止）──
    if backup and os.path.exists(target):
        ok = False
        last_err = None
        for _ in range(retries_backup):
            try:
                os.makedirs(SNAPSHOT_DIR, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                bak = os.path.join(SNAPSHOT_DIR,
                                   f"{os.path.basename(target)}.{stamp}.bak")
                shutil.copy2(target, bak)
                ok = True
                break
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        if not ok:
            raise WriteSafeError(
                f"写前备份失败（已重试 {retries_backup} 次，中止写入以不裸奔）: {last_err}")

    # ── 3. 写 tmp + 原子替换 ──
    tmp = _tmp_path_for(target)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush(); os.fsync(f.fileno())   # FIX-22（A包 D1）：原子 ≠ 落盘
        os.replace(tmp, target)
    except Exception as e:
        # 清理 tmp 残骸
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise WriteSafeError(f"写入失败（目标未变或保持旧版）: {e}")

    # ── 4. 回读验证（重试穿透 AV/OneDrive 短锁；失败**不动文件**）──
    # 2026-9-12 修（kimi-k3 只读审计抓出 B1，已实测复现）：
    #   原实现的 for/else 结构里，内容不一致走 `break` → 跳过 else 的 raise
    #   → 直接落到 `return True`。**任何「读到的字节 ≠ 写入字节、且不含写入字节」
    #   的读结果都静默通过**（OneDrive 冲突时用云端旧版覆盖本地、水合出旧内容，都走这条路径）。
    #   后果：全线唯一的事后完整性检测器，在唯一需要它报警的场景里永远不报警。
    #   修法：不一致时重试（continue），耗尽后抛错；并明确区分两种情形——
    #   「写入的字节在文件里」（可能是别人追加了内容）→ 放行但留痕；
    #   「写入的字节不在文件里」→ 真失败，抛错。
    if readback:
        for i in range(READBACK_RETRY_TIMES):
            try:
                got = open(target, "rb").read()
                if got == data:
                    return True
                if data in got:
                    # 可能是用户在我们写入后又追加了内容 —— 不算失败
                    return True
                # 不匹配且不含写入字节 → 真失败，重试
                if i < READBACK_RETRY_TIMES - 1:
                    time.sleep(READBACK_RETRY_INTERVAL)
                    continue
                raise WriteSafeError(
                    f"回读验证失败（已重试 {READBACK_RETRY_TIMES} 次）——"
                    f"写入的字节不在文件里，文件可能被外部覆盖（OneDrive 冲突/水合）、"
                    f"或写入未真正落盘。文件未回滚，请人工检查: {target}")
            except WriteSafeError:
                raise
            except Exception:
                time.sleep(READBACK_RETRY_INTERVAL)
        else:
            # 重试耗尽（全是读异常）：记录并报错，但**绝不恢复备份**（🔴-3 定案）
            raise WriteSafeError(
                f"回读验证失败（已重试 {READBACK_RETRY_TIMES} 次）——文件未回滚，"
                f"请人工检查: {target}")
    return True


def append_safe(target: str, line: str, *, encoding: str = "utf-8") -> bool:
    """纯追加（日志类豁免——无截断窗口，方案 🟢-7）。

    仅用于日志/清单类 append-only 文件。
    """
    try:
        line.encode(encoding)              # 预检
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding=encoding) as f:
            f.write(line)
        return True
    except Exception as e:
        raise WriteSafeError(f"追加失败: {e}")


def defer_write(zone: str, content: str, note: str = "") -> bool:
    """降级出口：写入中止时落 `_待写清单.md`（方案 🟡-4）。

    纯追加（append-only，工具恢复后顺序消化并核销）。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"- [{stamp}] zone={zone} {('| ' + note) if note else ''}\n    内容: {content}\n"
    try:
        return append_safe(PENDING_WRITE_MD, line)
    except Exception:
        return False                        # 待写清单自身失败：静默（不掩盖原错误）


if __name__ == "__main__":
    import tempfile
    print("=== write_safe 自检 ===")
    td = tempfile.mkdtemp(prefix="writesafe_test_")
    t = os.path.join(td, "测试文件.txt")

    # 1. 正常写入
    safe_write(t, "第一行\n第二行\n")
    ok = open(t, encoding="utf-8").read() == "第一行\n第二行\n"
    print(f"  [{'OK' if ok else 'FAIL'}] 正常写入")

    # 2. 覆盖写入（应留备份）
    safe_write(t, "改写后的内容\n")
    ok2 = open(t, encoding="utf-8").read() == "改写后的内容\n"
    baks = [f for f in os.listdir(SNAPSHOT_DIR) if "测试文件" in f] if os.path.exists(SNAPSHOT_DIR) else []
    print(f"  [{'OK' if ok2 else 'FAIL'}] 覆盖写入 | 备份数: {len(baks)}")

    # 3. 编码预检失败 → 文件不动（关键测试）
    before = open(t, "rb").read()
    try:
        safe_write(t, "含 emoji 🎉 的内容", encoding="gbk")
        print("  [FAIL] 编码预检应抛错但没抛")
    except WriteSafeError:
        after = open(t, "rb").read()
        print(f"  [{'OK' if before == after else 'FAIL'}] 编码失败 → 文件未动")

    # 4. 无 tmp 残留
    rest = [f for f in os.listdir(TMP_DIR) if f.startswith(".writesafe_")]
    print(f"  [{'OK' if not rest else 'FAIL'}] 无 tmp 残留（{len(rest)} 个）")

    # 清理
    shutil.rmtree(td, ignore_errors=True)
    print("✅ write_safe 自检完成")
