#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Half-Life 2 · 捡枪不自动切换 补丁工具 v1.2
==========================================

把《半条命 2》改成：
  * 手上已经有枪时，捡起一把自己没有的枪 —— 枪进背包，但手上的枪不切走；
  * 手上没枪时（剧情清空武器后等）—— 照原版直接拿起来，不会空手卡关。

v1.2 的核心：
  正式版捡枪会走两条切枪路径：
    1) CBasePlayer::Weapon_Equip() 里 g_pGameRules->FShouldSwitchWeapon()
       （按 autoswitchto / weight 判断）
    2) CBasePlayer::BumpWeapon() 末尾的 Weapon_Switch()
  本工具把两处都改成“手上有枪就不切、手无寸铁才装”。

特点
----
* 支持新旧两个游戏目录：
      <游戏目录>\\hl2_complete\\bin\\server.dll   （20 周年版本）
      <游戏目录>\\hl2\\bin\\server.dll            （旧版目录）
* 不写死版本：找不到已知哈希时，会用特征扫描自动定位两处补丁点、
  GetActiveWeapon 和代码洞，所以游戏更新后仍然能打。
* 自动备份、可还原、可只检查不写入。

用法
----
    python 补丁工具.py                 # 自动找 Steam 游戏目录并打补丁（会询问）
    python 补丁工具.py --check         # 只检查状态，不写文件
    python 补丁工具.py --restore       # 还原
    python 补丁工具.py --dry-run       # 只显示将要做什么
    python 补丁工具.py --yes           # 不询问
    python 补丁工具.py --game-dir "D:\\SteamLibrary\\steamapps\\common\\Half-Life 2"
    python 补丁工具.py --dll "D:\\...\\hl2_complete\\bin\\server.dll"
    python 补丁工具.py --debug         # 显示定位细节

依赖
----
* 只用 Python 标准库即可（PE 解析、特征扫描、字节级寄存器推断）。
* capstone 是可选的：安装后会额外用反汇编复核写入的桩。
      python -m pip install capstone

原理
----
补丁 A（BumpWeapon）：
    把 call PlayerHasMegaPhysCannon 换成
    “GetActiveWeapon() != NULL”
    -> 有枪：跳过补弹匣 + Weapon_Switch
    -> 没枪：照原版

补丁 B（Weapon_Equip）：
    把 g_pGameRules->FShouldSwitchWeapon(player, pWeapon) 调用换成
    “GetActiveWeapon() == NULL”
    -> 有枪：FShouldSwitchWeapon 返回 false，Weapon_Equip 不切
    -> 没枪：返回 true，Weapon_Equip 正常装备

桩 A 保存/恢复 ecx、edx；桩 B 只改 eax/ecx/edx（调用方只关心 AL）。
文件大小和其余内容不变。
"""

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

try:
    from capstone import Cs, CS_ARCH_X86, CS_MODE_32
    from capstone.x86 import X86_OP_REG, X86_OP_MEM, X86_REG_ECX
    HAVE_CAPSTONE = True
except Exception:                                    # pragma: no cover
    HAVE_CAPSTONE = False


TOOL_VERSION = "1.2"

# --------------------------------------------------------------------------
# 已知构建（当前 Steam 上能见到的两个 HL2 单机 server.dll）
# --------------------------------------------------------------------------
WEQUIP_OLD_COMMON = bytes.fromhex("8B80D4000000FFD0")   # mov eax,[eax+0xD4]; call eax

KNOWN_BUILDS = {
    # hl2_complete / 20 周年版本
    "7ce0bb2567bdb6d06306cdc0b5c2d31ba176759727d5d0c9a0ca8c6829cf9b3e": {
        "label": "HL2 (hl2_complete, 20 周年)",
        "site": 0x101FC81E,
        "func": 0x101FC6D0,
        "mega": 0x1041BE50,
        "this_reg": "ebx",
        "getactive": 0x100F8050,
        "active_off": 0x810,
        "cave": 0x10243E25,
        "wequip_site": 0x1020A83A,
        "wequip_cave": 0x101F2B78,
        "wequip_old": WEQUIP_OLD_COMMON,
    },
    # hl2 / 旧版目录
    "99f01211453b2c4a773ee81d73622b8b27d38b54483a4ae5ee48d33a38ec986b": {
        "label": "HL2 (hl2, 旧版目录)",
        "site": 0x101FC28E,
        "func": 0x101FC140,
        "mega": 0x1041BC50,
        "this_reg": "ebx",
        "getactive": 0x100F78E0,
        "active_off": 0x810,
        "cave": 0x101E1CCD,
        "wequip_site": 0x1020A32A,
        "wequip_cave": 0x1020FE39,
        "wequip_old": WEQUIP_OLD_COMMON,
    },
}

# v1.2 完整补丁后的文件哈希 -> 构建 key
PATCHED_HASHES = {
    "799cdede1bd6aa0e324287c1cbfece40ba887301dcda46f00c27a250a46bc066":
        "7ce0bb2567bdb6d06306cdc0b5c2d31ba176759727d5d0c9a0ca8c6829cf9b3e",
    "589af2c63c7f3a8c62ecd75b718371a7fe67e33214487bf89d6e157134002e9c":
        "99f01211453b2c4a773ee81d73622b8b27d38b54483a4ae5ee48d33a38ec986b",
}

REG_NAME = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"]
REG_ID = {n: i for i, n in enumerate(REG_NAME)}

MEGA_PAT = re.compile(
    rb"\x8b\x0d....\x33\xc0\x80\x79.\x01\x0f\x94\xc0\xc3", re.DOTALL)

GETACT_PAT = re.compile(
    rb"\x8b\x91....\x83\xfa\xff\x74.\xa1....\x8b\xca"
    rb"\x81\xe1\xff\x0f\x00\x00\xc1\xea\x0c\x03\xc9"
    rb"\x39\x54\xc8\x08\x75.\x8b\x44\xc8\x04\xc3", re.DOTALL)

CALL_JNE_PAT = re.compile(rb"\xE8....\x84\xC0\x75.", re.DOTALL)

BACKUP_SUFFIX = ".noautoswitch.bak"


class PatchError(Exception):
    pass


# --------------------------------------------------------------------------
# 轻量 PE32 解析
# --------------------------------------------------------------------------
class PEImage:
    def __init__(self, data, path=""):
        self.data = data
        self.path = path
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise PatchError("不是有效的 EXE/DLL 文件")
        e = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e:e + 4] != b"PE\0\0":
            raise PatchError("不是有效的 PE 文件")
        nsec = struct.unpack_from("<H", data, e + 6)[0]
        opt_size = struct.unpack_from("<H", data, e + 20)[0]
        opt = e + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        if magic != 0x10B:
            raise PatchError("不是 32 位 PE（HL2 的 server.dll 应为 PE32/x86）")
        self.image_base = struct.unpack_from("<I", data, opt + 28)[0]
        self.sections = []
        sh = opt + opt_size
        for i in range(nsec):
            o = sh + 40 * i
            name = data[o:o + 8].rstrip(b"\0").decode("latin1", "replace")
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
            self.sections.append((name, vaddr, vsize, rawptr, rawsize))

        text = next((s for s in self.sections if s[0] == ".text"), None)
        if text is None:
            raise PatchError("找不到 .text 段")
        _, tva, tvsize, traw, _ = text
        self.text_va = self.image_base + tva
        self.text_size = tvsize
        self.text_end = self.text_va + tvsize
        self.text_off = traw
        self.text = data[traw:traw + tvsize]

        self.callers = {}
        i = self.text.find(b"\xE8")
        while i != -1:
            rel = struct.unpack_from("<i", self.text, i + 1)[0]
            tgt = self.text_va + i + 5 + rel
            self.callers.setdefault(tgt, []).append(self.text_va + i)
            i = self.text.find(b"\xE8", i + 1)

    def va_to_off(self, va):
        rva = va - self.image_base
        for _n, vaddr, vsize, rawptr, _rawsize in self.sections:
            if vaddr <= rva < vaddr + vsize:
                return rawptr + (rva - vaddr)
        return None

    def off_to_va(self, off):
        for _n, vaddr, vsize, rawptr, rawsize in self.sections:
            if rawptr <= off < rawptr + rawsize:
                return self.image_base + vaddr + (off - rawptr)
        return None

    def read(self, va, n):
        o = self.va_to_off(va)
        if o is None:
            return b""
        return self.data[o:o + n]

    def in_text(self, va):
        return self.text_va <= va < self.text_end


# --------------------------------------------------------------------------
# 定位逻辑
# --------------------------------------------------------------------------
def _call_target(img, site):
    o = img.va_to_off(site)
    rel = struct.unpack_from("<i", img.data, o + 1)[0]
    return site + 5 + rel


def _func_start(img, va, limit=0x3000):
    off = img.va_to_off(va)
    if off is None:
        return None
    for i in range(off, max(0, off - limit), -1):
        if img.data[i:i + 3] == b"\x55\x8b\xec":
            prev = img.data[i - 1] if i else 0
            if prev in (0xCC, 0xC3, 0x90) or i == img.text_off:
                return img.off_to_va(i)
    return None


def _disasm(img, va, n):
    if not HAVE_CAPSTONE:
        return None
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = True
    return list(md.disasm(img.read(va, n), va))


def _this_reg_bytes(img, func, site):
    data = img.read(func, min(0x60, site - func))
    for i in range(len(data) - 1):
        if data[i] == 0x8B:
            modrm = data[i + 1]
            if (modrm & 0xC7) == 0xC1:          # mov r32, ecx
                dst = (modrm >> 3) & 7
                if dst != 1:
                    return REG_NAME[dst]
    return None


def _uses_as_this_bytes(img, func, site, reg):
    rid = REG_ID.get(reg)
    if rid is None:
        return False
    data = img.read(func, site - func)
    for i in range(len(data) - 1):
        if data[i] == 0x8B:
            modrm = data[i + 1]
            if (modrm & 0xC0) == 0x00 and (modrm & 7) == rid:
                return True
    return False


def _this_reg(img, func, site):
    insns = _disasm(img, func, site - func)
    if not insns:
        return None
    for insn in insns:
        if insn.mnemonic == "mov" and len(insn.operands) == 2:
            dst, src = insn.operands
            if (dst.type == X86_OP_REG and src.type == X86_OP_REG
                    and src.reg == X86_REG_ECX and dst.reg != X86_REG_ECX):
                return insn.reg_name(dst.reg)
    return None


def _uses_as_this(img, func, site, reg):
    insns = _disasm(img, func, site - func)
    if not insns:
        return False
    for insn in insns:
        if insn.mnemonic == "mov" and len(insn.operands) == 2:
            dst, src = insn.operands
            if (dst.type == X86_OP_REG and src.type == X86_OP_MEM
                    and src.mem.base != 0 and src.mem.disp == 0
                    and insn.reg_name(src.mem.base) == reg):
                return True
    return False


def _valid_bump_site(img, site):
    """验证 BumpWeapon 末尾的 `call X; test al,al; jne T` 块。"""
    b = img.read(site, 9)
    if len(b) < 9 or b[0] != 0xE8 or b[5:8] != b"\x84\xC0\x75":
        return None

    t = site + 9 + struct.unpack_from("<b", b, 8)[0]
    if not (site + 9 < t <= site + 0x100):
        return None

    block = img.read(site + 9, t - (site + 9))
    if len(block) != t - (site + 9):
        return None

    last = None
    j = 0
    while j <= len(block) - 2:
        if block[j] == 0xFF and 0x90 <= block[j + 1] <= 0x97:
            last = j
            j += 6
        elif block[j] == 0xFF and block[j + 1] in (0x50, 0x51, 0x52, 0x53,
                                                   0x55, 0x56, 0x57):
            last = j
            j += 2
        else:
            j += 1
    if last is None:
        return None

    pre = block[max(0, last - 12):last]
    if b"\x6a\x00" not in pre:
        return None
    if not any(0x50 <= x <= 0x57 for x in pre):
        return None

    ep = img.read(t, 20)
    if b"\xb0\x01" not in ep:
        return None
    if b"\xc2\x04\x00" not in ep and b"\xc3" not in ep:
        return None

    func = _func_start(img, site)
    if func is None or site - func < 0x80:
        return None

    whole = img.read(func, site - func + 0x30)
    if b"\x6a\x04" not in whole or b"\x6a\x20" not in whole:
        return None

    switch_disp = None
    cb = block[last:last + 6]
    if len(cb) >= 6 and cb[0] == 0xFF and 0x90 <= cb[1] <= 0x97:
        switch_disp = struct.unpack_from("<I", cb, 2)[0]

    return {
        "site": site,
        "func": func,
        "jne_target": t,
        "callee": _call_target(img, site),
        "switch_disp": switch_disp,
    }


def _find_bump_sites(img):
    found = {}
    for m in CALL_JNE_PAT.finditer(img.data):
        site = img.off_to_va(m.start())
        if site is None or not img.in_text(site):
            continue
        info = _valid_bump_site(img, site)
        if info:
            found[site] = info
    return list(found.values())


def _find_weapon_equip_site(img, switch_disp):
    """
    Weapon_Equip 里的：
        mov eax, [eax+0xD4]     ; FShouldSwitchWeapon 槽
        call eax
        test al, al
        jne  switch
    返回 `mov eax,[eax+disp]` 的地址（8 字节补丁点）。
    """
    t = img.text
    base = img.text_va
    regs = (0x90, 0x91, 0x92, 0x93, 0x95, 0x96, 0x97)
    for i in range(len(t) - 12):
        if t[i] != 0x8B or t[i + 1] not in (0x80, 0x81, 0x82, 0x83,
                                            0x85, 0x86, 0x87):
            continue
        if t[i + 6] != 0xFF or t[i + 7] not in (0xD0, 0xD1, 0xD2, 0xD3,
                                                0xD5, 0xD6, 0xD7):
            continue
        if t[i + 8:i + 10] != b"\x84\xC0" or t[i + 10] != 0x75:
            continue
        site = base + i
        tgt = site + 12 + struct.unpack_from("<b", t, i + 11)[0]
        tail = img.read(tgt, 0x20)
        if b"\x6a\x00" not in tail:
            continue
        if not any(0x50 <= x <= 0x57 for x in tail):
            continue
        if switch_disp is not None:
            found = False
            for mod in regs:
                if (b"\xff" + bytes((mod,)) +
                        struct.pack("<I", switch_disp)) in tail:
                    found = True
                    break
            if not found:
                continue
        else:
            if not any((b"\xff" + bytes((m,))) in tail for m in regs):
                continue
        return site
    return None


def _find_mega(img):
    return [img.off_to_va(m.start()) for m in MEGA_PAT.finditer(img.data)
            if img.off_to_va(m.start()) is not None]


def _find_get_active_weapon(img):
    cands = []
    for m in GETACT_PAT.finditer(img.data):
        va = img.off_to_va(m.start())
        if va is None or not img.in_text(va):
            continue
        off = struct.unpack_from("<I", img.data, m.start() + 2)[0]
        calls = len(img.callers.get(va, []))
        cands.append((calls, va, off))
    if not cands:
        raise PatchError("找不到 GetActiveWeapon 特征（游戏代码结构变化太大）")
    cands.sort(reverse=True)
    top = cands[0]
    second = cands[1][0] if len(cands) > 1 else 0
    if top[0] < 30:
        raise PatchError("GetActiveWeapon 调用点太少（%d），不敢确定" % top[0])
    if second and top[0] < second * 2:
        raise PatchError(
            "GetActiveWeapon 候选不唯一（%d vs %d），请用 --debug 查看"
            % (top[0], second))
    return top[1], top[2], cands[:5]


def _find_cave(img, need, near, avoid=None):
    need = max(need, 24)
    best = None
    i = 0
    n = len(img.text)
    while i < n:
        if img.text[i] == 0xCC:
            j = i
            while j < n and img.text[j] == 0xCC:
                j += 1
            length = j - i
            va = img.text_va + i
            if length >= need:
                overlap = (avoid is not None and
                           not (va + need <= avoid[0] or va >= avoid[1]))
                if not overlap:
                    dist = abs(va - near)
                    if best is None or dist < best[0]:
                        best = (dist, va, length)
            i = j
        else:
            i += 1
    if best is None:
        raise PatchError("找不到足够大的空闲代码洞（>= %d 字节 0xCC）" % need)
    return best[1], best[2]


def _build_stub(cave, getactive, this_reg):
    """BumpWeapon 用：返回 GetActiveWeapon() != NULL"""
    stub = bytearray(b"\x51\x52")
    if this_reg != "ecx":
        if this_reg not in REG_ID:
            raise PatchError("无法识别的 this 寄存器: %s" % this_reg)
        stub += bytes((0x8B, 0xC8 | REG_ID[this_reg]))
    call_va = cave + len(stub)
    rel = getactive - (call_va + 5)
    stub += b"\xE8" + struct.pack("<i", rel)
    stub += b"\x5A\x59\x85\xC0\x0F\x95\xC0\xC3"
    return bytes(stub)


def _build_wequip_stub(cave, getactive):
    """
    Weapon_Equip 用：替换 FShouldSwitchWeapon(player, weapon)，
    返回 GetActiveWeapon() == NULL。
    入口栈：[esp]=ret, [esp+4]=pPlayer, [esp+8]=pWeapon
    """
    stub = bytearray(b"\x8B\x4C\x24\x04")        # mov ecx,[esp+4] (pPlayer)
    call_va = cave + len(stub)
    rel = getactive - (call_va + 5)
    stub += b"\xE8" + struct.pack("<i", rel)     # call GetActiveWeapon
    stub += b"\x85\xC0"                          # test eax,eax
    stub += b"\x0F\x94\xC0"                      # sete al
    stub += b"\xC2\x08\x00"                      # ret 8
    return bytes(stub)


def make_plan(img, site, this_reg, getactive, active_off, cave,
              wequip_site, wequip_cave, trust=False):
    if not img.in_text(site) or not img.in_text(cave):
        raise PatchError("地址不在 .text 内")
    if not img.in_text(wequip_site) or not img.in_text(wequip_cave):
        raise PatchError("Weapon_Equip 地址不在 .text 内")
    func = _func_start(img, site)
    stub = _build_stub(cave, getactive, this_reg)
    old = img.read(site, 5)
    if len(old) != 5 or old[0] != 0xE8:
        raise PatchError("BumpWeapon 补丁点不是 call 指令")
    new = b"\xE8" + struct.pack("<i", cave - (site + 5))

    wequip_old = img.read(wequip_site, 8)
    if not trust:
        if (len(wequip_old) != 8 or wequip_old[0] != 0x8B or
                wequip_old[6] != 0xFF or wequip_old[7] not in
                (0xD0, 0xD1, 0xD2, 0xD3, 0xD5, 0xD6, 0xD7)):
            raise PatchError("Weapon_Equip 补丁点不是预期指令")
    wequip_stub = _build_wequip_stub(wequip_cave, getactive)
    wequip_new = (b"\xE8" +
                  struct.pack("<i", wequip_cave - (wequip_site + 5)) +
                  b"\x90\x90\x90")

    return {
        "site": site,
        "func": func,
        "this_reg": this_reg,
        "getactive": getactive,
        "active_off": active_off,
        "cave": cave,
        "cave_len": len(stub),
        "stub": stub,
        "site_old": old,
        "site_new": new,
        "mega": _call_target(img, site),
        "wequip_site": wequip_site,
        "wequip_cave": wequip_cave,
        "wequip_len": len(wequip_stub),
        "wequip_stub": wequip_stub,
        "wequip_old": wequip_old,
        "wequip_new": wequip_new,
    }


def analyze(img, debug=False):
    sha = hashlib.sha256(img.data).hexdigest()
    known_key = sha if sha in KNOWN_BUILDS else PATCHED_HASHES.get(sha)
    known = KNOWN_BUILDS.get(known_key) if known_key else None
    trust = sha in PATCHED_HASHES

    if known:
        plan = make_plan(img, known["site"], known["this_reg"],
                         known["getactive"], known["active_off"],
                         known["cave"], known["wequip_site"],
                         known["wequip_cave"], trust=trust)
        plan["label"] = known["label"]
        plan["known"] = True
        plan["build"] = known_key
        if HAVE_CAPSTONE and not trust:
            auto = _analyze_generic(img, debug=False)
            if (auto["site"] != plan["site"] or
                    auto["getactive"] != plan["getactive"] or
                    auto["this_reg"] != plan["this_reg"] or
                    auto["wequip_site"] != plan["wequip_site"]):
                raise PatchError(
                    "已知版本交叉校验失败：扫描结果与内置表不一致 "
                    "(site %08X/%08X, get %08X/%08X, reg %s/%s, weq %08X/%08X)" % (
                        auto["site"], plan["site"],
                        auto["getactive"], plan["getactive"],
                        auto["this_reg"], plan["this_reg"],
                        auto["wequip_site"], plan["wequip_site"]))
        return plan

    return _analyze_generic(img, debug=debug)


def _analyze_generic(img, debug=False):
    sites = _find_bump_sites(img)
    if not sites:
        raise PatchError("找不到 BumpWeapon 的切枪判断块（代码结构变化太大）")

    mega = set(_find_mega(img))
    if len(sites) > 1:
        preferred = [s for s in sites if s["callee"] in mega]
        if len(preferred) == 1:
            sites = preferred
        else:
            desc = ", ".join("%08X(func %08X)" % (s["site"], s["func"])
                             for s in sites)
            raise PatchError("找到多个可疑补丁点，无法确定: " + desc)
    site = sites[0]

    if HAVE_CAPSTONE:
        this_reg = _this_reg(img, site["func"], site["site"])
        if this_reg and not _uses_as_this(img, site["func"], site["site"], this_reg):
            this_reg = None
    else:
        this_reg = _this_reg_bytes(img, site["func"], site["site"])
        if this_reg and not _uses_as_this_bytes(img, site["func"], site["site"], this_reg):
            this_reg = None
    if not this_reg:
        raise PatchError("无法确定 BumpWeapon 的 this 寄存器")
    if this_reg in ("esp", "ebp"):
        raise PatchError("this 寄存器异常: %s" % this_reg)

    getactive, active_off, top5 = _find_get_active_weapon(img)
    if debug:
        print("  [debug] GetActiveWeapon 候选:")
        for calls, va, off in top5:
            print("      calls=%4d  %08X  +%#x" % (calls, va, off))

    wequip_site = _find_weapon_equip_site(img, site["switch_disp"])
    if wequip_site is None:
        raise PatchError("找不到 Weapon_Equip 的 FShouldSwitchWeapon 补丁点")
    if debug:
        print("  [debug] BumpWeapon site = %08X  switch slot = %s"
              % (site["site"], hex(site["switch_disp"]) if site["switch_disp"] else "?"))
        print("  [debug] Weapon_Equip site = %08X" % wequip_site)

    stub_len = 2 + (0 if this_reg == "ecx" else 2) + 5 + 8
    cave, cave_len = _find_cave(img, stub_len, site["site"])
    wequip_cave, wequip_cave_len = _find_cave(
        img, 17, wequip_site, avoid=(cave, cave + stub_len))

    plan = make_plan(img, site["site"], this_reg, getactive,
                     active_off, cave, wequip_site, wequip_cave)
    plan["label"] = "未知/新版本（特征定位）"
    plan["known"] = False
    plan["build"] = None
    return plan


# --------------------------------------------------------------------------
# 补丁 / 检查 / 还原
# --------------------------------------------------------------------------
def _parse_stub(img, va):
    b = img.read(va, 32)
    if len(b) < 15 or b[:2] != b"\x51\x52":
        return None
    i = 2
    if i < len(b) and b[i] == 0x8B:
        i += 2
    if i + 5 > len(b) or b[i] != 0xE8:
        return None
    i += 5
    tail = b"\x5A\x59\x85\xC0\x0F\x95\xC0\xC3"
    if b[i:i + len(tail)] != tail:
        return None
    return i + len(tail)


def _parse_wequip_stub(img, va):
    b = img.read(va, 32)
    if len(b) < 17 or b[:4] != b"\x8B\x4C\x24\x04":
        return None
    if b[4] != 0xE8:
        return None
    if b[9:17] != b"\x85\xC0\x0F\x94\xC0\xC2\x08\x00":
        return None
    return 17


def _site_a_patched(img, plan):
    if img.read(plan["site"], 1) != b"\xE8":
        return False
    return _parse_stub(img, _call_target(img, plan["site"])) is not None


def _site_b_patched(img, plan):
    if img.read(plan["wequip_site"], 1) != b"\xE8":
        return False
    return _parse_wequip_stub(img, _call_target(img, plan["wequip_site"])) is not None


def _find_stub_b(img):
    """在未知版本里找本工具的 Weapon_Equip 桩，并返回 (桩地址, 调用点)。"""
    pat = re.compile(
        rb"\x8b\x4c\x24\x04\xe8....\x85\xc0\x0f\x94\xc0\xc2\x08\x00",
        re.DOTALL)
    for m in pat.finditer(img.data):
        va = img.off_to_va(m.start())
        if va is None or not img.in_text(va):
            continue
        callers = img.callers.get(va, [])
        if len(callers) == 1:
            return va, callers[0]
    return None


def inspect(img, debug=False):
    """状态: stock / patched / partial / unknown"""
    try:
        plan = analyze(img, debug=debug)
    except PatchError:
        # 未知版本、但已经打过补丁：直接靠两段桩识别
        sites = _find_bump_sites(img)
        b = _find_stub_b(img)
        if len(sites) == 1 and b is not None:
            site = sites[0]["site"]
            if _parse_stub(img, _call_target(img, site)) is not None:
                plan = {
                    "site": site,
                    "wequip_site": b[1],
                    "cave": _call_target(img, site),
                    "wequip_cave": b[0],
                    "label": "未知/新版本（已打补丁）",
                    "known": False,
                    "build": None,
                }
                return "patched", plan
        raise

    a = _site_a_patched(img, plan)
    b = _site_b_patched(img, plan)
    if not a:
        if _call_target(img, plan["site"]) not in _find_mega(img):
            return "unknown", plan
    if not b:
        # 未打 B 时，补丁点必须是原始的 8 字节指令
        if img.read(plan["wequip_site"], 8) != plan["wequip_old"]:
            return "unknown", plan

    if a and b:
        return "patched", plan
    if not a and not b:
        return "stock", plan
    return "partial", plan


def apply_plan(img, plan):
    data = bytearray(img.data)
    so = img.va_to_off(plan["site"])
    co = img.va_to_off(plan["cave"])
    if data[so:so + 5] != plan["site_old"]:
        raise PatchError("BumpWeapon 补丁点内容已变化，拒绝写入")
    if bytes(data[co:co + len(plan["stub"])]) != b"\xCC" * len(plan["stub"]):
        raise PatchError("BumpWeapon 代码洞已被占用，拒绝写入")
    data[so:so + 5] = plan["site_new"]
    data[co:co + len(plan["stub"])] = plan["stub"]

    so = img.va_to_off(plan["wequip_site"])
    co = img.va_to_off(plan["wequip_cave"])
    if data[so:so + 8] != plan["wequip_old"]:
        raise PatchError("Weapon_Equip 补丁点内容已变化，拒绝写入")
    if bytes(data[co:co + len(plan["wequip_stub"])]) != b"\xCC" * len(plan["wequip_stub"]):
        raise PatchError("Weapon_Equip 代码洞已被占用，拒绝写入")
    data[so:so + 8] = plan["wequip_new"]
    data[co:co + len(plan["wequip_stub"])] = plan["wequip_stub"]
    return bytes(data)


def _verify_patched(data, plan):
    img = PEImage(data)
    if not _site_a_patched(img, plan):
        raise PatchError("写入后校验失败: BumpWeapon 桩")
    if not _site_b_patched(img, plan):
        raise PatchError("写入后校验失败: Weapon_Equip 桩")
    if HAVE_CAPSTONE:
        parts = []
        for label, site in (("A", plan["site"]), ("B", plan["wequip_site"])):
            ins = _disasm(img, site, 8)
            if not ins or ins[0].mnemonic != "call":
                raise PatchError("写入后校验失败: 补丁点 %s" % label)
            parts.append("%s: call %s" % (label, ins[0].op_str))
        return " ; ".join(parts)
    return ""


def _restore_structural(img, plan, state):
    """没有备份时按结构反向还原（仅完整 v1.2 补丁；未知版本 B 可能无法还原）。"""
    if state != "patched":
        raise PatchError("当前不是完整的 v1.2 补丁，无法按结构还原；"
                         "请用 .bak 备份或 Steam 验证文件完整性")
    data = bytearray(img.data)
    m = _find_mega(img)
    if len(m) != 1:
        raise PatchError("无法确定 BumpWeapon 原始 call 目标")
    mega = m[0]

    a = _site_a_patched(img, plan)
    if a:
        tgt = _call_target(img, plan["site"])
        stub_len = _parse_stub(img, tgt)
        if not stub_len:
            raise PatchError("没有发现 BumpWeapon 桩")
        ao = img.va_to_off(plan["site"])
        co = img.va_to_off(tgt)
        data[ao:ao + 5] = b"\xE8" + struct.pack("<i", mega - (plan["site"] + 5))
        data[co:co + stub_len] = b"\xCC" * stub_len

    b = _site_b_patched(img, plan)
    if b:
        key = _build_key_for_patched(img.data, plan)
        if key is None:
            raise PatchError("没有备份，且无法确定 Weapon_Equip 原始指令；"
                             "请用 Steam 验证文件完整性")
        old = KNOWN_BUILDS[key]["wequip_old"]
        tgt = _call_target(img, plan["wequip_site"])
        stub_len = _parse_wequip_stub(img, tgt)
        if not stub_len:
            raise PatchError("没有发现 Weapon_Equip 桩")
        bo = img.va_to_off(plan["wequip_site"])
        co = img.va_to_off(tgt)
        data[bo:bo + 8] = old
        data[co:co + stub_len] = b"\xCC" * stub_len

    return bytes(data)


def _build_key_for_patched(data, plan):
    """尝试把当前（已打补丁）文件对应到已知构建。"""
    sha = _sha(data)
    if sha in PATCHED_HASHES:
        return PATCHED_HASHES[sha]
    for key, info in KNOWN_BUILDS.items():
        if info["site"] == plan.get("site") and info["wequip_site"] == plan.get("wequip_site"):
            return key
    return None


def _write_atomic(path, data):
    folder = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".nas_", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _backup_path(dll):
    return dll + BACKUP_SUFFIX


def process_file(path, mode="apply", yes=False, dry=False, debug=False):
    if not os.path.isfile(path):
        return False, "文件不存在"
    data = open(path, "rb").read()
    sha = _sha(data)
    bak = _backup_path(path)

    try:
        img = PEImage(data, path)
    except PatchError as e:
        return False, "不是可处理的 server.dll：%s" % e

    print("  文件: %s" % path)
    print("  SHA256: %s" % sha)

    if mode in ("apply", "check") and sha in PATCHED_HASHES:
        print("  状态: 已打补丁 (v1.2)")
        return True, "already"

    if mode == "restore":
        state, plan = None, None
        try:
            state, plan = inspect(img, debug=debug)
        except PatchError:
            pass
        if state == "stock":
            print("  文件已是原版（未打补丁），无需还原")
            return True, "nothing"
        if os.path.isfile(bak):
            orig = open(bak, "rb").read()
            if not dry:
                try:
                    _write_atomic(path, orig)
                except OSError as e:
                    return False, "还原失败（游戏是否正在运行/文件只读？）：%s" % e
            print("  已从备份还原 (%s)" % _sha(orig))
            return True, "restored"
        if state != "patched":
            return False, ("当前不是完整的 v1.2 补丁，且没有 .bak 备份；"
                           "请用 Steam 验证文件完整性")
        try:
            orig = _restore_structural(img, plan, state)
        except PatchError as e:
            return False, "无法还原：%s" % e
        if not dry:
            try:
                _write_atomic(path, orig)
            except OSError as e:
                return False, "还原失败（游戏是否正在运行/文件只读？）：%s" % e
        print("  没有备份，已按结构反向还原 (%s)" % _sha(orig))
        return True, "restored"

    try:
        state, plan = inspect(img, debug=debug)
    except PatchError as e:
        return False, str(e)

    if state == "patched":
        print("  状态: 已打补丁 (v1.2)")
        return True, "already"
    if state == "unknown":
        return False, "文件已被其它补丁改过，拒绝处理"
    if state == "partial":
        return False, ("检测到不完整补丁（只写了一部分）。"
                       "请先 --restore 或 Steam 验证完整性，再重新打 v1.2。")

    print("  状态: 原版")
    print("  定位: %s" % plan["label"])
    print("        BumpWeapon      = %08X  判断点 = %08X" % (plan["func"], plan["site"]))
    print("        Weapon_Equip   判断点 = %08X" % plan["wequip_site"])
    print("        GetActiveWeapon = %08X  (m_hActiveWeapon = +%#x)"
          % (plan["getactive"], plan["active_off"]))
    print("        this 寄存器     = %s" % plan["this_reg"])
    print("        代码洞 A/B      = %08X / %08X" % (plan["cave"], plan["wequip_cave"]))

    new_data = apply_plan(img, plan)
    new_sha = _sha(new_data)
    print("  打补丁后 SHA256: %s" % new_sha)

    if mode == "check":
        return True, state

    if dry:
        print("  [dry-run] 不写入")
        return True, "dry"

    if not yes:
        try:
            ans = input("  写入补丁? [Y/n] ").strip().lower()
        except EOFError:
            ans = "y"
        if ans in ("n", "no"):
            print("  已取消")
            return False, "cancelled"

    try:
        shutil.copy2(path, bak)
        print("  已备份原版 -> %s" % bak)
    except OSError as e:
        return False, "无法创建备份（文件被占用/只读？）：%s" % e

    try:
        _write_atomic(path, new_data)
    except OSError as e:
        return False, "写入失败（游戏是否正在运行 / 文件只读？）：%s" % e

    check_data = open(path, "rb").read()
    if _sha(check_data) != new_sha:
        return False, "写入后哈希不一致"
    try:
        asm = _verify_patched(check_data, plan)
    except PatchError as e:
        return False, str(e)
    print("  已写入并校验通过。")
    if asm:
        print("  %s" % asm)
    return True, "patched"


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def find_steam_game():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam = winreg.QueryValueEx(key, "SteamPath")[0]
    except Exception:
        steam = None
    libs = []
    if steam:
        libs.append(steam)
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            try:
                txt = open(vdf, encoding="utf-8", errors="ignore").read()
                for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                    libs.append(m.group(1).replace("\\\\", "\\"))
                for m in re.finditer(r'"\d+"\s+"([A-Za-z]:[^"]+)"', txt):
                    libs.append(m.group(1).replace("\\\\", "\\"))
            except OSError:
                pass
    seen = set()
    fallback = None
    for lib in libs:
        lib = os.path.normpath(lib)
        if lib in seen:
            continue
        seen.add(lib)
        cand = os.path.join(lib, "steamapps", "common", "Half-Life 2")
        if not os.path.isdir(cand):
            continue
        if (os.path.isfile(os.path.join(cand, "hl2_complete", "bin", "server.dll"))
                or os.path.isfile(os.path.join(cand, "hl2", "bin", "server.dll"))):
            return cand
        if fallback is None:
            fallback = cand
    return fallback


def targets_for_game(game_dir):
    out = []
    for tag, rel in (("hl2_complete", os.path.join("hl2_complete", "bin", "server.dll")),
                     ("hl2", os.path.join("hl2", "bin", "server.dll"))):
        p = os.path.join(game_dir, rel)
        if os.path.isfile(p):
            out.append((tag, p))
    return out


def game_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq hl2.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
            errors="ignore").stdout.lower()
        return "hl2.exe" in out
    except Exception:
        return False


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Half-Life 2 捡枪不自动切换补丁工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python 补丁工具.py\n"
               "  python 补丁工具.py --check\n"
               "  python 补丁工具.py --restore\n"
               "  python 补丁工具.py --game-dir \"D:\\SteamLibrary\\steamapps\\common\\Half-Life 2\"\n")
    ap.add_argument("path", nargs="?", help="游戏目录（或直接给 server.dll）")
    ap.add_argument("--game-dir", help="游戏目录")
    ap.add_argument("--dll", action="append", default=[], help="只处理指定 server.dll（可重复）")
    ap.add_argument("--check", action="store_true", help="只检查状态")
    ap.add_argument("--restore", action="store_true", help="还原")
    ap.add_argument("--dry-run", action="store_true", help="不写文件")
    ap.add_argument("--yes", "-y", action="store_true", help="不询问")
    ap.add_argument("--debug", action="store_true", help="显示定位细节")
    ap.add_argument("--version", action="version", version="%(prog)s " + TOOL_VERSION)
    args = ap.parse_args()

    mode = "check" if args.check else ("restore" if args.restore else "apply")

    print("Half-Life 2 · 捡枪不自动切换 补丁工具 v%s" % TOOL_VERSION)
    if not HAVE_CAPSTONE:
        print("提示: 未安装 capstone（可选）。自动定位仍可用；")
        print("      安装后可多一层反汇编复核: python -m pip install capstone")

    targets = []
    if args.dll:
        for p in args.dll:
            targets.append((os.path.basename(os.path.dirname(os.path.dirname(p))) or "dll", p))
    else:
        path = args.game_dir or args.path
        if path and os.path.isfile(path):
            targets = [("dll", path)]
        else:
            game = path or find_steam_game()
            if not game:
                print("\n找不到游戏目录。请用 --game-dir 指定，例如：")
                print('  python 补丁工具.py --game-dir "D:\\SteamLibrary\\steamapps\\common\\Half-Life 2"')
                return 1
            print("游戏目录: %s" % game)
            targets = targets_for_game(game)
            if not targets:
                print("在游戏目录里找不到 hl2_complete\\bin\\server.dll 或 hl2\\bin\\server.dll")
                return 1

    if mode == "apply" and game_running():
        print("\n检测到 hl2.exe 正在运行，请先完全退出游戏再打补丁。")
        return 1

    print("")
    ok_all = True
    results = []
    for i, (tag, path) in enumerate(targets, 1):
        print("[%d/%d] %s" % (i, len(targets), tag))
        ok, res = process_file(path, mode=mode, yes=args.yes,
                               dry=args.dry_run, debug=args.debug)
        results.append((path, ok, res))
        if not ok:
            ok_all = False
            print("  失败: %s" % res)
        print("")

    if mode == "apply":
        patched = [p for p, ok, r in results if ok and r == "patched"]
        already = [p for p, ok, r in results if ok and r == "already"]
        if patched:
            print("完成：%d 个文件已打补丁 / 升级到 v1.2。" % len(patched))
        if already:
            print("%d 个文件本来就是 v1.2 补丁状态。" % len(already))
        if patched:
            print("\n进游戏测一下：拿着枪走过一把没有的枪，手上的枪应保持不变。")
            print("还原:  python 补丁工具.py --restore")
    elif mode == "check":
        for p, ok, r in results:
            if not ok:
                print("检查失败: %s" % p)
    elif mode == "restore":
        print("还原完成。")

    return 0 if ok_all else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(1)
    except PatchError as e:
        print("\n错误: %s" % e)
        sys.exit(1)
