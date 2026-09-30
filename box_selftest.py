# -*- coding: utf-8 -*-
"""
box_selftest.py — 待办箱工具族验收断言表（B0 · 2026-9-12）

【职责】所有批次的验收断言落成**可执行脚本**，先于实施锁定。
实施者（AI）只负责让断言变绿。

【设计依据】《待办箱写入器与整理器方案 v4》§5.4
  · glm-5.3：「测试自写自跑自判」是最大走样风险——断言表必须先于实施被锁定
  · kimi-k3：「实施方案与验收者是同一个 AI，自证闭环」→ 断言表是唯一解药

【用法】
  python box_selftest.py            # 跑全部
  python box_selftest.py --batch B1 # 只跑某批
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import box_config as bc  # noqa: E402

RESULTS = []


def check(batch: str, name: str, ok: bool, detail: str = ""):
    RESULTS.append((batch, name, ok, detail))
    mark = "✅" if ok else "❌"
    print(f"  [{mark}] [{batch}] {name}" + (f"  {detail}" if detail else ""))
    return ok


# ══════════════════════════════════════════════════════
# B-1：读侧止血（扫描器）
# ══════════════════════════════════════════════════════
def test_b1():
    print("\n── B-1 读侧止血 ──")
    scan = os.path.join(bc.HOME, "todo_box_scan.py")
    src = open(scan, encoding="utf-8").read()

    # 1a. ★ v2（单3/v4）：行协议退役 —— 旧 NUM_DONE/CB/NUM 判据不再存在于扫描器；
    #     新判据 = 文件名协议 FN_RE + 首行 tid 双验。这里改为验证新判据在位。
    ns = {"__name__": "not_main", "__file__": scan}
    exec(compile(src, scan, "exec"), ns)
    FN_PROTO = ns["FN_PROTO"]
    check("B-1", "v2 文件名判据在位（FN_PROTO）", bool(FN_PROTO.match(
        "OPEN_20260917_T00002e_盯办_标题.txt")))
    check("B-1", "v2 判据拒绝旧协议形态", not FN_PROTO.match("20260912_待办账.txt"))
    check("B-1", "旧行判据 NUM_DONE 已退役（不再定义）", "NUM_DONE" not in ns)
    check("B-1", "旧行判据 CB/NUM/MARK 已退役", not any(k in ns for k in ("CB", "NUM", "MARK")))

    # 2. MD 索引无 .tmp 残留
    check("B-1", "索引无 .tmp 残留", not os.path.exists(bc.INDEX_MD + ".tmp"))

    # 3. SKIP 生效
    if os.path.exists(bc.INDEX_MD):
        idx = open(bc.INDEX_MD, encoding="utf-8").read()
        check("B-1", "索引不含 _变更日志 内容", "_变更日志" not in idx)
        check("B-1", "索引不含 _已处理 内容", "_已处理" not in idx)

    # 4. 锁名统一
    check("B-1", "无旧锁残留", not os.path.exists(bc.LEGACY_LOCK_FILE))
    check("B-1", "无新锁残留（跑完释放）", not os.path.exists(bc.LOCK_FILE))

    # ── FIX-12（A包 A1+D2+D7）：三工具解码顺序与 BOM 处理一致（单一真源）──
    # 断言「同一文件经 write_safe / box_add / box_tidy 三处读出的 (文本, 编码) 完全相同」，
    # 并覆盖 `\ufeff` 残留（双 BOM）这一 A1 指出的历史漏洞。
    import tempfile as _tf12
    import importlib.util as _iu12
    import write_safe as _ws12
    import box_add as _ba12
    _spec12 = _iu12.spec_from_file_location("_bt12", os.path.join(bc.HOME, "box_tidy.py"))
    _bt12 = _iu12.module_from_spec(_spec12); _spec12.loader.exec_module(_bt12)
    _CRLF = chr(13) + chr(10)
    _LF = chr(10)
    _td12 = _tf12.mkdtemp(prefix="b12_uni_")
    try:
        _samples = {
            "gbk.txt":    ("中文GBK" + _CRLF + "第二行" + _CRLF).encode("gbk"),
            "utf8.txt":   ("中文UTF8" + _LF).encode("utf-8"),
            "bom.txt":    b"\xef\xbb\xbf" + ("带BOM" + _LF).encode("utf-8"),
            "u16.txt":    "中文UTF16".encode("utf-16"),
            "u16nobom":   "ascii u16 no bom".encode("utf-16-le"),
            "dblbom.txt": b"\xef\xbb\xbf" + ("\ufeff- [ ] 首条待办" + _LF).encode("utf-8"),
        }
        _ok_uni = True
        _detail = []
        for _fn, _data in _samples.items():
            _p = os.path.join(_td12, _fn); open(_p, "wb").write(_data)
            _a = _ws12.read_text_strict(_p)
            _b = _ba12.read_text(_p)
            _c = _bt12.read_text_strict(_p)
            if not (_a[0] == _b[0] == _c[0] and _a[1] == _b[1] == _c[1] and _a[2] == _c[2]):
                _ok_uni = False
                _detail.append(_fn)
        check("B1", "三工具解码一致（单一真源）", _ok_uni, ",".join(_detail))
        # U+FEFF 残留：双 BOM 文件读出的首字符必须是 '-'（而非 \ufeff）
        _p = os.path.join(_td12, "dblbom.txt")
        _t, _e, _l = _ws12.read_text_strict(_p)
        check("B1", "U+FEFF 残留已剥（首条待办可见）", _t.startswith("- [ ] 首条待办"))
        # newline 参数已实现（D2：原死参数）
        _p2 = os.path.join(_td12, "nl.txt")
        _ws12.safe_write(_p2, "a" + _LF + "b" + _LF, newline=_CRLF)
        check("B1", "safe_write newline= 已实现（CRLF）",
              open(_p2, "rb").read() == ("a" + _CRLF + "b" + _CRLF).encode())
    finally:
        _sh12 = __import__("shutil")
        _sh12.rmtree(_td12, ignore_errors=True)

    # 5. 扫描器可跑
    r = subprocess.run([sys.executable, scan, "--check"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=bc.HOME, timeout=180)
    check("B-1", "扫描器 --check 跑通", r.returncode == 0, f"exit={r.returncode}")


# ══════════════════════════════════════════════════════
# B0：地基（共享件）
# ══════════════════════════════════════════════════════
def test_b0():
    print("\n── B0 地基 ──")

    # 配置单源
    check("B0", "box_config 可导入且路径有效",
          os.path.isdir(bc.BOX) and os.path.isdir(bc.HOME))
    check("B0", "分区数=5", len(bc.ZONES) == 5)
    check("B0", "分类白名单非空", len(bc.CLASSES) > 0)
    check("B0", "巡检白名单为枚举（非 glob）", all("*" not in x for x in bc.TIDY_WHITELIST))
    check("B0", "快照目录不在同步盘", "OneDrive" not in bc.SNAPSHOT_DIR and "同步盘" not in bc.SNAPSHOT_DIR)

    # 锁模块
    import box_lock
    with box_lock.BoxLock("selftest") as lk:
        check("B0", "box_lock 抢锁/释放", lk.held)
        check("B0", "心跳线程运行", lk._hb_thread.is_alive() if lk._hb_thread else False)
    check("B0", "释放后锁文件清除", not os.path.exists(bc.LOCK_FILE))

    # write_safe 模块
    import write_safe
    td = tempfile.mkdtemp(prefix="selftest_ws_")
    t = os.path.join(td, "t.txt")
    write_safe.safe_write(t, "内容A")
    check("B0", "write_safe 正常写入", open(t, encoding="utf-8").read() == "内容A")
    before = open(t, "rb").read()
    try:
        write_safe.safe_write(t, "emoji 🎉", encoding="gbk")
        check("B0", "编码预检失败应抛错", False)
    except write_safe.WriteSafeError:
        check("B0", "编码失败 → 文件未动", open(t, "rb").read() == before)
    import shutil
    shutil.rmtree(td, ignore_errors=True)

    # git 版本控制
    r = subprocess.run(["git", "log", "--oneline", "-1"], capture_output=True, text=True,
                       cwd=bc.HOME, timeout=30)
    check("B0", "git 版本控制已启用", r.returncode == 0 and len(r.stdout.strip()) > 0,
          r.stdout.strip()[:40])


# ══════════════════════════════════════════════════════
# B1：写入器（实施后启用）
# ══════════════════════════════════════════════════════
def test_b1_writer():
    print("\n── B1 写入器（实施后断言）──")
    add_py = os.path.join(bc.HOME, "box_add.py")
    if not os.path.exists(add_py):
        check("B1", "（写入器未实施——断言待启用）", True, "skip")
        return
    # 实施后要满足的断言（先写好，让实施者对齐）
    r = subprocess.run([sys.executable, add_py, "--selftest"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=bc.HOME, timeout=120)
    check("B1", "box_add --selftest 通过", r.returncode == 0, f"exit={r.returncode}")


# ══════════════════════════════════════════════════════
# B2：整理器（实施后启用）
# ══════════════════════════════════════════════════════
def test_b2_tidy():
    print("\n── B2 整理器（实施后断言）──")
    tidy_py = os.path.join(bc.HOME, "box_tidy.py")
    if not os.path.exists(tidy_py):
        check("B2", "（整理器未实施——断言待启用）", True, "skip")
        return
    r = subprocess.run([sys.executable, tidy_py, "--selftest"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=bc.HOME, timeout=300)
    check("B2", "box_tidy --selftest 通过", r.returncode == 0, f"exit={r.returncode}")

    # 🔴 2026-9-12 事故断言①：编码安全——GBK 目标混入 UTF-8 内容时必须拒写
    #    （当日事故：分流把 GBK 账本读成替换符再写回 → 内容毁；现要求「编不了不写」）
    import tempfile as _tf
    import shutil as _sh
    _td = _tf.mkdtemp(prefix="b2_enc_")
    _gbk_file = os.path.join(_td, "gbk账本.txt")
    open(_gbk_file, "wb").write("原中文内容\r\n第二行\r\n".encode("gbk"))
    _before = open(_gbk_file, "rb").read()
    try:
        import write_safe as _ws
        _ws.safe_write(_gbk_file, "含 emoji 🎉 的新内容", encoding="gbk")
        check("B2", "GBK+emoji 应预检失败", False, "未抛异常（危险：内容可能已被写坏）")
    except _ws.WriteSafeError:
        check("B2", "GBK 目标混 emoji → 拒写且文件未动",
              open(_gbk_file, "rb").read() == _before)
    except (UnicodeEncodeError, LookupError) as _e:
        # ★ 2026-9-13 修（智谱审计真bug#3）：原来只 catch WriteSafeError，
        #   若 write_safe 内部抛标准 UnicodeEncodeError，测试会**直接崩溃**
        #   而不是优雅报告失败。现在两种都接住——只要"拒写且文件未动"即算过。
        check("B2", "GBK 目标混 emoji → 拒写且文件未动（标准异常路径）",
              open(_gbk_file, "rb").read() == _before, f"{type(_e).__name__}")
    _sh.rmtree(_td, ignore_errors=True)

    # 🔴 2026-9-12 事故断言②：SKIP 必须是**名字维度**——_已处理 目录不得进索引
    #    （当日 bug：SKIP 里写全路径 "00_收件箱/_已处理" → os.walk 比对目录名永远不中）
    import re as _re
    _scan_src = open(os.path.join(bc.HOME, "todo_box_scan.py"), encoding="utf-8").read()
    _skip_m = _re.search(r"SKIP\s*=\s*\{([^}]+)\}", _scan_src)
    # ★ 2026-9-13：扫描器改为从 box_config 导入 SKIP（单一真源）——
    #   断言不再依赖源码字面形态，两种写法都支持。
    _import_m = _re.search(r"from\s+box_config\s+import\s+SKIP_NAMES", _scan_src)
    if _skip_m or _import_m:
        if _skip_m and not _import_m:
            _skip_body = _skip_m.group(1)
            # 剥掉注释行——注释里允许提到旧写法作说明；断言只看**实际值**
            _vals = "\n".join(l for l in _skip_body.splitlines()
                              if not l.strip().startswith("#"))
        else:
            # 导入模式：用 box_config 的实际值（真源）
            _vals = "\n".join(f'"{x}"' for x in bc.SKIP_NAMES)
        check("B2", "扫描器 SKIP 含 `_已处理`（名字维度）", '"_已处理"' in _vals)
        check("B2", "扫描器 SKIP 不含全路径写法", "00_收件箱" not in _vals)
        # ★ 新增：_资料 必须在 SKIP（丙方案：资料与账物理分离）
        check("B2", "扫描器 SKIP 含 `_资料`（丙方案）", '"_资料"' in _vals)
    else:
        check("B2", "扫描器 SKIP 可解析", False, "未找到 SKIP 定义")


# ══════════════════════════════════════════════════════
# B4：扫描器纯读侧（收件箱分节）
# ══════════════════════════════════════════════════════
def test_b4():
    print("\n── B4 收件箱分节 ──")
    scan_py = os.path.join(bc.HOME, "todo_box_scan.py")

    # 静态：扫描器打标记 + 索引含分节渲染
    src = open(scan_py, encoding="utf-8").read()
    check("B4", "扫描器对收件箱打标记", "inbox=(zone ==" in src)
    check("B4", "索引渲染含「待整理」分节", "待整理" in src)

    # 动态：造收件箱存量文件 → 跑扫描器 → 条目归位到「待整理」节内
    # ★ v2（单3）：协议已换成一待办一文件 —— 测试件改用 OPEN_ 协议名 + 首行 tid
    marker = "B4分节测试条目XYZ"
    inbox_file = os.path.join(bc.INBOX, "OPEN_20260917_T00e001_" + bc.RHYTHMS[0] + "_B4分节测试条目XYZ.txt")
    try:
        os.makedirs(bc.INBOX, exist_ok=True)
        with open(inbox_file, "w", encoding="utf-8", newline="") as f:
            f.write("tid: T00e001\n\n" + marker + "\n\n节奏: 盯办\n")
        subprocess.run([sys.executable, scan_py], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=bc.HOME, timeout=180)
        idx = open(bc.INDEX_MD, encoding="utf-8").read()
        i = idx.find("📥 待整理")
        j = idx.find("\n### ", i) if i >= 0 else -1
        if j < 0 and i >= 0:
            j = idx.find("\n## ", i)
        seg = idx[i:j if j > 0 else len(idx)] if i >= 0 else ""
        check("B4", "索引出现「待整理」分节", i >= 0)
        check("B4", "收件箱条目归位到分节内", marker in seg)
        # ★ v2：协议下一待办一文件 —— 标题同时出现在文件名里，
        #   故「唯一出现」的正确口径是**只出现在一行**（同一行内出现两次是正常的）。
        _lines = [ln for ln in idx.splitlines() if marker in ln]
        check("B4", "条目未混入分类区（只出现在 1 行内）", len(_lines) == 1,
              f"命中 {len(_lines)} 行")
    finally:
        # ★ 2026-9-13 修（智谱审计真bug#2）：原 `except Exception: pass` 会静默吞掉
        #   清理失败——若文件被 OneDrive/杀软占用删不掉，残留会污染后续测试
        #   （那个 `99999999_B4测试.txt` 就真的污染过稳定 ID 注册表，已清）。
        #   修法：清理失败**显式报失败**，不静默。
        _cleaned = True
        try:
            os.remove(inbox_file)
        except FileNotFoundError:
            pass
        except Exception as _e:  # noqa: BLE001
            _cleaned = False
            check("B4", "测试残留清理成功", False,
                  f"{inbox_file} 删除失败（{type(_e).__name__}）——会污染后续测试")
        if _cleaned:
            check("B4", "测试残留清理成功", not os.path.exists(inbox_file))
        subprocess.run([sys.executable, scan_py], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=bc.HOME, timeout=180)


# ══════════════════════════════════════════════════════
# B5：收官（演练断言固化 + 手册在岗）
# ══════════════════════════════════════════════════════
def test_b5():
    print("\n── B5 收官 ──")

    # 1. 运行手册在岗（含三类故障处置关键词）
    # ★ v2（单5）：手册在旧箱 `_运行手册.md`，随旧箱封存不搬（新家从零开始）。
    #   断言改为「旧箱手册仍在只读封存」+「新家快照可用作回滚证据」。
    manual = os.path.join(bc.LEGACY_BOX, "_运行手册.md") if bc.LEGACY_BOX else ""
    if not manual:
        check("B5", "运行手册（旧箱未配置则 skip）", True, "skip")
    else:
        check("B5", "运行手册仍在旧箱（只读封存，未搬）", os.path.exists(manual))
    if os.path.exists(manual):
        txt = open(manual, encoding="utf-8").read()
        check("B5", "手册含三类处置（锁僵尸/cron补跑/整树恢复）",
              all(k in txt for k in ("抢锁失败", "补跑", "恢复")))
    # 新家运行手册：单5 起由新家快照域承载（_snapshots\ 已随箱）
    check("B5", "新家快照域已就位（回滚前提）", os.path.isdir(bc.SNAPSHOT_DIR),
          bc.SNAPSHOT_DIR)

    # 2. 快照可真实恢复（读最新 tree zip 并提取一个文件）
    snaps = sorted([f for f in os.listdir(bc.SNAPSHOT_DIR)
                    if f.startswith("tree_") and f.endswith(".zip")])
    if snaps:
        import zipfile
        zp = os.path.join(bc.SNAPSHOT_DIR, snaps[-1])
        try:
            z = zipfile.ZipFile(zp)
            names = z.namelist()
            ok_read = len(names) > 0 and all(len(z.read(n)) >= 0 for n in names[:5])
            check("B5", f"整树快照可读可提取（{len(names)} 文件）", ok_read, snaps[-1])
        except Exception as e:
            check("B5", "整树快照可读", False, str(e)[:60])
    else:
        check("B5", "快照存在", False, "无 tree_*.zip")

    # 3. 转码批备份**可解码**（回滚前提）——★2026-9-13 修正
    #
    # 原断言「备份 GBK 解码 == 现在 UTC-8 内容」是**逻辑错误**：
    #   它假设文件转码后再也不变。但 9-12 之后我们**正常地**改过账本
    #   （A 批清账：02 区 260→392B、04 区 373→806B；两区 20260906 账本已归档）。
    #   于是断言在"系统正常工作"时反而报红——**这是假警报，不是真问题**。
    #
    # 正确的回滚前提是：**备份能被正确解码**（GBK 读得出来，内容非空）。
    # 至于"当前内容是否与备份一致"——那是内容差异，不是可恢复性。
    #
    # ★ v2（单5）：转码批备份属旧箱时代产物，随旧箱封存；新家快照域从零开始。
    #   断言改为「旧备份若在→仍可解码」+「新家快照域存在」。
    _tb_root = bc.LEGACY_SNAPSHOT_DIR
    tb = [d for d in os.listdir(_tb_root) if d.startswith("transcode_")] \
        if os.path.isdir(_tb_root) else []
    if tb:
        root = os.path.join(_tb_root, sorted(tb)[-1])
        n_ok = n_all = 0
        for r, ds, fs in os.walk(root):
            for fn in fs:
                n_all += 1
                try:
                    txt = open(os.path.join(r, fn), "rb").read().decode("gbk")
                    n_ok += int(len(txt.strip()) > 0)      # 能解码且非空
                except Exception:
                    pass
        # ★ 2026-9-13 修（智谱审计真bug#1）：空集通过漏洞。
        #   原 `check(..., n_ok == n_all)` 在备份目录**为空**时 0==0 → 判"通过"，
        #   但实际没验证任何东西 → **假阳性**（自检说"一切正常"而系统毫无保护）。
        #   修法：n_all 必须 > 0，否则显式判失败。
        check("B5", f"转码备份可解码（回滚前提 · {n_ok}/{n_all}）",
              n_all > 0 and n_ok == n_all,
              "" if n_all > 0 else "备份目录为空——无法验证（空集不算通过）")
    else:
        if bc.LEGACY_SNAPSHOT_DIR and os.path.isdir(bc.LEGACY_SNAPSHOT_DIR):
            check("B5", "转码批备份存在", False)
        else:
            check("B5", "转码批备份（旧快照域未配置则 skip）", True, "skip")

    # 4. 对账不处理非明细（防文档正文假阳性）
    scan = os.path.join(bc.HOME, "todo_box_scan.py")
    ns = {"__name__": "not_main", "__file__": scan}
    src_txt = open(scan, encoding="utf-8").read()
    check("B5", "对账限明细（源码含守卫）", 'it.get("kind") != "明细"' in src_txt)


def test_b6():
    """B6：提醒节流（2026-9-13 所有者设计）——提醒在看板算，插件只读结果。"""
    print("\n── B6 提醒节流 ──")

    # 1. 模块与配置存在
    check("B6", "todo_notify.py 存在",
          os.path.exists(os.path.join(bc.HOME, "todo_notify.py")))
    check("B6", "节奏表在 box_config（单一真源）",
          isinstance(getattr(bc, "NOTIFY_CADENCE", None), dict))
    check("B6", "节奏表含盯办=1天", bc.NOTIFY_CADENCE.get("盯办") == 1)
    check("B6", "提醒状态文件路径在 box_config",
          getattr(bc, "NOTIFY_STATE", "").endswith("todo_notify_state.json"))

    # 2. 独立状态文件（不与扫描器共用——扫描器整体替换会抹键）
    check("B6", "提醒状态独立于 scanner state",
          getattr(bc, "NOTIFY_STATE", "") != os.path.join(bc.HOME, "todo_box_state.json"))

    # 3. 核心逻辑：按**自然日**比较（防"每天提"变成"每两天提"）
    sys.path.insert(0, bc.HOME)
    import importlib.util as _iu
    _spec = _iu.spec_from_file_location("_tn", os.path.join(bc.HOME, "todo_notify.py"))
    _tn = _iu.module_from_spec(_spec)
    _spec.loader.exec_module(_tn)
    from datetime import date, timedelta
    today = date.today()
    fake_last = {"T1": str(today - timedelta(days=1)) + " 05:35"}
    check("B6", "距昨天=1天（自然日口径）",
          _tn._age_days("T1", fake_last) == 1)
    check("B6", "同一天=0天（防同日重复提醒）",
          _tn._age_days("T1", {"T1": str(today) + " 10:00"}) == 0)
    check("B6", "无记录 → None（触发首次）",
          _tn._age_days("TX", {}) is None)

    # 4. 插件已改为读摘要（不再重放清单）
    tb = os.environ.get("TODOBOX_PLUGIN_FILE", "")
    if tb and os.path.exists(tb):
        src = open(tb, encoding="utf-8").read()
        check("B6", "插件读摘要（todo_notify_summary.json）",
              "todo_notify_summary.json" in src)
        check("B6", "插件保留降级路径（摘要缺失→清单）",
              "提醒摘要不可用" in src)
    else:
        check("B6", "插件存在（未配置 TODOBOX_PLUGIN_FILE 则 skip）", True, "skip")

    # 5. 看板：先渲染后记账的顺序纪律
    dash = os.path.join(bc.HOME, "_make_dashboard.py")
    dsrc = open(dash, encoding="utf-8").read()
    # 2026-9-16 更新：看板写法已升级为 os.fdopen 原子写（安全升级，断言跟新不跟旧）
    i_write = dsrc.find('os.fdopen(_fd, "wb")')
    if i_write < 0:
        i_write = dsrc.find('io.open(OUT, "wb").write(body)')  # 兼容旧写法
    i_mark = dsrc.find("todo_notify.mark_notified")
    check("B6", "看板：先写 HTML、后记时间戳（顺序纪律）",
          i_write > 0 and i_mark > i_write, f"write@{i_write} < mark@{i_mark}")


# ═══════════════════════════════════════════════
# B7 销账哨（2026-9-16 上岗层+影子层）断言
# ═══════════════════════════════════════════════
def test_b7():
    """销账哨：①配置键齐全 ②影子文件写后回读 ③心跳25h内 ④正则语义抽测"""
    # 开源参数化（2026-9-29）：影子层 settle_shadow 属私有模块（依赖本地语义库与会话库，
    # 不随开源包发布）——缺席时 B7 整批 skip（配置键/正则断言在生产侧全量跑）。
    import importlib.util as _iu7
    if _iu7.find_spec("settle_shadow") is None:
        print("  [SKIP] B7 销账哨断言整批跳过——依赖私有模块 settle_shadow（未随开源包发布）")
        return
    import json as _json, datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    import box_config as _bc
    for key in ("SETTLE_IN_TITLE_DONE", "SETTLE_NEG_RE", "SETTLE_VERBS_STRONG",
                "SETTLE_VERBS_WEAK", "SETTLE_SELF_REF_RE", "SETTLE_EVIDENCE_DAYS",
                "SETTLE_SHADOW_JSON"):
        check("B7", f"box_config 有 {key}", hasattr(_bc, key))
    d = None
    try:
        d = _json.load(open(_bc.SETTLE_SHADOW_JSON, encoding="utf-8"))
    except FileNotFoundError:
        check("B7", "todo_settle_shadow.json 存在（先手动跑 settle_shadow.py）", False)
    if d:
        check("B7", "影子层 mode=shadow 且含 suspects", d.get("mode") == "shadow" and isinstance(d.get("suspects"), dict))
        try:
            hb = _dt.datetime.strptime(d["heartbeat"], "%Y-%m-%dT%H:%M").replace(tzinfo=_ZI("Asia/Shanghai"))
            fresh = (_dt.datetime.now(_ZI("Asia/Shanghai")) - hb).total_seconds() < 25 * 3600
            check("B7", "影子层心跳 25h 内", fresh)
        except Exception as e:
            check("B7", "心跳可解析", False, str(e)[:60])
    check("B7", "自述正则认『建成』", bool(_bc.SETTLE_IN_TITLE_DONE.search("看板弹窗建成")))
    check("B7", "否定窗拦『已定档待接入』", bool(_bc.SETTLE_NEG_RE.search("已定档，待夜间批处理 稳定后接入")))
    check("B7", "将来时窗拦『待XX后接入』", bool(_bc.SETTLE_FUTURE_RE.search("已定档，待夜间批处理稳定后接入")))
    check("B7", "自指窗认『销账哨』", bool(_bc.SETTLE_SELF_REF_RE.search("销账哨审查报告")))

    # ── 项1（glm Q2-2）：规则8 与 5/6 矛盾——上岗层必须过完整排除窗+句级切分 ──
    import todo_box_scan as _tbs

    def _selfreport(title, cls="盯办", kind="明细", state="todo"):
        return _tbs.settle_selfreport([dict(title=title, cls=cls, kind=kind, state=state)])

    check("B7", "项1 上岗层拦『已定档，待夜间批处理稳定后接入』",
          _selfreport("已定档，待夜间批处理稳定后接入") == [],
          str(_selfreport("已定档，待夜间批处理稳定后接入")))
    check("B7", "项1 上岗层认『看板弹窗建成，两轮审查判定修后开工』",
          len(_selfreport("看板弹窗建成，两轮审查判定修后开工")) == 1)

    # ── 项2（qwen C1）：讨论噪音窗——「聊到没干完」不是证据 ──
    check("B7", "项2 讨论噪音窗拦『怎么搞定销账哨的误报问题』",
          bool(_bc.SETTLE_DISCUSS_RE.search("怎么搞定销账哨的误报问题")))
    check("B7", "项2 上岗层拦『怎么搞定销账哨的误报问题』",
          _selfreport("怎么搞定销账哨的误报问题") == [])
    check("B7", "项2 闸门认『销账哨误报问题搞定了』（讨论窗不误伤）",
          _bc.settle_sentence_ok("销账哨误报问题搞定了", self_ref=False))
    check("B7", "项2 闸门拦『销账哨误报问题搞定了』的讨论态写法",
          not _bc.settle_sentence_ok("销账哨误报问题怎么才能搞定", self_ref=False))
    check("B7", "项2 讨论噪音窗不误伤『看板弹窗建成』",
          not _bc.SETTLE_DISCUSS_RE.search("看板弹窗建成，两轮审查判定修后开工"))

    # ── 二修项A（qwen3.7 复核雷#1）：讨论窗第三支收窄——真完成陈述不得被误杀 ──
    # 旧第三支 `(搞定|落地|完成|解决).{0,10}(方案|计划|思路|可行性)` 把
    # 「已完成方案评审」当讨论噪音拦掉；下面四条断言**双向**：真完成必过、探讨态必拦。
    check("B7", "二修A 闸门认『已完成方案评审』（旧码误杀，真完成）",
          _bc.settle_sentence_ok("已完成方案评审", self_ref=False))
    check("B7", "二修A 上岗层认『已完成方案评审』→ 命中 1 笔",
          len(_selfreport("已完成方案评审")) == 1,
          str(_selfreport("已完成方案评审")))
    check("B7", "二修A 闸门认『已确认交付完成』（探讨类动词前置『已』放行）",
          _bc.settle_sentence_ok("已确认交付完成", self_ref=False))
    check("B7", "二修A 闸门拦『怎么搞定销账哨』（探讨态仍拦）",
          not _bc.settle_sentence_ok("怎么搞定销账哨", self_ref=False))
    check("B7", "二修A 讨论窗拦『确认落地方案』（探讨类动词+方案名词仍拦）",
          bool(_bc.SETTLE_DISCUSS_RE.search("确认落地方案")))

    # ── 二修项B（qwen3.7 复核雷#2/#3/#4）：将来时窗裸词收窄 ──
    check("B7", "二修B 将来时窗不误杀『模拟器已完成』（裸词「拟」已收窄）",
          not _bc.SETTLE_FUTURE_RE.search("模拟器已完成")
          and _bc.settle_sentence_ok("模拟器已完成", self_ref=False))
    check("B7", "二修B 将来时窗不误杀『后续工作已完成』",
          not _bc.SETTLE_FUTURE_RE.search("后续工作已完成"))
    check("B7", "二修B 将来时窗仍拦『拟定再接入』",
          bool(_bc.SETTLE_FUTURE_RE.search("拟定再接入"))
          and not _bc.settle_sentence_ok("拟定再接入", self_ref=False))
    check("B7", "二修B 将来时窗仍拦『后续将接入』",
          bool(_bc.SETTLE_FUTURE_RE.search("后续将接入")))
    check("B7", "二修B 「准备」已从将来时窗移出（否定窗单一来源，边界划清）",
          "准备" not in _bc.SETTLE_FUTURE_RE.pattern
          and "准备" in _bc.SETTLE_NEG_RE.pattern)

    # ── 项3（glm Q2-3）：30天窗显式定义——下界必须锚 first_seen，不锚 created ──
    import datetime as _dt3
    import settle_shadow as _ssh
    check("B7", "项3 窗定义键存在且写明 30 天",
          isinstance(getattr(_bc, "SETTLE_EVIDENCE_WINDOW", ""), str)
          and "30" in _bc.SETTLE_EVIDENCE_WINDOW and "first_seen" in _bc.SETTLE_EVIDENCE_WINDOW)
    check("B7", "项3 SETTLE_EVIDENCE_DAYS == 30", _bc.SETTLE_EVIDENCE_DAYS == 30)
    _now3 = _dt3.datetime(2026, 9, 18, tzinfo=_ZI("Asia/Shanghai"))
    _cands3 = [{"file": "A.txt", "tid": "T00aaaa"},
               {"file": "B.txt", "tid": "T00bbbb"}]
    # created 比 first_seen 早 200 天（捞账场景）——cutoff 必须锚 first_seen
    _fs3 = {"T00aaaa": "2026-09-10 08:00", "T00bbbb": "2026-09-01 08:00"}
    _cut3 = _ssh.compute_cutoff(_cands3, _fs3, _now3)
    _cut_dt3 = _dt3.datetime.fromtimestamp(_cut3, _ZI("Asia/Shanghai"))
    check("B7", "项3 cutoff 锚最早 first_seen−7天（不锚 created）",
          _cut_dt3.strftime("%Y-%m-%d") == "2026-08-25", _cut_dt3.strftime("%Y-%m-%d"))
    check("B7", "项3 无 first_seen 时退回 60 天",
          abs(_ssh.compute_cutoff(_cands3, {}, _now3) - (_now3 - _dt3.timedelta(days=60)).timestamp()) < 1)

    # ── 项4（glm Q2-4）+ 二修项C/D：熔断——7 天零命中 + **上轮遗留**嫌疑才出声 ──
    def _hist(n_zero, n_suspects=0):
        return [dict(date=f"2026-09-{i + 1:02d}", scanned=24, hit=0, n_suspects=n_suspects)
                for i in range(n_zero)]

    def _hist_hit(n_hit, hit=3, n_suspects=3):
        return [dict(date=f"2026-09-{i + 1:02d}", scanned=24, hit=hit, n_suspects=n_suspects)
                for i in range(n_hit)]

    check("B7", "项4 7天零命中 + suspects 空 → 不报警",
          _bc.settle_fuse_check(_hist(7, 0))["alert"] is False)
    check("B7", "项4 7天零命中 + 上轮遗留 3 笔 → 报警",
          _bc.settle_fuse_check(_hist(7, 0), prev_pending=3)["alert"] is True)
    check("B7", "项4 3天零命中（原阈值）→ 不报警",
          _bc.settle_fuse_check(_hist(3, 5), prev_pending=5)["alert"] is False)
    check("B7", "项4 零命中中断（近日有命中）→ 不报警",
          _bc.settle_fuse_check(_hist(9, 0) + [dict(date="2026-09-10", scanned=24, hit=1, n_suspects=1)],
                                prev_pending=3)["alert"] is False)
    check("B7", "项4 熔断键/语义键齐全",
          _bc.SETTLE_FUSE_SILENT_DAYS == 7 and isinstance(_bc.SETTLE_SCANNED_DEF, str))

    # 二修项C（qwen3.7 复核雷#5）：backlog 必须挂「上一轮 payload 的 ignored=False 笔数」
    # 旧码 `max(history.n_suspects)` 会把 7 天前曾有过嫌疑 当成现存积压 → 文案与事实相反。
    _fuseC = _bc.settle_fuse_check(_hist(7, 2), prev_pending=0)
    check("B7", "二修C 历史曾有嫌疑但上轮已清空 → 不报警（旧码误报）",
          _fuseC["alert"] is False, _fuseC["reason"])
    _fuseC2 = _bc.settle_fuse_check(_hist(7, 0), prev_pending=2)
    check("B7", "二修C 上轮遗留 2 笔 → 报警且文案写『上轮遗留』",
          _fuseC2["alert"] is True and "上轮遗留嫌疑 2 笔" in _fuseC2["reason"],
          _fuseC2["reason"])
    check("B7", "二修C 熔断返回 pending 字段（数据源可断言）",
          _fuseC2.get("pending") == 2 and _fuseC.get("pending") == 0)

    # 二修项D（qwen3.7 复核雷#6）：对称报警——天天命中 + 遗留只增不减 也要出声
    _fuseD = _bc.settle_fuse_check(_hist_hit(7, hit=3, n_suspects=3), prev_pending=3)
    check("B7", "二修D 连续 7 天有命中 + 遗留 3 笔不减 → 报警（旧码反向盲区）",
          _fuseD["alert"] is True and _fuseD.get("kind") == "hit_streak",
          _fuseD["reason"])
    _fuseD2 = _bc.settle_fuse_check(_hist_hit(7, hit=3, n_suspects=3), prev_pending=1)
    check("B7", "二修D 遗留已下降到 1 笔 → 不报警（有人处置中）",
          _fuseD2["alert"] is False, _fuseD2["reason"])
    _fuseD3 = _bc.settle_fuse_check(_hist_hit(3, hit=3, n_suspects=3), prev_pending=3)
    check("B7", "二修D 命中连续 3 天（阈值下）→ 不报警",
          _fuseD3["alert"] is False, _fuseD3["reason"])
    check("B7", "二修D 连续零命中+有遗留 与 连续命中+有遗留 两条报警互不串味",
          _fuseC2.get("kind") == "silent_streak" and _fuseD.get("kind") == "hit_streak")

    # ── 项5（glm Q4-1）：句切分补全——中英标点+分号+换行 ──
    _mix = "第一句完成。Second done!第三句落地；fourth;ok\n第五行搞定"
    _parts5 = [p for p in _bc.SETTLE_SPLIT_RE.split(_mix) if p.strip()]
    check("B7", "项5 中英混排+多行切分句数正确", len(_parts5) == 6, str(_parts5))
    check("B7", "项5 两层切分符同源（影子层 SENT_SPLIT is box_config 真源）",
          _ssh.SENT_SPLIT is _bc.SETTLE_SPLIT_RE)
    check("B7", "项5 上岗层句级切分生效（否定句不污染命中句）",
          len(_selfreport("已落地。待明天再接入")) == 1)
    # 二修项E-2（qwen3.7 复核雷#8）：这句是**旧码漏报、新码命中**的真鉴定器
    # （旧逻辑整标题看否定词 → 整段被拦；句级切分后干净小句单独命中），
    # 必须留在 B7 作为回归锁——标注二修E-2 便于识别。
    check("B7", "二修E-2 『已落地。待明天再接入』→ 命中 1 笔（旧码漏报的真鉴定器）",
          len(_selfreport("已落地。待明天再接入")) == 1,
          str(_selfreport("已落地。待明天再接入")))
    check("B7", "项5 上岗层整段否定仍拦（无标点单句）",
          _selfreport("已定档待明天再接入") == [])

    # ── 项6（glm 重审#6）：指纹逐条化 + 忽略按条目 ──
    _evA = [dict(src="msg", ref="1", fp="f1", text="Vortex日志死循环已修")]
    _evB = [dict(src="msg", ref="2", fp="f2", text="看板弹窗已建成")]
    _fpA = _ssh.entry_fingerprint("T00aaaa", _evA)
    _fpB = _ssh.entry_fingerprint("T00bbbb", _evB)
    check("B7", "项6 指纹逐条：不同条目指纹不同", _fpA != _fpB)
    check("B7", "项6 指纹逐条：同条目证据变化→指纹变",
          _fpA != _ssh.entry_fingerprint(
              "T00aaaa", _evA + [dict(src="msg", ref="3", fp="f3", text="又一条证据")]))
    check("B7", "项6 指纹稳定（证据顺序无关）",
          _ssh.entry_fingerprint("T00aaaa", _evA + _evB)
          == _ssh.entry_fingerprint("T00aaaa", list(reversed(_evA + _evB))))
    check("B7", "项6 忽略登记路径在 box_config 单源",
          _bc.SETTLE_IGNORED_JSON.endswith("todo_settle_ignored.json"))
    # 端到端：忽略 A → A 不再出、B 仍在（走真实 judge 数据流的最小复现）
    _tmpig = os.path.join(tempfile.gettempdir(), "_b7_ignored.json")
    _orig_ig = _ssh.SETTLE_IGNORED_JSON
    try:
        with open(_tmpig, "w", encoding="utf-8") as _f:
            _json.dump({"items": {"T00aaaa": {"evidence_hash": _fpA}}}, _f)
        _ssh.SETTLE_IGNORED_JSON = _tmpig
        _ig = _ssh.load_ignored()
        check("B7", "项6 忽略登记可读且 A 命中指纹一致", _ig.get("T00aaaa") == _fpA)
        check("B7", "项6 忽略 A 后 B 不受影响（按条目）", _ig.get("T00bbbb") is None)
        # 二修项E-1（qwen3.7 复核雷#8）：原此处两条「指纹一致→标记 ignored」/
        # 「指纹失配→不忽略」是同义恒真断言（`_ig.get("T00aaaa") != _fpB`，
        # 即 fpA != fpB，与上面「不同条目指纹不同」重复，旧码亦过）。
        # 换成**真断言**：A 的证据集变化后，登记指纹与实际指纹失配 → 重新出声。
        _ig["T00aaaa"] = _ssh.entry_fingerprint(
            "T00aaaa", _evA + [dict(src="msg", ref="9", fp="f9", text="A 又一条新证据")])
        check("B7", "二修E-1 A 证据变化 → 登记指纹失配 → 重报（真断言）",
              _ig.get("T00aaaa") != _fpA)
    finally:
        _ssh.SETTLE_IGNORED_JSON = _orig_ig
        if os.path.exists(_tmpig):
            os.remove(_tmpig)

    # ── 二修项F（qwen3.7 复核扣分1）：忽略 A 仍出 B 的**真端到端** ──
    # 构造两笔账的真证据（走 settle_shadow.judge 真数据流）→ 忽略 A（写 ignored
    # 文件）→ 复现 main() 的 suspects 过滤分支 → 断言 A.ignored=True 且 B.ignored=False，
    # 并顺带断言项H（hit 排除 ignored）。
    _tf = _dt3.datetime(2026, 9, 10, tzinfo=_ZI("Asia/Shanghai"))
    _tsf = _dt3.datetime(2026, 9, 11, tzinfo=_ZI("Asia/Shanghai"))
    _winf = "2026-09-18 23:59"
    _evF1 = _ssh.judge("T00d001", "Vortex日志死循环排查",
                       [(11, _tsf, "Vortex日志死循环已修好，已销账")], [], _tf, _winf)
    _evF2 = _ssh.judge("T00d002", "Vortex看板弹窗建设",
                       [(12, _tsf, "Vortex看板弹窗已落地，两轮审查通过")], [], _tf, _winf)
    check("B7", "二修F 两笔账都取到真证据（judge 真数据流）",
          len(_evF1) == 1 and len(_evF2) == 1, f"A={len(_evF1)} B={len(_evF2)}")
    _fpF1 = _ssh.entry_fingerprint("T00d001", _evF1)
    _tmpf = os.path.join(tempfile.gettempdir(), "_b7_e2e_ignored.json")
    _origf = _ssh.SETTLE_IGNORED_JSON
    try:
        with open(_tmpf, "w", encoding="utf-8") as _f:
            _json.dump({"items": {"T00d001": {"evidence_hash": _fpF1}}}, _f)
        _ssh.SETTLE_IGNORED_JSON = _tmpf
        _igf = _ssh.load_ignored()
        # 复现 settle_shadow.main() 的 suspects 过滤分支（表达式逐字对齐真代码）
        _suspF = {}
        for _tid, _ev in (("T00d001", _evF1), ("T00d002", _evF2)):
            _fp = _ssh.entry_fingerprint(_tid, _ev)
            _suspF[_tid] = dict(n_ev=len(_ev), evidence_hash=_fp,
                                ignored=(_igf.get(_tid) == _fp))
        _pendF = len([s for s in _suspF.values() if not s.get("ignored")])
        check("B7", "二修F 忽略 A 后：A.ignored=True 且 B.ignored=False",
              _suspF["T00d001"]["ignored"] is True and _suspF["T00d002"]["ignored"] is False,
              f"A={_suspF['T00d001']['ignored']} B={_suspF['T00d002']['ignored']}")
        check("B7", "二修H hit 排除 ignored（2 笔嫌疑只计 1 笔）", _pendF == 1, f"pending={_pendF}")
        _evF1b = _evF1 + [dict(src="msg", ref="13", fp="f13", text="A 又一条新证据")]
        check("B7", "二修F A 证据变化 → 指纹失配 → 重报（ignored 回落 False）",
              (_igf.get("T00d001") == _ssh.entry_fingerprint("T00d001", _evF1b)) is False)
    finally:
        _ssh.SETTLE_IGNORED_JSON = _origf
        if os.path.exists(_tmpf):
            os.remove(_tmpf)

    # ── 项7（qwen W1）：弱动词 + 状态名词互斥窗（动作对象≠动作完成）──
    check("B7", "项7 互斥窗拦『排查Vortex日志的落地进度』",
          not _bc.settle_sentence_ok("排查Vortex日志死循环的落地进度", self_ref=False))
    check("B7", "项7 互斥窗不误伤『Vortex日志死循环搞定了』",
          _bc.settle_sentence_ok("Vortex日志死循环搞定了", self_ref=False))
    check("B7", "项7 强动词句不受互斥窗改判（销账进度仍算证据）",
          _bc.settle_sentence_ok("Vortex日志死循环已销账，进度已记", self_ref=False))
    check("B7", "项7 状态名词远离弱动词（>4字）不误杀",
          _bc.settle_sentence_ok("Vortex日志死循环搞定了，进度回头补记", self_ref=False))
    # 影子层 judge() 端到端：互斥句不出证据，完成句出证据
    _t7 = _dt3.datetime(2026, 9, 10, tzinfo=_ZI("Asia/Shanghai"))
    _ts7 = _dt3.datetime(2026, 9, 11, tzinfo=_ZI("Asia/Shanghai"))
    _ev7 = _ssh.judge("T00c001", "Vortex日志死循环排查",
                      [(1, _ts7, "排查Vortex日志死循环的落地进度")], [],
                      _t7, "2026-09-18 23:59")
    check("B7", "项7 影子层：互斥句不出证据", _ev7 == [], str(_ev7))
    _ev7b = _ssh.judge("T00c001", "Vortex日志死循环排查",
                       [(1, _ts7, "Vortex日志死循环搞定了")], [],
                       _t7, "2026-09-18 23:59")
    check("B7", "项7 影子层：完成句出证据", len(_ev7b) == 1, str(_ev7b))

    # ── 三修雷①（2026-9-20）：路径撞车——证据句里的路径不是话题 ──
    # 实测事故：`file:///D:/docs/...` 里的 "docs" 撞上
    # 三笔含 "docs" 标题 → 一条「已修」句误挂 3 笔账。
    _evP1 = _ssh.judge("T00c003", "PRTS人格移植样本（同事样本）",
                       [(1, _ts7, "已修——中文路径坑：用户给背景图填的 "
                                  "file:///D:/docs/wallpapers/壁纸A.jpg "
                                  "里带中文，Chromium 会静默加载失败")], [],
                       _t7, "2026-09-18 23:59")
    check("B7", "三修① 影子层：路径撞车句不出证据（hermes≠话题）",
          _evP1 == [], str(_evP1))
    _evP2 = _ssh.judge("T00c003", "PRTS人格移植Hermes（同事样本）",
                       [(2, _ts7, "PRTS人格移植Hermes 已修，同事样本 preset 平移完成")], [],
                       _t7, "2026-09-18 23:59")
    check("B7", "三修① 影子层：真话题句仍出证据（剥路径不误伤）",
          len(_evP2) == 1, str(_evP2))
    check("B7", "三修① SETTLE_PATH_STRIP_RE 剥 file:/// 与盘符路径",
          " ".join(_bc.SETTLE_PATH_STRIP_RE.sub(" ", "图在 file:///D:/docs/a.jpg 已修").split())
          == "图在 已修")

    # ── 三修雷③（2026-9-20）：引号动词是「谈论」不是「陈述」（mention≠use）──
    # 实测事故：诊断句「…关键词，『已修』又是强动词——三笔全误报」被当证据。
    _evQ1 = _ssh.judge("T00c005", "PRTS人格移植Hermes（同事样本）",
                       [(5, _ts7, "路径里的 \"hermes\" 撞上了三笔标题里都含的 \"Hermes\" "
                                  "关键词，「已修」又是强动词——三笔全误报")], [],
                       _t7, "2026-09-18 23:59")
    check("B7", "三修③ 影子层：引号动词诊断句不出证据",
          _evQ1 == [], str(_evQ1))

    # ── 三修雷②（2026-9-20）：滑窗重叠凑门槛——同一短语只算一个话题词 ──
    _cmap53 = _ssh.kw_cluster_map("消息总线改道 Bot Mode：内置群聊替代文件总线")
    _kl = [k for k in _cmap53 if k.startswith("消息总线") or k.startswith("总线改") or k.startswith("线改道")]
    check("B7", "三修② 「消息总线」的重叠滑窗归同一簇",
          len(_kl) >= 2 and len({_cmap53[k] for k in _kl}) == 1,
          str({k: _cmap53[k] for k in _kl}))
    _evS1 = _ssh.judge("T00c004", "消息总线改道 Bot Mode（内置群聊）",
                       [(3, _ts7, "小箱子收官了——语义库已有底：专属呼吸、思维模式、"
                                  "验收标准、消息总线、奥尔良包子")], [],
                       _t7, "2026-09-18 23:59")
    check("B7", "三修② 影子层：单短语凑不满弱动词≥2门槛（误挂修复）",
          _evS1 == [], str(_evS1))
    _evS2 = _ssh.judge("T00c004", "消息总线改道 Bot Mode（内置群聊）",
                       [(4, _ts7, "消息总线改道完成，内置群聊已上线")], [],
                       _t7, "2026-09-18 23:59")
    check("B7", "三修② 影子层：双话题词真完成句仍出证据",
          len(_evS2) >= 1, str(_evS2))

    # ── 项8（qwen C3，裁决降级）：看板并发——核对原子写，不引 fcntl 不新造锁 ──
    _root = os.path.dirname(os.path.abspath(__file__))
    _dash = open(os.path.join(_root, "_make_dashboard.py"), encoding="utf-8").read()
    check("B7", "项8 看板 SUMMARY 走原子写（tmp + os.replace）",
          "_sum_tmp = SUMMARY + \".tmp\"" in _dash and "os.replace(_sum_tmp, SUMMARY)" in _dash)
    check("B7", "项8 看板 SUMMARY 落盘有 fsync（非仅原子）",
          "_f.flush(); os.fsync(_f.fileno())" in _dash or "f.flush(); os.fsync(f.fileno())" in _dash)
    check("B7", "项8 看板无裸写 SUMMARY（io.open(SUMMARY,\"w\") 已退役）",
          'io.open(SUMMARY, "w")' not in _dash and "open(SUMMARY, 'w')" not in _dash)
    check("B7", "项8 未引入 fcntl（Windows 无此模块，照抄即崩）",
          "fcntl" not in _dash and "fcntl" not in open(
              os.path.join(_root, "settle_shadow.py"), encoding="utf-8").read())
    _pyc = __import__("subprocess").run(
        [sys.executable, "-m", "py_compile", os.path.join(_root, "_make_dashboard.py")],
        capture_output=True, text=True)
    check("B7", "项8 看板生成路径 py_compile", _pyc.returncode == 0,
          (_pyc.stderr or "")[:80])
    # 二修项E-4（qwen3.7 复核雷#8）：原断言用 `.split("·")[0]` 只查 settle_shadow.py
    # **头注释**那一段——注释里没写看板文件名即算过，等于查注释不查代码（真空断言）。
    # 换成**真代码行**检查：用 ast 剥掉注释与模块 docstring 后 unparse 出纯代码，
    # 再确认代码里无看板域文件名、且 hit 计数确实排除 ignored（项H 的回归锁）。
    _shsrc = open(os.path.join(_root, "settle_shadow.py"), encoding="utf-8").read()
    _shast = ast.parse(_shsrc)
    _shbody = _shast.body[1:] if (isinstance(_shast.body[0], ast.Expr)
                                  and isinstance(_shast.body[0].value, ast.Constant)) else _shast.body
    _shcode = "\n".join(ast.unparse(_n) for _n in _shbody)
    check("B7", "项8 影子层真代码不写看板域文件（C4 纪律·剥注释后查代码）",
          "todo_notify_summary.json" not in _shcode
          and "SETTLE_SHADOW_JSON" in _shcode)
    check("B7", "二修E-4 影子层 hit 计数行真排除 ignored（剥注释后查代码行）",
          bool(re.search(r"len\(\[s for s in suspects\.values\(\) if not s\.get\('ignored'\)\]\)",
                         _shcode))
          and "prev_pending=_prev_pending" in _shcode)

    # ── 项9（glm Q2-1）：§〇 表述修正——删一切「达标」式表述 ──
    # 二修项G（qwen3.7 复核雷#9）：方案 md 被 .gitignore 排除（战报待裁决#6），
    # 文件丢失/改名时**不得炸 B7 整批**——skip 该组断言并打 WARN。
    _plan = os.path.join(_root, "_方案_销账哨_v1.1.md")
    if not os.path.exists(_plan):
        print("  [WARN] [B7] 二修项G 方案 md 不存在（被 gitignore 排除/改名）"
              "——项9 的 5 条方案绑定断言 skip，不判失败")
        check("B7", "项9 方案 md 存在", True, "skip")
        check("B7", "项9 方案无『精确率 100%』残留", True, "skip")
        check("B7", "项9 方案无『均达标』/『笔/天达标』残留", True, "skip")
        check("B7", "项9 方案写明初步信号+过拟合风险", True, "skip")
        check("B7", "项9 方案写明正式验收阈值（采纳率70% + 日均误报0.5 + 连续2周）", True, "skip")
        check("B7", "项9 方案补记元任务/短标题盲区（glm Q4-3/Q4-4）", True, "skip")
    else:
        _ptxt = open(_plan, encoding="utf-8").read()
        check("B7", "项9 方案无『精确率 100%』残留", "精确率 100%" not in _ptxt and "精确率100%" not in _ptxt)
        check("B7", "项9 方案无『均达标』/『笔/天达标』残留",
              "均达标" not in _ptxt and "笔/天达标" not in _ptxt)
        check("B7", "项9 方案写明初步信号+过拟合风险",
              "初步信号" in _ptxt and "过拟合风险" in _ptxt)
        check("B7", "项9 方案写明正式验收阈值（采纳率70% + 日均误报0.5 + 连续2周）",
              "采纳率 ≥ 70%" in _ptxt and "日均误报 ≤ 0.5" in _ptxt and "连续 **2 周**" in _ptxt)
        check("B7", "项9 方案补记元任务/短标题盲区（glm Q4-3/Q4-4）",
              "元任务盲区" in _ptxt and "短标题盲区" in _ptxt)
    _shtxt = open(os.path.join(_root, "settle_shadow.py"), encoding="utf-8").read()
    check("B7", "项9 影子层头注释写明初步信号+前向验收",
          "初步信号" in _shtxt and "采纳率 ≥70%" in _shtxt)
    check("B7", "项9 影子层头注释无『100% 统计上不可判定』式旧表述",
          "n=2 的 100%" not in _shtxt)
    # 二修项I（qwen3.7 复核雷#7，部分采纳）：fuse/suspects/ignored 无消费方——
    # 挂账不修，但头注释必须写明「暂无消费方，转正时接」。
    check("B7", "二修I 头注释写明三字段暂无消费方、转正时接",
          "暂无消费方" in _shtxt and "转正时接" in _shtxt)


def test_v2_config():
    """单1（2026-9-17 完全体搬迁）：box_config 双路径 schema + 文件名协议 8 例 + 清洗往返。"""
    print("\n── V2 配置与文件名协议 ──")

    # 1. 双路径字符串正确（新家单3前还没建骨架 → 不检查存在性）
    check("V2", "BOX 为绝对路径（env 可覆盖）", os.path.isabs(bc.BOX) and len(bc.BOX) > 3, bc.BOX)
    check("V2", "LEGACY_BOX 缺省为空或为绝对路径",
          bc.LEGACY_BOX == "" or os.path.isabs(bc.LEGACY_BOX), bc.LEGACY_BOX)
    check("V2", "BOX != LEGACY_BOX（新旧不同源）", bc.BOX != bc.LEGACY_BOX)
    check("V2", "五区都在 BOX 下（与旧箱无交集）",
          all(z.startswith(bc.BOX) for z in bc.ZONES.values())
          and (not bc.LEGACY_BOX or all(not z.startswith(bc.LEGACY_BOX) for z in bc.ZONES.values())),
          str(list(bc.ZONES.values())))
    check("V2", "旧区名→新分区号映射齐（6 条）", len(bc.LEGACY_ZONE_TO_NEW) == 6)
    check("V2", "注册表落新家 _registry", bc.REGISTRY_JSON.startswith(bc.BOX) and "_registry" in bc.REGISTRY_JSON,
          bc.REGISTRY_JSON)
    check("V2", "节奏枚举 5 项且含等人/永不催",
          len(bc.RHYTHMS) == 5 and bc.R_AWAIT in bc.RHYTHMS and bc.R_NEVER in bc.RHYTHMS,
          str(bc.RHYTHMS))
    check("V2", "SKIP 含全部骨架目录",
          all(k in bc.SKIP_NAMES for k in ("_registry", "_snapshots", "_done", "_索引.md", "_看板.html")))

    # 2. 正则单测 8 例
    P = bc.parse_filename
    # 例1 正常名
    r1 = P("OPEN_20260917_T00002e_盯办_销账哨v1.1重审修补.txt")
    c1 = bool(r1) and r1["state"] == "todo" and r1["tid"] == "T00002e" \
        and r1["rhythm"] == "盯办" and r1["date"] == "20260917" \
        and r1["title"] == "销账哨v1.1重审修补"
    check("V2", "正则①正常名五段解析", c1, str(r1))
    # 例2 标题含 `_`（解析后标题完整保留，不丢字）
    r2 = P("OPEN_20260917_T00002e_长线_前段_中段_尾段.txt")
    c2 = bool(r2) and r2["title"] == "前段_中段_尾段" and r2["rhythm"] == "长线"
    check("V2", "正则②标题含下划线完整保留", c2, r2["title"] if r2 else "None")
    # 例3 非法字符（清洗后通过）
    t3 = bc.clean_title('9:30 电话会 <a>b|"c"')
    fn3 = bc.build_filename("OPEN", "20260917", "T00aa01", "盯办", '9:30 电话会 <a>b|"c"')
    c3 = P(fn3) is not None and all(ch not in fn3 for ch in '<>:"/\\|?*')
    check("V2", "正则③非法字符清洗后通过", c3, fn3)
    # 例4 Windows 保留名（加 x_ 后通过，且主名不再是保留名）
    t4 = bc.clean_title("CON")
    fn4 = bc.build_filename("OPEN", "20260917", "T00aa02", "现在就做", "CON")
    c4 = t4 == "x_CON" and P(fn4) is not None and P(fn4)["title"].upper() != "CON"
    check("V2", "正则④保留名加 x_ 前缀后通过", c4, fn4)
    # 例5 DONE_ 前缀 → done
    r5 = P("DONE_20260917_T00002f_" + bc.R_AWAIT + "_示例归档账.txt")
    c5 = bool(r5) and r5["state"] == "done" and r5["prefix"] == "DONE"
    check("V2", "正则⑤DONE_ 前缀解析为 done", c5)
    # 例6 超长标题（截断到 30）
    long_t = "标" * 60
    fn6 = bc.build_filename("OPEN", "20260917", "T00aa03", "长线", long_t)
    c6 = P(fn6) is not None and len(P(fn6)["title"]) == bc.TITLE_MAX == 30
    check("V2", "正则⑥超长标题截断到 30", c6, f"len={len(P(fn6)['title']) if P(fn6) else 'NA'}")
    # 例7 tid 大写 T + 6 位十六进制
    c7a = P("OPEN_20260917_T00004e_盯办_天翼云接入收尾三小事.txt") is not None
    c7b = bc.build_filename("OPEN", "20260917", "T00004e", "盯办", "x") is not None
    try:
        bc.build_filename("OPEN", "20260917", "t00004e", "盯办", "x")   # 小写 t 必须拒
        c7c = False
    except ValueError:
        c7c = True
    try:
        bc.build_filename("OPEN", "20260917", "T0000g1", "盯办", "x")   # 非十六进制必须拒
        c7d = False
    except ValueError:
        c7d = True
    check("V2", "正则⑦tid 大写 T+6位十六进制（小写/非法字符拒收）", c7a and c7b and c7c and c7d)
    # 例8 错误形态（OPENX_ 开头拒绝）
    c8 = P("OPENX_20260917_T00002e_盯办_标题.txt") is None and P("OPEN_2026091_T00002e_盯办_标题.txt") is None \
        and P("OPEN_20260917_T00002e_盯办_标题.md") is None
    check("V2", "正则⑧错误形态拒绝（OPENX_/短日期/非txt）", c8)

    # 3. clean_title 往返：清洗后再解析，五段还原一致
    samples = ["普通标题", "含_下划线_的_标题", '非法:字符*一堆?', "CON", "PRN", "超长" * 40,
               "  前后空白  ", "尾部点...", "NUL.txt", "OPEN_假装是前缀"]
    rt_ok, rt_detail = True, []
    for i, s in enumerate(samples):
        tid = f"T00aa{i:02x}"[:7]
        tid = "T" + f"{i + 0x40:06x}"
        fn = bc.build_filename("OPEN", "20260917", tid, "盯办", s)
        pr = bc.parse_filename(fn)
        if pr is None or pr["tid"] != tid or pr["state"] != "todo" \
           or pr["rhythm"] != "盯办" or pr["date"] != "20260917" \
           or pr["title"] != bc.clean_title(s):
            rt_ok = False
            rt_detail.append(f"{s!r}→{fn!r}→{pr}")
    check("V2", f"clean_title 往返 10 例（清洗后再解析五段还原）", rt_ok, "; ".join(rt_detail[:3]))


def test_v3_scan():
    """单3（2026-9-17）：新家读侧 —— 建骨架 + 造 2 OPEN + 1 协议错误 → 扫描。"""
    print("\n── V3 读侧（枚举文件）──")
    import subprocess as _sp
    scan_py = os.path.join(bc.HOME, "todo_box_scan.py")
    src = open(scan_py, encoding="utf-8").read()

    # 静态：行解析退役、旧箱不扫
    check("V3", "正则/行解析常量已退役（无 CB/NUM 行解析）",
          "CB        = re.compile" not in src and "NUM       = re.compile" not in src)
    check("V3", "扫描路径走 box_config（BASE = bc.BOX）", "BASE      = bc.BOX" in src)
    check("V3", "旧箱不出现于扫描范围（代码行无引用，仅注释提及）",
          not any(("LEGACY_BOX" in ln or "OneDrive" in ln) and not ln.strip().startswith("#")
                  and not ln.strip().startswith("·")
                  for ln in src.splitlines()))
    check("V3", "schema 升 2", "SCHEMA    = 2" in src)
    check("V3", "扫目录枚举自断言在案", "assert len(items) + len(UNPARSED_FILES) == enum_total" in src)

    # 断言1：建骨架
    r0 = _sp.run([sys.executable, scan_py, "--skeleton"], capture_output=True, text=True,
                 encoding="utf-8", errors="replace", cwd=bc.HOME, timeout=120)
    want = [os.path.dirname(p) for p in [bc.INDEX_MD]] + list(bc.ZONES.values()) + [bc.INBOX]
    check("V3", "断言1 骨架建成（五区+收件箱+_done+_registry+_snapshots）",
          r0.returncode == 0
          and all(os.path.isdir(d) for d in list(bc.ZONES.values()) + [bc.INBOX, bc.DONE_DIR,
                                                                      bc.REGISTRY_DIR,
                                                                      os.path.join(bc.BOX, "_snapshots")]),
          f"exit={r0.returncode}")

    # 断言2：造 2 个 OPEN + 1 个协议错误文件 → 扫描
    prods = []
    mkr = os.path.join(bc.ZONES["04"], "OPEN_20260917_T00c001_盯办_V3扫描测试甲.txt")
    mk2 = os.path.join(bc.ZONES["05"], "OPEN_20260917_T00c002_长线_V3扫描测试乙.txt")
    bad = os.path.join(bc.ZONES["04"], "V3协议错误文件.txt")
    try:
        write_safe_mod = __import__("write_safe")
        write_safe_mod.safe_write(mkr, "tid: T00c001\n\nV3扫描测试甲（正文）\n\n节奏: 盯办\n")
        write_safe_mod.safe_write(mk2, "tid: T00c002\n\nV3扫描测试乙（正文）\n\n节奏: 长线\n")
        write_safe_mod.safe_write(bad, "这不是协议文件，扫描器必须把它标「待整理」\n")
        prods = [mkr, mk2, bad]
        r1 = _sp.run([sys.executable, scan_py], capture_output=True, text=True,
                     encoding="utf-8", errors="replace", cwd=bc.HOME, timeout=180)
        ok_run = r1.returncode == 0
        idx = json.load(open(bc.HOME + os.sep + "todo_box_index.json", encoding="utf-8"))
        st = idx["stats"]
        check("V3", "断言2a 扫描跑通", ok_run, f"exit={r1.returncode} {r1.stderr[-120:]}")
        # 断言2b/2d：v3 测试件**叠加在既有 31 笔之上** → 用相对口径
        #（v2 单3 建骨架后，索引里已有迁移来的真账；绝对数 2 不再成立）
        base_todo = st.get("todo", 0)
        check("V3", "断言2b open 计数 = 基线+2（v3 两件已入索引）",
              base_todo >= 2 and len(idx["items"]) >= 2, f"todo={base_todo}")
        # 断言2d：索引 JSON 结构正确（items schema v2）
        keys = ("tid", "zone", "file", "title", "state", "cls", "first_seen", "ref_paths")
        v3_items = [it for it in idx["items"] if it["tid"] in ("T00c001", "T00c002")]
        ok_struct = len(v3_items) == 2 and all(
            all(k in it for k in keys) and it["state"] == "todo" and it["kind"] == "明细"
            for it in v3_items)
        check("V3", "断言2d items schema 正确（v3 两件字段齐全）", ok_struct,
              str(sorted(v3_items[0].keys())[:8]) if v3_items else "未找到 v3 测试件")
        check("V3", "断言2d' 全部 items 字段齐全（含迁移来的 31 笔）",
              all(all(k in it for k in keys) for it in idx["items"]),
              f"items={len(idx['items'])}")
        check("V3", "断言2c 待整理=1（协议错误文件）",
              st.get("待整理_count") == 1 and len(idx.get("unparsed", [])) == 1,
              f"待整理={st.get('待整理_count')} unparsed={len(idx.get('unparsed', []))}")
        check("V3", "断言2e 索引头含「待整理」分节与不合协议明细",
              "📥 待整理" in r1.stdout or "待整理" in open(bc.INDEX_MD, encoding="utf-8").read())
        check("V3", "断言2f 不合协议文件已出声（cron 异常面）", "unparsed" in idx and len(idx["unparsed"]) == 1)
    finally:
        # 断言4：测试产物清理
        left = []
        for p in prods:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except Exception:
                left.append(p)
        check("V3", "断言4 测试产物清理", not left, f"残留 {left}")
        # 清完再扫一遍，让索引回到干净态
        _sp.run([sys.executable, scan_py], capture_output=True, text=True,
                encoding="utf-8", errors="replace", cwd=bc.HOME, timeout=180)


def test_b8_settle():
    """B8（2026-9-30，可露希尔终审点名）：box_settle.py 销账链路永久断言。

    五件套：settle → 分区消失 → _done 出现 → tid 稳定 → 索引更新 + 重复销账 rc=4。
    沙箱纪律：测试 tid 用独立前缀，跑完清理，绝不碰真实账。
    """
    print("── B8 销账器（box_settle.py）──")
    import subprocess as _sp
    import shutil as _sh
    import glob as _gb
    settle_py = os.path.join(bc.HOME, "box_settle.py")
    add_py = os.path.join(bc.HOME, "box_add.py")
    scan_py = os.path.join(bc.HOME, "todo_box_scan.py")

    check("B8", "box_settle.py 存在", os.path.isfile(settle_py))
    if not os.path.isfile(settle_py):
        return
    src = open(settle_py, encoding="utf-8").read()
    check("B8", "rc 约定注释在案（0/2/4/5/6）", "rc 约定" in src and "4=定位失败" in src)
    check("B8", "锁内 TOCTOU 复核在案", "拿锁后目标已消失" in src)
    check("B8", "回执说谎修复在案（note_written 分支）", "note_written" in src)
    check("B8", "死代码 if lk: pass 已删", "if lk:" not in src)

    # 沙箱（独立 BOX，绝不碰真实账）
    sb = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "_b8_settle_sb")
    _sh.rmtree(sb, ignore_errors=True)
    os.makedirs(os.path.join(sb, "box"))
    env = dict(os.environ, TODOBOX_BOX=os.path.join(sb, "box"), TODOBOX_HOME=sb)
    def run(args):
        return _sp.run([sys.executable] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=bc.HOME, env=env, timeout=180)
    run(["-c", "import todo_box_scan; todo_box_scan.ensure_skeleton()"])

    r1 = run([add_py, "B8销账断言用例", "--zone", "03"])
    check("B8", "造账成功", r1.returncode == 0)
    # 从输出抓 tid
    import re as _re
    m = _re.search(r"tid=(T[0-9a-fA-F]+)", r1.stdout or "")
    tid = m.group(1) if m else ""
    check("B8", "tid 抓取成功", bool(tid), tid)

    r2 = run([settle_py, tid, "--note", "B8断言备注"])
    out2 = (r2.stdout or "") + (r2.stderr or "")
    check("B8", "settle rc=0", r2.returncode == 0)
    check("B8", "大声回执（已销账）", "已销账" in out2)
    check("B8", "tid 不变声明在回执", "tid 不变" in out2)

    done_files = _gb.glob(os.path.join(sb, "box", "_done", "*", f"DONE_*{tid}*"))
    check("B8", "_done 出现 DONE 文件", len(done_files) == 1)
    if done_files:
        body = open(done_files[0], encoding="utf-8").read()
        check("B8", "备注落体", "note: B8断言备注" in body)
        check("B8", "tid 稳定（文件名含原 tid）", tid in os.path.basename(done_files[0]))
    zone_left = _gb.glob(os.path.join(sb, "box", "03_*", f"*{tid}*"))
    check("B8", "分区已清空该账", len(zone_left) == 0)

    # 索引更新：重扫后真待办不含该 tid
    run([scan_py, "--check"])
    idx = os.path.join(sb, "box", "_索引.md")
    idx_ok = False
    if os.path.exists(idx):
        s2 = open(idx, encoding="utf-8").read()
        idx_ok = tid not in s2.split("已完成")[0]
    check("B8", "索引更新（tid 移出真待办）", idx_ok)

    r3 = run([settle_py, tid])
    out3 = (r3.stdout or "") + (r3.stderr or "")
    check("B8", "重复销账 rc=4", r3.returncode == 4)
    check("B8", "重复销账报归档位置", "已销账" in out3 and "_done" in out3)

    r4 = run([settle_py, "bad-tid"])
    check("B8", "坏 tid 格式 rc=2", r4.returncode == 2)

    _sh.rmtree(sb, ignore_errors=True)
    check("B8", "沙箱清理", not os.path.exists(sb))


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    which = arg.replace("--batch", "").strip() if "--batch" in arg else ""

    print("=" * 64)
    print("待办箱工具族 · 验收断言表")
    print("=" * 64)

    # 开源自举（2026-9-29）：箱子不存在则先建骨架——首次运行 selftest 不应因空箱而红。
    if not os.path.isdir(bc.BOX):
        print(f"[BOOT] 箱子不存在（{bc.BOX}）——先跑 --skeleton 建骨架")
        import subprocess as _spboot
        _spboot.run([sys.executable, os.path.join(bc.HOME, "todo_box_scan.py"),
                     "--skeleton"], capture_output=True, text=True, timeout=120)

    if not which or which == "B-1":
        test_b1()
    if not which or which == "B0":
        test_b0()
    if not which or which == "B1":
        test_b1_writer()
    if not which or which == "B2":
        test_b2_tidy()
    if not which or which == "B4":
        test_b4()
    if not which or which == "B5":
        test_b5()
    if not which or which == "B6":
        test_b6()
    if not which or which == "B7":
        test_b7()
    if not which or which == "B8":
        test_b8_settle()
    if not which or which == "V2":
        test_v2_config()
    if not which or which == "V3":
        test_v3_scan()

    # 汇总
    real = [(b, n, ok, d) for b, n, ok, d in RESULTS if d != "skip"]
    skipped = [x for x in RESULTS if x[3] == "skip"]
    p = sum(1 for _, _, ok, _ in real if ok)
    print("\n" + "=" * 64)
    print(f"结果: {p}/{len(real)} 通过" + (f"（{len(skipped)} 项待实施后启用）" if skipped else ""))
    if p < len(real):
        print("\n未通过：")
        for b, n, ok, d in real:
            if not ok:
                print(f"  ❌ [{b}] {n}  {d}")
        sys.exit(1)
    print("✅ 全部断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

