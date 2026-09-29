#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
box_add.py — 待办箱写入器 v2（2026-9-17 完全体搬迁 · 单2）

【v2 语义变更】写 = **建文件**（一待办一文件），不再是往账本追加行。
    旧 v1：`- [ ] [分类] 内容` 追加进 `YYYYMMDD_待办账.txt`
    新 v2：`OPEN_YYYYMMDD_<tid>_<节奏>_<标题>.txt` —— 文件名即协议

【设计依据】《方案_待办箱完全体搬迁_20260917》v1.1 §1.2 / §三 / 工单单2

【新增安全点】
  · tid 铸造走 todo_id.assign，但 **测试模式用临时注册表**（BOX_TEST_REGISTRY 环境变量
    或 --selftest），C7 教训：绝不污染生产注册表
  · 重名检测：同分区内标题 norm 相同 → rc=3（输入面从「行」改「文件」）
  · 收件箱降级：分区判定失败 → 落 00_收件箱，节奏字段用「待整理」
  · 文件名清洗走 box_config.clean_title（单一真源）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import box_config as bc  # noqa: E402
import write_safe  # noqa: E402
from box_lock import BoxLock, LockBusy  # noqa: E402

#: 测试模式临时注册表（C7 教训：selftest 绝不写生产注册表）
TEST_REG = os.path.join(
    os.environ.get("TEMP") or os.path.expanduser("~"),
    "_box_test_registry.json")


def registry_target() -> str | None:
    """返回本轮应使用的注册表路径；None = 走 todo_id 生产路径。

    优先级：环境变量 BOX_TEST_REGISTRY > --selftest 的临时路径 > 生产。
    """
    env = os.environ.get("BOX_TEST_REGISTRY")
    if env:
        return env
    if "--selftest" in sys.argv:
        return TEST_REG
    return None


def _read_body_file(p: str) -> str:
    """读已有待办文件正文（严格解码，解不开就拒——沿用铁律）。"""
    return read_text(p)[0]


def _norm(s: str) -> str:
    """归一化（重复检测用）：去掉格式符号、分类标记与空白。"""
    s = re.sub(r"^\s*[-*]\s*\[[ xX]\]\s*", "", str(s))
    s = re.sub(r"^[\[【](" + "|".join(bc.CLASSES + ("现在",)) + r")[\]】]\s*", "", s)
    return re.sub(r"\s+", "", s)




# ══════════════════════════════════════════════════════
# 读文件（多编码，与扫描器同策略）
# ══════════════════════════════════════════════════════
class UndecodableError(RuntimeError):
    """文件无法用任何已知编码解码——**拒绝写入**，绝不静默转码。

    2026-9-12 修（glm-5.3 只读审计抓出，已实测复现）：
      原实现在四种编码全失败时 `return raw.decode("utf-8", errors="replace"), "utf-8"`
      —— 返回**替换后的文本**并声称编码是 utf-8。调用方无从得知发生了替换：
      GBK 中文全变成 U+FFFD → 以 UTF-8 写回 → 整个账本被毁。
      这与 9-12 上午的 0 字节事故**等价**，但更隐蔽——那次是崩溃（看得见），
      这次是打印「✓ 已写入」、退出码 0（看不见）。
    """


def read_text(p: str) -> tuple[str, str]:
    """读文件 → (文本, 编码)。文件不存在时返回 ("", "utf-8")。

    ⚠ 与 `todo_box_scan.read_text` 共享同一铁律：**解不开就拒绝，绝不替换**。
    ★ FIX-12（A包 A1+D2+D7）：本函数原为三份拷贝之一，现收敛为
      `write_safe.read_text_strict`（单一真源）的薄包装——它已内含
      「只在真带 BOM 时承认 utf-8-sig」与「lstrip U+FEFF 残留」两条修正。
    """
    txt, enc, lossy = write_safe.read_text_strict(p)
    if lossy:
        raise UndecodableError(
            f"文件无法用任何已知编码解码（utf-8-sig/utf-8/gbk/utf-16 全部失败）: {p}\n"
            f"  已拒绝写入——文件保持原样，未做任何替换或转码。\n"
            f"  处置：用工具确认实际编码后人工转码，或把该文件移出待办箱另存。")
    return txt, enc


# ══════════════════════════════════════════════════════
# 文件名清洗（Windows 非法字符 + 长度）—— v2 走 box_config 单源
# ══════════════════════════════════════════════════════
def sanitize_filename(s: str) -> str:
    """清洗为 Windows 合法文件名（薄包装 → box_config.clean_title 单一真源）。"""
    return bc.clean_title(s)


# ══════════════════════════════════════════════════════
# v2：目标文件 = 新文件（一待办一文件）
# ══════════════════════════════════════════════════════
def resolve_zone_dir(zone_id: str | None) -> tuple[str, str, bool]:
    """解析目标分区目录。

    返回 (目录, 节奏, 是否降级)。zone 不可用 → 降级落收件箱（节奏「待整理」）。
    """
    if zone_id and zone_id in bc.ZONES:
        return bc.ZONES[zone_id], "", False
    return bc.INBOX, bc.RHYTHM_INBOX, True


def mint_tid(title: str, zone_id: str | None) -> str:
    """铸造全新 tid —— 走 todo_id.mint_fresh（写侧专用铸号入口）。

    ⚠ 为什么不用 todo_id.assign：assign 的相似度复用判据是给扫描器追同一笔账的，
      对写入器会把「标题相近的新账」误认成旧账（单2 实测抓出，详见 todo_id.mint_fresh 注释）。

    ⚠ 测试模式（--selftest / BOX_TEST_REGISTRY）**必须**用临时注册表路径，
      否则 selftest 产物会污染生产注册表（C7 教训）。
    """
    import todo_id
    tgt = registry_target()
    if tgt:
        todo_id.REG = tgt
        os.makedirs(os.path.dirname(tgt), exist_ok=True)
        if not os.path.exists(tgt):
            todo_id.save({"schema": 1, "next_seq": 0, "items": {}})
    rel = os.path.join(bc.ZONES.get(zone_id, bc.INBOX), title)
    return todo_id.mint_fresh(title, f"{rel}:1", title)


def existing_tid_files() -> dict:
    """全箱枚举：tid → 文件名清单（重名/uniqueness 检测用）。"""
    out: dict = {}
    for d in list(bc.ZONES.values()) + [bc.INBOX]:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            pr = bc.parse_filename(fn)
            if pr:
                out.setdefault(pr["tid"], []).append(os.path.join(d, fn))
    return out


def duplicate_check(zone_id: str | None, title: str) -> str | None:
    """同分区内标题 norm 相同的 **未完成** 待办 → 返回那个文件名，否则 None。

    v2：输入面从「行」改为「文件」——同分区已有 OPEN_ 文件标题 norm 相同即为重复。
    """
    zdir, _rh, _dg = resolve_zone_dir(zone_id)
    if not os.path.isdir(zdir):
        return None
    want = _norm(title)
    if not want:
        return None
    for fn in sorted(os.listdir(zdir)):
        pr = bc.parse_filename(fn)
        if not pr or pr["state"] != "todo":
            continue
        full = os.path.join(zdir, fn)
        try:
            body = _read_body_file(full)
        except UndecodableError:
            continue
        # 标题来源：文件名（协议）优先；文件体首行 tid 只作校验
        if _norm(pr["title"]) == want:
            return fn
    return None


def build_body(tid: str, content: str, rhythm: str, ref: str = "", first_seen: str = "") -> str:
    """构造文件体（协议 §1.2）：首行 tid 标记 → 完整描述 → 上下文 → first_seen → 节奏。"""
    content = str(content).strip()
    lines = [f"tid: {tid}", "", content]
    if ref:
        lines += ["", f"上下文: {ref}"]
    lines += ["", f"first_seen: {first_seen or time.strftime('%Y-%m-%d %H:%M')}",
              f"节奏: {rhythm}"]
    return "\n".join(lines) + "\n"


def add(content: str, zone: str | None, type_: str, cls: str | None,
        date_str: str | None, force: bool) -> int:
    """写入一条待办 = 建一个文件。返回退出码。

    退出码：0 成功 ｜ 2 内容空 ｜ 3 重名 ｜ 4 抢锁失败 ｜ 5 写失败 ｜ 6 解码/编码受阻
    """
    # 0. 空箱守卫（FIX-2 / 前置修复 C1 沿用）：箱子不在 → 拒绝静默新建整棵树
    if not os.path.isdir(bc.BOX):
        print(f"✗ 箱子本体不存在，拒绝写入（防凭空造箱）: {bc.BOX}")
        write_safe.defer_write(zone or "00", content, "箱子不存在（拒绝静默新建）")
        return 5

    # 1. 节奏判定 + 分区判定（不可用 → 收件箱降级，不静默改写）
    rhythm = cls or bc.RHYTHM_DEFAULT
    diverted = False
    reason = ""
    if rhythm not in bc.RHYTHMS:
        diverted = True
        reason = f"节奏「{rhythm}」不在枚举 {bc.RHYTHMS} → 转入收件箱待整理"
        rhythm = bc.RHYTHM_INBOX
    if not zone or zone not in bc.ZONES:
        diverted = True
        reason = reason or "未指定/未知分区 → 落收件箱"
    zdir, _rh, _dg = resolve_zone_dir(zone)
    if diverted:
        rhythm = bc.RHYTHM_INBOX
    zdir = bc.INBOX if diverted else zdir

    # 2. 重名检测（同分区内 norm 相同 → rc=3）
    if type_ == "待办" and not force:
        try:
            dup = duplicate_check(zone, content)
        except UndecodableError as e:
            print(f"✗ 重复检测受阻（分区文件无法解码）：{e}")
            write_safe.defer_write(zone or "00", content, "分区文件无法解码（重复检测阶段）")
            return 6
        if dup:
            print(f"⚠ 疑似重复：分区内已有同标题待办「{dup}」")
            print("  （确认要重复添加，加 --force）")
            return 3

    # 3~5. 铸号 + 查重 + 建文件 + 写 —— 全部收进锁内
    # ★ 修复 F1（外审 2026-09-28）：原实现 mint_tid 在 BoxLock **之前**执行，
    #   而 todo_id 的 load→mint→save 自身无锁——两个 box_add 并发时会各自
    #   load→mint→save，铸出同一个 tid：轻则误报重名（rc=3、账落待写清单），
    #   重则两个文件共用一个 tid，对账主键报废。修法：铸号、tid 查重、
    #   目标文件冲突检查、写文件全部挪进锁内串行化（锁本来就是干这个的）。
    day = date_str or time.strftime("%Y%m%d")
    hb = False
    try:
        with BoxLock("box_add") as lk:
            # 3. tid 铸造（冻结 tid 只由 migrate_v2 沿用；这里是新账 → 新铸）
            try:
                tid = mint_tid(content, zone if not diverted else None)
            except Exception as e:  # noqa: BLE001
                print(f"✗ tid 铸造失败：{e}")
                write_safe.defer_write(zone or "00", content,
                                       f"tid 铸造失败: {str(e)[:60]}")
                return 5

            # tid 唯一性（文件协议硬要求：一个 tid 只能有一个文件）
            clash = [p for p in existing_tid_files().get(tid, [])]
            if clash:
                print(f"⚠ tid {tid} 已被占用（{os.path.basename(clash[0])}）——"
                      "同一笔账不建第二个文件，按重名处理")
                return 3

            # 4. 造文件名 + 文件体
            fn = bc.build_filename("OPEN" if type_ != "完成" else "DONE", day, tid,
                                   rhythm, content)
            target = os.path.join(zdir, fn)
            if os.path.exists(target):                 # 极端并发：同名已存在
                print(f"⚠ 目标文件已存在：{fn}")
                return 3
            body = build_body(tid, content, rhythm)

            # 5. 写（write_safe 原子写 + 回读）
            ok = write_safe.safe_write(target, body, backup=False, encoding="utf-8")
            if not ok:
                raise write_safe.WriteSafeError(
                    f"safe_write 返回 False（未抛异常）——拒绝谎报成功: {target}")
            hb = lk.heartbeat_stalled
    except LockBusy as e:
        print(f"✗ {e}")
        write_safe.defer_write(zone or "00", content, "抢锁失败")
        return 4
    except (write_safe.WriteSafeError, UndecodableError) as e:
        print(f"✗ {e}")
        write_safe.defer_write(zone or "00", content, str(e)[:80])
        return 5

    # 6. 报告
    tag = "（转收件箱：" + reason + "）" if diverted else ""
    print(f"✓ 已建账文件{tag}")
    print(f"  文件: {target}")
    print(f"  五段: 状态=OPEN 日期={day} tid={tid} 节奏={rhythm} 标题={bc.clean_title(content)}")
    if hb:
        print("  ⚠ 锁心跳曾停摆（已记录）")
    # 2026-09-18 看板即时刷新（用户报告：白天动账看板不动，要等次日05:35）：
    # 记账成功后顺手重生成看板——账一动，板就新。失败不阻塞记账（看板是派生视图）。
    # ★ 2026-09-28 修（用户报告「看板还是死的」）：原实现只跑 _make_dashboard，
    #   但看板只读索引（todo_box_index.json）——新账不进索引，重生成也白跑。
    #   正确顺序：先扫描重建索引，再生成看板。两步都失败不阻塞记账。
    try:
        import subprocess, sys as _sys
        _here = os.path.dirname(os.path.abspath(__file__))
        # ① 扫描重建索引（box_add 自身不持锁，走正常加锁路径）
        subprocess.run([_sys.executable, os.path.join(_here, "todo_box_scan.py"), "--auto"],
                       capture_output=True, timeout=120)
        # ② 索引新了，看板才有新东西可显示
        subprocess.run([_sys.executable, os.path.join(_here, "_make_dashboard.py")],
                       capture_output=True, timeout=60)
    except Exception:
        pass
    return 0



# ══════════════════════════════════════════════════════
# 自检（供 box_selftest 调用）—— v2：写=建文件
# ══════════════════════════════════════════════════════
def selftest() -> int:
    """单2 断言：3 个测试待办 → 3 文件；重名 rc=3；临时注册表；清理干净。

    ⚠ 全程 BOX_TEST_REGISTRY + 新家骨架，绝不碰生产注册表。
    """
    import hashlib
    import json as _json
    import shutil
    fails = []
    print("=== box_add 自检（v2 写=建文件）===")

    # 0. 生产注册表哈希基线（断言3：测试全程不动它）
    #    两个都要盯：① 现役注册表（真数据，污染=灾难）
    #                ② 新家 _registry（单4 才落位；若已存在也不许动）
    import todo_id as _tid_mod
    prods = [p for p in (_tid_mod.REG, bc.REGISTRY_JSON)]
    prod_h0 = {p: (hashlib.sha256(open(p, "rb").read()).hexdigest()
                   if os.path.exists(p) else "(不存在)") for p in prods}

    # 1. 骨架就位（单3 才正式建；这里造一份测试用，事后不留痕）
    created_zone = not os.path.isdir(bc.ZONES["01"])
    tmpreg = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"),
                          f"_boxadd_selftest_reg_{os.getpid()}.json")
    os.environ["BOX_TEST_REGISTRY"] = tmpreg
    if os.path.exists(tmpreg):
        os.remove(tmpreg)
    made_dirs = []
    for d in list(bc.ZONES.values()) + [bc.INBOX, bc.REGISTRY_DIR, bc.DONE_DIR,
                                        os.path.join(bc.BOX, "_snapshots")]:
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            made_dirs.append(d)

    produced = []        # 本次自检产出的文件（断言4：事后清理）
    try:
        # ── 断言1：造 3 个测试待办（正常 / 含 `_` / 含非法字符）──
        cases = [
            (f"B2自检正常标题_{os.getpid()}", "04", "现在就做"),
            (f"B2自检含_下划线_标题_{os.getpid()}", "04", "盯办"),
            (f'B2自检非法:字符*测试?_{os.getpid()}', "04", "长线"),
        ]
        tids = []
        for i, (txt, zone, rhythm) in enumerate(cases):
            rc = add(txt, zone, "待办", rhythm, None, False)
            if rc != 0:
                fails.append(f"建文件 rc={rc}（{txt[:20]}）")
            # 定位刚建的产物
            for fn in os.listdir(bc.ZONES[zone]):
                if fn.endswith(".txt") and f"T" in fn:
                    pr = bc.parse_filename(fn)
                    if pr and txt[:10] in fn.replace("：", ":").replace("＜", "<") \
                       or (pr and bc.clean_title(txt) == pr["title"]):
                        produced.append(os.path.join(bc.ZONES[zone], fn))
        produced = sorted(set(produced))
        ok1 = len(produced) == 3 and not any("rc=" in f for f in fails)
        print(f"  [{'OK' if ok1 else 'FAIL'}] 断言1 三个测试待办 → 3 个文件（{len(produced)}）")
        if not ok1:
            fails.append("断言1")

        # 五段可解析 + 首行 tid 与文件名 tid 一致
        seg_ok, tid_ok = True, True
        for p in produced:
            pr = bc.parse_filename(os.path.basename(p))
            if not pr:
                seg_ok = False
                continue
            body = _read_body_file(p)
            first = body.splitlines()[0].strip() if body.strip() else ""
            if pr["tid"] not in first or not first.startswith("tid: "):
                tid_ok = False
        print(f"  [{'OK' if seg_ok else 'FAIL'}] 断言1a 文件名五段可解析")
        print(f"  [{'OK' if tid_ok else 'FAIL'}] 断言1b 首行 tid 标记 == 文件名 tid")
        if not seg_ok:
            fails.append("断言1a")
        if not tid_ok:
            fails.append("断言1b")

        # ── 断言2：重复添加同标题 → rc=3 ──
        dup_txt = cases[0][0]
        rc_dup = add(dup_txt, "04", "待办", "现在就做", None, False)
        # 换一个分区（收件箱）的同标题也应拦（同分区内判据）
        ok2 = rc_dup == 3
        print(f"  [{'OK' if ok2 else 'FAIL'}] 断言2 同标题重复 → rc=3（实得 {rc_dup}）")
        if not ok2:
            fails.append("断言2")

        # ── 断言3：生产注册表哈希前后不变 ──
        prod_h1 = {p: (hashlib.sha256(open(p, "rb").read()).hexdigest()
                       if os.path.exists(p) else "(不存在)") for p in prods}
        ok3 = prod_h0 == prod_h1
        det3 = "；".join(f"{os.path.basename(p)}={v[:10]}…" if v != "(不存在)"
                        else f"{os.path.basename(p)}=不存在" for p, v in prod_h0.items())
        print(f"  [{'OK' if ok3 else 'FAIL'}] 断言3 生产注册表哈希未变（{det3}）")
        if not ok3:
            fails.append("断言3")

        # ── 断言1c：非法字符标题确实被清洗 ──
        bad_fn = [os.path.basename(p) for p in produced
                  if "：" in os.path.basename(p) or "＜" in os.path.basename(p)]
        ok4 = len(bad_fn) >= 1 and all(ch not in "".join(bad_fn) for ch in '<>:"/\\|?*')
        print(f"  [{'OK' if ok4 else 'FAIL'}] 断言1c 非法字符已全角清洗（{bad_fn[:1]}）")
        if not ok4:
            fails.append("断言1c")

        # ── 断言1d：含 `_` 标题五段解析后标题完整 ──
        u_fn = [p for p in produced if "下划线" in os.path.basename(p)]
        pr_u = bc.parse_filename(os.path.basename(u_fn[0])) if u_fn else None
        ok5 = bool(pr_u) and "_" in pr_u["title"] and pr_u["title"].endswith(str(os.getpid()))
        print(f"  [{'OK' if ok5 else 'FAIL'}] 断言1d 含下划线标题完整保留（{pr_u['title'] if pr_u else 'NA'}）")
        if not ok5:
            fails.append("断言1d")

        # ── 断言：收件箱降级（未知分区 → 00_收件箱 + 节奏 待整理）──
        rc_in = add(f"B2自检降级_{os.getpid()}", None, "待办", "现在就做", None, False)
        inbox_new = [f for f in os.listdir(bc.INBOX)
                     if f.endswith(".txt") and str(os.getpid()) in f]
        pr_i = bc.parse_filename(inbox_new[0]) if inbox_new else None
        ok6 = rc_in == 0 and bool(pr_i) and pr_i["rhythm"] == bc.RHYTHM_INBOX
        print(f"  [{'OK' if ok6 else 'FAIL'}] 断言 未指定分区 → 收件箱 + 节奏=待整理"
              f"（{pr_i['rhythm'] if pr_i else 'NA'}）")
        if not ok6:
            fails.append("收件箱降级")
        produced += [os.path.join(bc.INBOX, f) for f in inbox_new]

    finally:
        # ── 断言4：测试产物清理干净（新家测试文件删除）──
        leftovers = []
        for p in produced:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except Exception:
                leftovers.append(p)
        os.environ.pop("BOX_TEST_REGISTRY", None)
        try:
            os.remove(tmpreg)
        except Exception:
            pass
        ok7 = not leftovers and not [
            f for f in os.listdir(bc.ZONES["04"]) if str(os.getpid()) in f]
        print(f"  [{'OK' if ok7 else 'FAIL'}] 断言4 测试产物已清理（残留 {len(leftovers)}）")
        if not ok7:
            fails.append("断言4")
        # 骨架是本单自检临时造的 → 单3 才正式建，这里若原是空的就撤掉目录（不留痕）
        if created_zone:
            for d in reversed(made_dirs):
                try:
                    os.rmdir(d)
                except Exception:
                    pass

    if fails:
        print(f"\n✗ 失败项: {fails}")
        return 1
    print("✅ box_add 自检通过（v2 写=建文件）")
    return 0



# ══════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description="待办箱写入器（唯一推荐写入入口）")
    ap.add_argument("content", nargs="?", help="条目内容（含特殊字符时改用 --stdin）")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读内容（防 shell 转义）")
    ap.add_argument("--zone", help="分区: 01-05（省略则落收件箱）")
    ap.add_argument("--type", default="待办", choices=("待办", "完成", "事件", "笔记"))
    ap.add_argument("--cls", help=f"节奏（枚举: {'/'.join(bc.RHYTHMS)}）")
    ap.add_argument("--date", dest="date_str", help="文件名日期 YYYYMMDD（一般不用）")
    ap.add_argument("--force", action="store_true", help="跳过重复检测")
    ap.add_argument("--selftest", action="store_true", help="运行自检")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    content = sys.stdin.read() if args.stdin else (args.content or "")
    if not content.strip():
        print("✗ 内容为空。用法: box_add.py \"内容\" [--zone 04] [--type 待办] [--cls 现在就做]")
        return 2
    return add(content.strip(), args.zone, args.type, args.cls, args.date_str, args.force)


if __name__ == "__main__":
    sys.exit(main())
