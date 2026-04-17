# 2026-04-16 — 灵动岛 macOS 支持（P0 崩溃修复 + 原生方案）

## 目标

修复 macOS 上 `coco` 启动必崩的 bug（NSWindow 主线程约束），并为 macOS 提供一个**等价但原生风格**的通知/状态体验——用系统通知 + 终端标题 + 系统音替代 tkinter 浮动窗。

实现方式要和 Windows/Linux 的 tkinter 实现**并存不互相干扰**，后续想做 pyobjc 真悬浮岛时能无痛替换。

---

## 背景与约束

### 崩溃根因

`src/core/island.py:152-196` 起一个守护线程跑 `tk.Tk().mainloop()`。

- **Windows**：Tk 允许非主线程创建 window → 正常
- **Linux (no GUI)**：`_HAS_TK=False` 走 null 路径 → 正常
- **macOS**：`_HAS_TK=True` 但 Tk 底层走 Cocoa `NSWindow`，Cocoa **强制 NSWindow 必须在主线程 alloc**，否则 `NSInternalInconsistencyException` 直接 `abort()`

Traceback 证据：

```
thread_run → _tkinter_create → TkpInit → TkMacOSXMakeRealWindowExist → [NSWindow alloc]
... NSInternalInconsistencyException: 'NSWindow should only be instantiated on the main thread!'
```

这是 2026-04-13 commit `6155aca` 的回归 bug，作者在 Windows 开发，macOS 未覆盖。

### 为什么不走"把 Tk 搬主线程"

理论上把 CLI REPL 挪到后台线程、让 Tk 占主线程能统一跨平台。但代价极高：

- `prompt_toolkit` 的 `PromptSession` 期望主线程拥有终端
- `EscListener` / signal 处理依赖主线程
- rich 的 `Live` spinner 假设 stdout 单主控
- `main.py:entry()` 的整个控制流都要颠倒

这是一个 P0 修复，不是一次重构。

### 为什么不直接 null（彻底关掉）

可以修好崩溃，但 macOS 用户就失去了：

- agent 忙/闲状态的实时可视反馈
- 请求失败时的非终端通知
- 后台任务完成时的提示音

iPhone 灵动岛的**核心价值是不占前台也能被感知**，这个需求在 macOS 上存在且可以用原生方式满足。

### 约束

1. **零新依赖** —— 不引 pyobjc / rumps；`osascript` 和 `afplay` 是 macOS 系统自带
2. **公开 API 完全兼容** —— `DynamicIsland().start()/.set_working()/.notify()/.ask_permission()/.stop()` + `.available` 属性，所有调用点零改动
3. **平台独立** —— Windows/Linux 的 tkinter 代码一字不动
4. **回退链清晰** —— `COCO_NO_ISLAND=1` 关全部；其他路径按平台选
5. **权限确认在 macOS 上走终端** —— GUI 对话框不做（`input()` 已够用），通过 `ask_permission` 抛 `NotImplementedError` 让 `permissions.py:46` 的 `except` 自动回退

---

## 计划

### Step 1 — 抽 `_IslandBackend` 协议（结构化鸭子类型，不用正式 Protocol）

三个 backend 都要实现：`available: bool`（属性/字段）、`start()`、`set_working(working)`、`notify(title, body, *, error)`、`ask_permission(tool, inputs) -> str`、`stop()`。

### Step 2 — 现有 tkinter 代码 inline rename

`class DynamicIsland` → `class _TkIslandBackend`。代码一字不改（573 → ~570 行仅类名变化）。

### Step 3 — `_NullIslandBackend`

所有方法 no-op；`available=False`；`ask_permission` 抛 `NotImplementedError`。

### Step 4 — `_MacOSIslandBackend`（核心新增）

- **状态标题**：`set_working(True)` 写 `\033]0;Coco · working…\007`；`set_working(False)` 先写 ✓ done 再回 idle + 播 `Glass.aiff`；`stop()` 写空标题清理
- **通知**：`notify(title, body, *, error)` 组 AppleScript `display notification`，用 `_osa_escape` 转义反斜杠和双引号，`subprocess.run` 2 秒超时
- **错误声**：`error=True` 时额外 `afplay Basso.aiff`
- **权限**：`ask_permission` 抛 `NotImplementedError`

### Step 5 — `DynamicIsland` 薄 facade

`__init__` 调 `_choose_backend()` 选 backend；所有方法透传。

### Step 6 — `_choose_backend()` 分发

顺序：`COCO_NO_ISLAND` 白名单 → `darwin` → `_HAS_TK` → null。

### Step 7 — `/doctor` 诊断加一行

显示 backend 类名和 `available` 状态。

### Step 8 — 测试 + 手工冒烟

覆盖平台分发、转义、静默、OSError 兜底等。macOS 机上跑 `coco -p "hi"` 确认不再崩。

---

## 不做的事

- ❌ 不引 pyobjc / rumps / terminal-notifier 依赖
- ❌ 不做真正的 macOS 浮动岛 UI（要等 main.py 重构主线程所有权）
- ❌ 不改 `permissions.py`（现有 `try/except` 已够）
- ❌ 不改 Windows/Linux tkinter 代码路径
- ❌ 不做 iTerm2 特有 badge API（只用通用 OSC 0 终端标题）

---

## 验证

- 单元：`pytest tests/test_island_backend.py`
- 回归：`pytest tests/`
- 手工：macOS 机上跑 `coco -p "hi"`（无 key 也应走到"需要密钥"提示而不崩）
- Windows 不回归：测试用 monkeypatch 切平台验证 `_TkIslandBackend` 仍被选中

---

## Summary

按计划实施，零偏差。

**改动**：

- `src/core/island.py`（+~170 行）：
  - 现 `class DynamicIsland` 原地改名为 `class _TkIslandBackend`，573 行 tkinter 代码一字不改
  - 新增 `_NullIslandBackend`：`available=False`，所有方法 no-op；`ask_permission` 抛 `NotImplementedError`
  - 新增 `_MacOSIslandBackend`：`available=True`；`start/stop` 写 OSC 0 终端标题；`set_working` 切 idle/working/done（done 播 `Glass.aiff`）；`notify` 走 `osascript display notification`（2 秒超时 + 引号/反斜杠转义）；错误通知播 `Basso.aiff`；`ask_permission` 抛 `NotImplementedError`
  - `_osa_escape(s)` 私有 helper：`"` → `\"`，`\` → `\\`
  - `_choose_backend()`：env `COCO_NO_ISLAND` → `darwin` → `_HAS_TK` → null 四层决策
  - 新公开 `class DynamicIsland`（~30 行 facade）：委托给选定 backend；链式 `start()` 返回 self；`available` property 反映 backend 实际能力
- `src/core/commands.py`（+11 行）：`/doctor` 末尾新增第 9 项"灵动岛 backend"
- `src/core/permissions.py`：**零改动**（现有 `try/except Exception` 已经捕获 macOS backend 抛的 `NotImplementedError`，自动走终端 `input()`）
- `docs/sessions/2026-04-16-island-macos.md`（本文件）+ `docs/changelog.md`
- `tests/test_island_backend.py`（+19 tests）：
  - `_NullIslandBackend` 所有方法合约
  - `_choose_backend` 四路径（env 禁用 / darwin / linux+tk / linux 无 tk）+ env false 值不禁用 + Windows+tk 命中 Tk
  - `_MacOSIslandBackend.ask_permission` 抛 `NotImplementedError`
  - `notify` 调 osascript（形状 + 超时参数）
  - 引号/反斜杠转义验证（`_osa_escape` 独立 + `notify` 集成）
  - `error=True` 触发 `afplay Basso.aiff`
  - 未 `start()` 前 `notify` 静默（不调 subprocess）
  - `set_working` 写 OSC 0 到 stdout
  - stdout 写失败（OSError）被吞不崩
  - `stop` 清空标题（写 `\033]0;\007`）
  - osascript OSError 被吞
  - `DynamicIsland` facade 委托 + 链式 start + available 反映 backend

**测试**：`pytest tests/ -v` → **163 passed**（原 144 + 新增 19）。

**手工冒烟**（macOS 实测）：

```
$ unset ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY
$ coco -p "hi"
```

- **之前**：`NSInternalInconsistencyException: NSWindow should only be instantiated on the main thread` → abort
- **现在**：正常打印 banner + "API 密钥未配置" 提示 + 退出；终端标题变 "Coco · idle"

直接 Python 验证：

```
$ python -c "from core.island import DynamicIsland, _MacOSIslandBackend, _choose_backend; \
             b = _choose_backend(); \
             assert isinstance(b, _MacOSIslandBackend); \
             DynamicIsland().start().set_working(True)"
# 终端窗口标题变 "Coco · working…"
```

**关键不变量（给维护者）**：

- 三个 backend 都有 `available` 属性（bool）和 5 个方法；所有方法对"未 `start()`" 状态安全
- `_MacOSIslandBackend.ask_permission` 抛 `NotImplementedError` 是**契约**——`PermissionChecker._prompt`（`permissions.py:46`）的 `except Exception` 依赖它来回退终端；不要改成返回 `"n"`，否则所有非只读工具会被静默拒绝
- `DynamicIsland.start()` 必须返回 `self`（`main.py:243` 用 `island = DynamicIsland().start()` 链式）

**未做 / 后续**：

- 真·macOS 浮动岛（pyobjc + NSWindow）需 `main.py` 主线程让渡给 Tk/Cocoa，REPL 跑后台线程——大重构，不在本 PR 范围。届时 `_MacOSIslandBackend` 可被 `_MacOSNativeIslandBackend` 替换对上游透明
- Linux Wayland 下 tkinter 的 `_HAS_TK=True` 但可能无 display —— 现有 `_run()` 已有 `try/except` 兜底，本 PR 不碰
- `COCO_NO_ISLAND` 没在 README / `.env.example` 宣传 —— 后续补
