# 半条命 2 捡枪不自动切换

Cannot be achieved through the workshop's vpk; only modify server.dll

Implement the feature that when picking up a new weapon, the item in hand does not immediately switch to the new weapon.

**Note: Modifications will be invalid after verifying game integrity!**

**Plot-related gravity gun is still placed in the backpack but does not automatically equip; press the default **G** / `phys_swap` to pick it up manually**

Analyzed and provided the solution by Deepseek.

无法通过创意工坊的 vpk 实现，只能修改 server.dll

实现当捡起新武器时手中的物品不会立即切换到新武器。

**注意：验证游戏完整性后修改会失效！**

**剧情直接给的重力枪仍进背包但不自动拿在手上（按默认 **G** / `phys_swap` 拿一下）**

由 Deepseek 分析并给出方案。

## 使用

使用前确保安装了 python3.1x 版本及以上。

下载 `补丁工具.py` 双击后按提示操作。

## 高级用法

以下为示例，请根据实际情况修改：

```
python 补丁工具.py                 # 自动找 Steam 游戏目录并打补丁（会询问）
python 补丁工具.py --check         # 只检查状态，不写文件
python 补丁工具.py --restore       # 还原
python 补丁工具.py --dry-run       # 只显示将要做什么
python 补丁工具.py --yes           # 不询问
python 补丁工具.py --game-dir "D:\SteamLibrary\steamapps\common\Half-Life 2"   # 手动指定游戏目录
python 补丁工具.py --dll "D:\...\hl2_complete\bin\server.dll"  # 手动指定 dll 文件
python 补丁工具.py --debug         # 显示定位细节
```

# 技术细节

## 可选依赖

`capstone` 只用于“反汇编复核”和 `--debug` 的交叉验证。

不装 `capstone` 时只是少了“写入后用反汇编再确认一遍”的额外保险。

安装（可选）：

```bash
python -m pip install capstone
```

## 目标行为

- 手上有枪，捡起一把自己没有的枪：
  - 枪进入背包（弹药、弹匣、HUD、数字键切换都正常）；
  - **手上原来的枪不切走**。
- 手上没有枪（剧情 `player_weaponstrip` 后等）：
  - 照原版直接把新枪拿到手上，不会空手卡关。
- 同种枪补弹药、数字键换枪、存档/成就都不受影响。

## 两条切枪路径（问题的核心）

正式版 `CBasePlayer::BumpWeapon()` 捡到新枪时，先调用 `Weapon_Equip()`：

```asm
; CBasePlayer::BumpWeapon（hl2_complete，0x101FC6D0）
101FC7EF  mov eax, [ebx]          ; player vtable
101FC7F1  mov ecx, ebx
101FC7F3  push edi                ; pWeapon
101FC7F4  call dword ptr [eax+0x410]   ; Weapon_Equip(pWeapon)
```

`CBasePlayer::Weapon_Equip()`（`0x1020A820`）里第一次切枪判断：

```asm
1020A82B  call 0x100F6DA0          ; CBaseCombatCharacter::Weapon_Equip：把枪加进 m_hMyWeapons
1020A830  mov ecx, [0x106BE910]    ; g_pGameRules
1020A836  push ebx                 ; pWeapon
1020A837  push edi                 ; pPlayer
1020A838  mov eax, [ecx]
1020A83A  mov eax, [eax+0xD4]      ; FShouldSwitchWeapon
1020A840  call eax
1020A842  test al, al
1020A844  jne 0x1020A880           ; -> Weapon_Switch(pWeapon, 0)
```

`CSingleplayRules::FShouldSwitchWeapon()`（`0x1024ABB0`，通过 `CSingleplayRules` vtable`0x1052CCB0 + 0xD4` 找到）的判断：

```cpp
bool CSingleplayRules::FShouldSwitchWeapon( CBasePlayer *pPlayer, CBaseCombatWeapon *pWeapon )
{
    // 1. 新枪没弹药且玩家也没有对应备弹 -> 不切
    if ( ( pWeapon->HasAnyAmmo() == false ) &&
         ( pPlayer->GetAmmoCount( pWeapon->m_iPrimaryAmmoType ) <= 0 ) )
        return false;

    // 2. 玩家当前没有手持武器 -> 切
    if ( pPlayer->GetActiveWeapon() == NULL )
        return true;

    // 3. 新枪不允许 autoswitchto -> 不切
    if ( !pWeapon->AllowsAutoSwitchTo() )
        return false;

    // 4. 当前武器不允许 autoswitchfrom -> 不切
    if ( !pPlayer->GetActiveWeapon()->AllowsAutoSwitchFrom() )
        return false;

    // 5. 当前武器不能被 holster -> 不切
    if ( pPlayer->GetActiveWeapon()->CanHolster() == false )
        return false;

    // 6. 新枪 weight 不高于当前武器 -> 不切
    if ( ( pWeapon != pPlayer->GetActiveWeapon() ) &&
         ( pWeapon->GetWeight() <= pPlayer->GetActiveWeapon()->GetWeight() ) )
        return false;

    return true;
}
```

之后 `BumpWeapon` 末尾还有**第二条**无条件切枪：

```asm
101FC81E  call PlayerHasMegaPhysCannon
101FC823  test al, al
101FC825  jne 0x101FC860           ; 超级重力枪 -> 跳过
...
101FC853  mov eax, [ebx]
101FC855  mov ecx, ebx
101FC857  push 0
101FC859  push edi
101FC85A  call [eax+0x41C]         ; Weapon_Switch(pWeapon, 0)
101FC860  pop edi
101FC861  mov al, 1
101FC863  pop ebx
101FC864  pop ebp
101FC865  ret 4
```

所以只改 `BumpWeapon` 或只改 `Weapon_Equip` 都不够：

`Weapon_Equip` 里的 `FShouldSwitchWeapon` 会先按 weight/脚本判断切一次，`BumpWeapon` 末尾又会无条件切一次。

## 关键地址 / 结构

两个目标版本

| 项 | `hl2_complete`（20 周年） | `hl2`（旧版目录） |
|---|---|---|
| 原版 SHA-256 | `7CE0BB25…CF9B3E` | `99F01211…38EC986B` |
| `CHL2_Player` vtable | `0x1055B968` | `0x1055B928` |
| `BumpWeapon` 本体 | `0x101FC6D0` | `0x101FC140` |
| BumpWeapon 判断点（A） | `0x101FC81E` | `0x101FC28E` |
| `PlayerHasMegaPhysCannon` | `0x1041BE50` | `0x1041BC50` |
| 代码洞 A | `0x10243E25`（59 字节） | `0x101E1CCD`（67 字节） |
| `Weapon_Equip` 判断点（B） | `0x1020A83A` | `0x1020A32A` |
| 代码洞 B | `0x101F2B78`（40 字节） | `0x1020FE39`（39 字节） |
| `GetActiveWeapon` | `0x100F8050` | `0x100F78E0` |
| `m_hActiveWeapon` | `+0x810` | `+0x810` |
| `Weapon_Switch` vtable 槽 | `+0x41C` | `+0x41C` |
| `Weapon_Equip` vtable 槽 | `+0x410` | `+0x410` |
| `BumpWeapon` vtable 槽 | `+0x630`（396） | `+0x630`（396） |
| `FShouldSwitchWeapon` vtable 槽 | `+0xD4` | `+0xD4` |

## 用到的对象/结构

- `CBasePlayer` / `CHL2_Player`：`m_hActiveWeapon` 在 `+0x810`（4 字节 `CNetworkHandle`，空 = `0xFFFFFFFF`）。
- `FileWeaponInfo_t`（武器脚本解析结果）：
  - `bAutoSwitchTo` 在 `+0x180`（`autoswitchto`）
  - `bAutoSwitchFrom` 在 `+0x181`（`autoswitchfrom`）
  - `iFlags` 在 `+0x184`
- 武器 vtable（`CBaseCombatWeapon`，`0x104CC3B8`）：
  - 槽 315 = `AllowsAutoSwitchTo()`（函数 `0x100FC4C0`，读 `FileWeaponInfo +0x180`）
  - 槽 316 = `AllowsAutoSwitchFrom()`（函数 `0x100FC4A0`，读 `+0x181`）
- `CSingleplayRules` vtable `0x1052CCB0`，槽 `0xD4` = `FShouldSwitchWeapon`（`0x1024ABB0`）。
- `FShouldSwitchWeapon` 里用到的虚槽：
  - `HasAnyAmmo` `+0x3C4`
  - `GetAmmoCount` `+0x3F8`
  - `AllowsAutoSwitchTo` `+0x4EC`
  - `AllowsAutoSwitchFrom` `+0x4F0`
  - `CanHolster` `+0x3D0`
  - `GetWeight` `+0x4E8`

## 捡枪调用链

```
武器实体 Touch（或 +use）
  -> CBaseCombatWeapon::DefaultTouch / Use
     -> pPlayer->BumpWeapon(this)          [player vtable +0x630]
        -> Weapon_Equip(pWeapon)           [player vtable +0x410]
           -> CBaseCombatCharacter::Weapon_Equip  [0x100F6DA0]
              -> 把武器加入 m_hMyWeapons[0x30]
              -> pWeapon->Equip(this)
           -> g_pGameRules->FShouldSwitchWeapon(player, weapon)  [vtable +0xD4]
              -> 按 autoswitchto / autoswitchfrom / CanHolster / weight 判断
              -> 若 true：Weapon_Switch(pWeapon, 0)  [vtable +0x41C]
        -> IsInAVehicle / PlayerHasMegaPhysCannon ...
        -> 若 !mega：补满弹匣 + Weapon_Switch(pWeapon, 0)  [vtable +0x41C]
        -> return true
```

## GetActiveWeapon 与 m_hActiveWeapon

```asm
100F8050  8B 91 10 08 00 00   mov edx, [ecx+0x810]   ; m_hActiveWeapon
100F8056  83 FA FF            cmp edx, -1
100F8059  74 1D               je  返回 NULL
100F805B  A1 B4 D9 62 10      mov eax, [g_EntityList]
...
100F8073  8B 44 C8 04         mov eax, [eax+ecx*8+4]
100F8077  C3                  ret
```

## 为什么只补 BumpWeapon 不够

`Weapon_Equip` 里的 `FShouldSwitchWeapon` 才是**第一条**切枪路径：

- 例如手枪 weight 2、霰弹枪 weight 5：
  - `AllowsAutoSwitchTo()` / `AllowsAutoSwitchFrom()` / `CanHolster()` 都通过；
  - `shotgun weight 5 > pistol weight 2` → 返回 true；
  - `Weapon_Equip` 立刻 `Weapon_Switch(shotgun)`。
- 之后 `BumpWeapon` 才执行我们原来补的那段（补弹匣 + 再切一次）。
- 所以只跳过后面那段，前面已经切完了。

因此需要：

- **补丁 A**：`BumpWeapon` 的 `PlayerHasMegaPhysCannon` 判断 → 改成“手上有枪”；
- **补丁 B**：`Weapon_Equip` 的 `FShouldSwitchWeapon` 调用 → 改成“手无寸铁”。

## 补丁 A：BumpWeapon

原指令（5 字节）：

```asm
101FC81E  E8 2D F6 21 00   call 0x1041BE50   ; PlayerHasMegaPhysCannon
```

改成 `call 代码洞A`：

```asm
101FC81E  E8 xx xx xx xx   call 0x10243E25
```

代码洞 A（`0x10243E25`，17 字节）：

```asm
push ecx
push edx
mov  ecx, ebx               ; this = player（BumpWeapon 的 this 在 ebx）
call 0x100F8050             ; GetActiveWeapon()
pop  edx
pop  ecx
test eax, eax
setne al                    ; 有枪 -> 1，空手 -> 0
ret
```

流程：

```asm
101FC81E  call 桩A
101FC823  test al, al
101FC825  jne 0x101FC860    ; 有枪：跳过补弹匣 + Weapon_Switch
...
101FC85A  call [eax+0x41C]  ; 空手：原版 Weapon_Switch
```

`push/pop ecx, edx` 是为了对调用方透明：原 `PlayerHasMegaPhysCannon` 是普通函数调用，编译器允许破坏 eax/ecx/edx；新桩只改 eax（返回值），ecx/edx 原样恢复。

## 补丁 B：Weapon_Equip / FShouldSwitchWeapon

原指令（8 字节）：

```asm
1020A83A  8B 80 D4 00 00 00   mov eax, [eax+0xD4]   ; FShouldSwitchWeapon
1020A840  FF D0               call eax
```

改成 `call 代码洞B` + 3 个 NOP（仍是 8 字节）：

```asm
1020A83A  E8 xx xx xx xx   call 0x101F2B78
1020A83F  90 90 90         nop; nop; nop
```

代码洞 B（`0x101F2B78`，17 字节）：

```asm
mov  ecx, [esp+4]           ; pPlayer（thiscall + 2 个栈参数）
call 0x100F8050             ; GetActiveWeapon()
test eax, eax
sete al                     ; 空手 -> 1，有枪 -> 0
ret  8                      ; 清掉 pPlayer / pWeapon 两个栈参数
```

调用约定细节（重要）：

- 调用点先 `push pWeapon`、再 `push pPlayer`，然后 `call`；
- 进入桩时：
  - `[esp]` = 返回地址
  - `[esp+4]` = pPlayer
  - `[esp+8]` = pWeapon
- 原 `FShouldSwitchWeapon` 是 `__thiscall`，用 `ret 8` 清参数；
  桩 B 也必须 `ret 8`，否则调用方栈会错位。
- 桩 B 只改 eax/ecx/edx；调用方只关心 AL（bool），后面的代码会重新加载 ecx/edx。

流程：

```asm
1020A842  test al, al
1020A844  jne 0x1020A880    ; 空手 -> Weapon_Switch
...                          ; 有枪 -> 走后面的 physcannon 专用分支（普通拾取不会命中）
```

## 代码洞选择

- 在 `.text` 里找连续的 `0xCC`（int3 对齐填充）；
- 取长度 ≥ 24 字节、离补丁点最近的一段；
- 两个桩不允许重叠（`_find_cave(..., avoid=...)`）；
- 写入前确认目标字节仍是 `0xCC`。

当前两个版本：

| 版本 | 代码洞 A | 可用长度 | 代码洞 B | 可用长度 |
|---|---|---|---|---|
| `hl2_complete` | `0x10243E25` | 59 | `0x101F2B78` | 40 |
| `hl2` | `0x101E1CCD` | 67 | `0x1020FE39` | 39 |

每个桩 17 字节，充足。

## 补丁工具实现

| 部分 | 函数/类 | 说明 |
|---|---|---|
| PE 解析 | `PEImage` | DOS/PE 头、可选头、节表、VA↔offset、`.text` 的 `E8` 调用表 |
| 特征 | `MEGA_PAT`、`GETACT_PAT`、`CALL_JNE_PAT` | `PlayerHasMegaPhysCannon`、handle getter、`call;test;jne` |
| 定位 | `_valid_bump_site`、`_find_weapon_equip_site`、`_this_reg`、`_find_get_active_weapon`、`_find_cave`、`_find_mega`、`_find_stub_b` | 找两处补丁点、寄存器、代码洞、已打补丁的桩 |
| 计划 | `make_plan` | 生成两处补丁的原始/新字节、桩、代码洞 |
| 状态 | `inspect` | `stock / patched / partial / unknown` |
| 写入 | `apply_plan`、`_verify_patched` | 校验原字节/代码洞 → 写入 → 重新解析校验 |
| 备份/还原 | `_restore_structural`、`process_file` | `.bak` 优先；已知版本可结构还原 |
| 命令行 | `main` | 自动找 Steam 目录、`--dll`、`--check`、`--restore`、`--dry-run`、`--yes`、`--debug` |

## 定位逻辑

BumpWeapon（补丁 A）

1. 扫 `E8 rel32 84 C0 75 rel8`（`call X; test al,al; jne T`）；
2. 校验：
   - `T` 附近有 `mov al,1` + `ret`（返回 true）；
   - `T` 之前的块里有 `push 0` + `push <武器>` + `call [reg+disp]`（`Weapon_Switch`）；
   - 所在函数里有 `push 4`（`AddSolidFlags(FSOLID_NOT_SOLID)`）和
     `push 0x20`（`AddEffects(EF_NODRAW)`）；
   - 函数体 > 0x80 字节；
3. 记录 `Weapon_Switch` 的 vtable 位移（当前 `0x41C`），供补丁 B 校验。

Weapon_Equip（补丁 B）

1. 扫 `8B 8? <disp32> FF D? 84 C0 75`（`mov r32,[reg+disp]; call r32; test al,al; jne`）；
2. 校验 jne 目标在 ~0x20 字节内出现：
   - `push 0`
   - `push <寄存器>`
   - `call [reg+Weapon_Switch槽]`（槽值来自补丁 A 的动态提取）
3. 当前四个构建（`hl2_complete`、`hl2`、`episodic`、`lostcoast`）都只命中 1 个。

this 寄存器

- 有 capstone：从函数开头反汇编，找第一条 `mov r32, ecx`，再用
  `mov eax, [reg]` 验证它确实当对象指针用；
- 无 capstone：用字节编码 `8B C8..CF`（`mov r32, ecx`）和
  `8B 0?`（`mov r32, [reg]`）做同样的事。

GetActiveWeapon

- 扫 `8B 91 <disp32> 83 FA FF 74 ...` 的 handle-getter；
- 统计每个候选的**直接调用**次数（`.text` 里的 `E8`）；
- 取最大值，要求 ≥ 30 且 ≥ 第二名的 2 倍；
- 从匹配字节里读出 `m_hActiveWeapon` 偏移。

已打补丁的未知版本

- `_find_bump_sites` 找 A 的 `call;test;jne` 块；
- `_find_stub_b` 扫桩 B 的 17 字节特征
  `8B 4C 24 04 E8 .... 85 C0 0F 94 C0 C2 08 00`；
- 用 `PEImage.callers` 找唯一调用点；
- 两段桩都在 → `--check` 报“已打补丁”。

## 状态机

| 状态 | 条件 | 行为 |
|---|---|---|
| `stock` | A 的调用目标是 mega、B 还是原始 8 字节 | 打 A + B |
| `patched` | 两段桩都在 | 已打补丁，跳过 |
| `partial` | 只命中一段桩 | 拒绝，提示先 `--restore` 或 Steam 验证 |
| `unknown` | A 调用目标不是 mega / B 被其它补丁改过 | 拒绝，避免破坏其它 mod |

只命中一段桩的文件属于 `partial`：工具不会自动补另一段，避免把半成品当成正常状态；

请先 `--restore` 或 Steam 验证完整性，再重新打完整的补丁。

## 校验与安全

- 写入前：
  - 补丁点必须是原始字节（A 是 `E8`，B 是 `8B 8? … FF D?`）；
  - 代码洞必须是 `0xCC`；
- 写入后：
  - 重新解析 PE；
  - 检查两段桩和调用点；
  - 有 capstone 时反汇编确认 `call` 目标；
  - 核对 SHA-256；
- 原子写入：`tempfile` + `os.replace`；
- 检测 `hl2.exe` 是否在运行（`tasklist`），在运行则拒绝写；
- `.bak` 备份：每次对原版打补丁都会刷新 `.bak`；
- `--restore`：
  - 当前已是原版 → 提示无需还原；
  - 有 `.bak` → 用备份；
  - 已知版本无 `.bak` → 反汇编/字节反向还原（A 恢复 `call mega`，B 恢复原始 8 字节）；
  - 未知版本无 `.bak` → 提示 Steam 验证。

## 命令行

| 参数 | 作用 |
|---|---|
| *(无)* | 自动找 Steam 游戏目录，问是否写入 |
| `--game-dir DIR` | 指定游戏根目录 |
| `--dll FILE` | 只处理指定 DLL（可重复） |
| `--check` | 只检查，不写 |
| `--restore` | 还原 |
| `--dry-run` | 只预览 |
| `--yes` / `-y` | 不询问 |
| `--debug` | 打印定位细节（候选、槽等） |
| `--version` | 版本 |
| `-h/--help` | 帮助 |

退出码：0 成功；1 失败/拒绝/取消。

## 适配新的游戏版本

正常情况下直接跑工具即可：哈希不认识时会走特征扫描。

如果 Valve 以后大改，工具报“找不到 …”时，可以按下面步骤手动更新`KNOWN_BUILDS`（或先只用 `--debug` 看扫描结果）：

1. **找 `CHL2_Player` vtable**：RTTI 类型名 `.?AVCHL2_Player@@` → COL → vtable；
   槽 396（`0x630`）是 `BumpWeapon`（通常是 tail-jump thunk）。
2. **找 BumpWeapon 判断点**：`BumpWeapon` 里 `call PlayerHasMegaPhysCannon; test al,al; jne`
   的 `call` 地址；函数尾部有 `push 0; push pWeapon; call [vtbl+0x41C]`。
3. **找 Weapon_Equip 判断点**：`CHL2_Player` vtable 槽 `0x410`（`Weapon_Equip`）
   → `CBasePlayer::Weapon_Equip` 里 `mov eax,[g_pGameRules]; push pWeapon; push pPlayer;
   mov eax,[eax+0xD4]; call eax; test al,al; jne` 的 `mov eax,[eax+0xD4]` 地址。
4. **找 GetActiveWeapon**：handle-getter 里直接调用次数最多的那个；
   偏移通常还是 `+0x810`（用构造函数/析构函数的 `-1` 初始化确认）。
5. **找代码洞**：`.text` 里 ≥ 24 字节的 `0xCC` 填充，离判断点最近。
6. 把 `site / func / mega / this_reg / getactive / active_off / cave /
   wequip_site / wequip_cave / wequip_old` 填进 `KNOWN_BUILDS`。
7. 在**副本**上测试 apply/check/restore，确认无误再让游戏目录生效。

## 已知边界

- 改的是 `server.dll` 逻辑，不是 VPK 内容包。
- `Weapon_Equip` 的桩会作用于所有调用 `Weapon_Equip` 的路径：
  - 手上有枪时不会自动切到新装备的武器（本 mod 的目的）；
  - 手无寸铁时仍正常装备；
  - `use weapon_*` 命令在 `Weapon_Equip` 后还会显式 `Weapon_Switch`，不受影响。
- 测试请用“走到地上的枪 / 按 E”；控制台 `give` 是另一条装备路径，不是本 mod 的目标。
- 剧情直接给的重力枪仍进背包但不自动拿在手上（按默认 **G** / `phys_swap` 拿一下）。
- 未知版本 + 没有 `.bak` 时，结构还原可能无法恢复 B 的原始 8 字节（需要知道原 vtable 位移）；
  正常用工具打补丁会自动生成 `.bak`。
- Steam 验证/更新会覆盖 DLL；更新后重新运行工具。
- 与其它替换 `server.dll` 的 mod 不兼容。
- 仅针对 PE32/x86 的 Windows `server.dll`（`hl2_complete` / `hl2`）。