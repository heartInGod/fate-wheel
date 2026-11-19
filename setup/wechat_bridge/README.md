# 微信个人号桥接服务（Windows 侧）

fate-wheel 的定时任务跑在 WSL 里，无法直接操作 Windows 上的微信客户端；而个人号
机器人（wxautox / ntchat / wcferry 等）都绑定特定微信版本，写死某一个会让项目跟着
失效。所以这里把「协议」和「驱动」解耦：

```
WSL 定时任务 ──python3 main.py notify──> POST http://<Windows>:8766/send ──> sender ──> 微信
```

- **协议固定**：`POST /send`，JSON `{"text": "...", "token": "..."}`，返回 `{"ok": true/false}`。
- **驱动可插拔**：`bridge_config.json` 的 `sender.type` 决定怎么真正发出去。

## 1. 启动桥接（先验证链路）

用 Windows 的 Python 3（标准库即可，不用装任何包）：

```bat
cd W:\home\fox\projects\fate-wheel\setup\wechat_bridge
python bridge.py --selftest        :: 自检：按当前 sender 发一条测试消息
python bridge.py                   :: 启动服务，保持窗口开着
```

默认 `sender.type=file`，只把消息追加到 `outbox.txt`（不真发微信）。先这样验证
WSL 能连到桥接：

```bash
# WSL 侧
cd /home/fox/projects/fate-wheel
python3 main.py notify --test      # 应返回「微信已发送」，并能在 outbox.txt 看到
```

> **连不上？** WSL2 里 `127.0.0.1` 通常能转发到 Windows 监听的端口；若不行，
> 在 WSL 执行 `cat /etc/resolv.conf | grep nameserver` 取 Windows 主机 IP，
> 把 `config/notify.json` 的 `wechat.bridge_url` 改成
> `http://<那个IP>:8766/send`（Windows 防火墙需允许 8766 入站）。

## 2. 接上真正的个人号机器人

选一种 sender，改 `bridge_config.json`（复制 `bridge_config.example.json`）：

### 方式 A：command（最简单，接任何带命令行的机器人）

```json
"sender": {"type": "command", "command": "python C:\\bots\\send_wechat.py --msg {text}"}
```

`{text}` 会被替换成 JSON 转义后的消息字符串；你的脚本收到后自行调用 wxautox 等发送。

### 方式 B：module（推荐，进程内调用，少一次启动开销）

把你封装好的发送逻辑写成一个模块，例如 `setup/wechat_bridge/my_sender.py`：

```python
# pip install wxautox 后（示例，具体 API 以你装的版本为准）
import wxautox

def send(text: str) -> bool:
    wx = wxautox.WeChat()
    wx.send_msg(text, "文件传输助手")   # 收件人改成你自己/小号
    return True
```

配置：

```json
"sender": {"type": "module", "module": "my_sender", "func": "send"}
```

> 两种方式的收件人、登录态、微信版本兼容都由你的机器人代码负责，本项目不绑定实现。
> 个人号自动化有账号风险，请自行评估；敏感提醒建议也保留飞书渠道作为备份。

## 3. 开机自启（可选）

在 Windows「任务计划程序」建一个基本任务，触发器「登录时」，操作「启动程序」：
- 程序：`python.exe` 的完整路径
- 参数：`W:\home\fox\projects\fate-wheel\setup\wechat_bridge\bridge.py`
- 起始位置：上面的 `wechat_bridge` 目录

或用启动文件夹放一个 `.bat`：

```bat
@echo off
cd /d W:\home\fox\projects\fate-wheel\setup\wechat_bridge
pythonw bridge.py
```

## 4. 安全

- 在 `bridge_config.json` 与 `config/notify.json` 填同一个 `token`，否则同网段任何人都能推消息。
- 两个 `*_config.json` / `notify.json` 均已在 `.gitignore`，不会入库；模板带 `_` 前缀字段是说明，可留可删。

## 接口速查

| 项 | 值 |
|---|---|
| 地址 | `http://<Windows>:8766/send` |
| 请求 | `POST`，`Content-Type: application/json`，body `{"text":"...","token":"..."}` |
| 成功 | `200 {"ok": true, "message": "..."}` |
| 失败 | `4xx/502 {"ok": false, "message": "原因"}` |
| 健康检查 | `GET /health` |
