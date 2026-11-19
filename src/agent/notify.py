#!/usr/bin/env python3
"""统一通知出口：微信个人号（HTTP 桥接）或飞书（lark-cli），由配置切换。

为什么要有这一层
----------------
重构前，9 条定时任务的 prompt 各自硬编码 `lark-cli im +messages-send ...`，
换一个通知渠道就要改所有 prompt。现在 prompt 只调用本脚本：

    python3 main.py notify --stdin < 消息文件
    python3 main.py notify --text '【盘中提醒】...'
    python3 main.py notify --channel feishu --text '...'   # 临时覆盖渠道
    python3 main.py notify --status                         # 查看当前配置
    python3 main.py notify --test                           # 发一条测试消息

渠道配置在 config/notify.json（模板 config/notify.example.json）：
  - channel=wechat（默认）：把文本 POST 到本地桥接服务，由 Windows 侧的微信
    个人号桥接驱动 PC 微信发送。桥接协议见 setup/wechat_bridge/README.md。
  - channel=feishu：保留原 lark-cli 私聊机器人实现，open_id 从配置读取。

设计原则：
  - 只用 Python 标准库（urllib / subprocess），不加任何 pip 依赖。
  - 消息文本不经过 shell 拼接：飞书用 subprocess 参数列表，微信用 JSON body，
    避免中文、单引号、换行导致命令注入或被截断。
  - 发送失败返回非 0 并在 stderr 打印原因；调用方（定时任务）据此在日志注明，
    不影响分析与记录本身。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from paths import ROOT

CONFIG_DIR = os.path.join(ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "notify.json")
EXAMPLE_PATH = os.path.join(CONFIG_DIR, "notify.example.json")

DEFAULTS = {
    "channel": "wechat",
    "wechat": {
        "bridge_url": "http://127.0.0.1:8766/send",
        "token": "",
        "timeout": 5,
    },
    "feishu": {
        "cli": "lark-cli",
        "open_id": "",
    },
}


def _merge(base, override):
    """只覆盖已认识的键，缺的用默认值补齐，兼容用户手写的精简配置。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    """读 config/notify.json；不存在时退回 example，再退回内置默认值。"""
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_PATH
    if not os.path.exists(path):
        return dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            return _merge(DEFAULTS, json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"警告：读取通知配置 {path} 失败（{exc}），使用默认值", file=sys.stderr)
        return dict(DEFAULTS)


def send_wechat(cfg, text):
    """POST 到本地微信个人号桥接服务。返回 (是否成功, 说明)。"""
    wc = cfg.get("wechat") or {}
    url = (wc.get("bridge_url") or "").strip()
    if not url:
        return False, "微信桥接地址未配置（config/notify.json → wechat.bridge_url）"
    body = json.dumps({
        "text": text,
        "source": "fate-wheel",
        "token": wc.get("token") or "",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        timeout = float(wc.get("timeout") or 5)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace").strip()
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            result = {"ok": resp.status < 400, "message": raw[:200]}
        if result.get("ok") is False:
            return False, f"微信桥接返回失败：{result.get('message') or result.get('error') or raw[:200]}"
        return True, f"微信已发送（桥接：{url}）"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return False, f"微信桥接 HTTP {exc.code}：{detail}"
    except Exception as exc:  # 连接拒绝 / 超时 / DNS 等
        return False, f"微信桥接不可用（{type(exc).__name__}: {exc}）；请确认 Windows 侧桥接服务已启动"


def send_feishu(cfg, text):
    """调用 lark-cli 私聊机器人（原实现，渠道可切换时保留）。返回 (是否成功, 说明)。"""
    fs = cfg.get("feishu") or {}
    cli = (fs.get("cli") or "lark-cli").strip()
    oid = (fs.get("open_id") or "").strip()
    if not oid:
        return False, "飞书 open_id 未配置（config/notify.json → feishu.open_id）"
    if not _which(cli):
        return False, f"找不到 {cli}，飞书渠道不可用（可切回 channel=wechat）"
    cmd = [cli, "im", "+messages-send", "--as", "bot", "--user-id", oid, "--markdown", text]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return False, f"飞书命令执行失败：{exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        return False, f"飞书发送失败：{detail}（检查 {cli} auth status）"
    return True, f"飞书已发送给 {oid}"


def _which(name):
    from shutil import which
    return which(name)


def status(cfg):
    ch = cfg.get("channel")
    print(f"当前通知渠道：{ch}")
    if ch == "wechat":
        wc = cfg.get("wechat") or {}
        print(f"  桥接地址：{wc.get('bridge_url')}")
        print(f"  超时：{wc.get('timeout')} 秒；token：{'已设置' if wc.get('token') else '未设置'}")
    elif ch == "feishu":
        fs = cfg.get("feishu") or {}
        print(f"  飞书 open_id：{fs.get('open_id')}（{_which(fs.get('cli') or 'lark-cli') or 'lark-cli 未安装'}）")
    else:
        print(f"  未知渠道，可用值：wechat / feishu")
    print(f"  配置文件：{CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_PATH}"
          + ("" if os.path.exists(CONFIG_PATH) else "（当前回退到模板，可复制为 notify.json 后修改）"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="fate-wheel 统一通知出口（微信 / 飞书）")
    ap.add_argument("message", nargs="*", help="消息文本（也可 --text / --stdin / --file）")
    ap.add_argument("--text", help="消息文本")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读取消息全文")
    ap.add_argument("--file", help="从文件读取消息（UTF-8）")
    ap.add_argument("--channel", choices=("wechat", "feishu"), help="临时覆盖配置里的渠道")
    ap.add_argument("--test", action="store_true", help="发一条测试消息")
    ap.add_argument("--status", action="store_true", help="显示当前通知配置并退出")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.status:
        status(cfg)
        return 0

    if args.test:
        import platform
        text = f"fate-wheel 通知测试：{platform.node()} 上的{args.channel or cfg.get('channel')}通知已打通。"
    else:
        if args.stdin:
            text = sys.stdin.read()
        elif args.file:
            try:
                with open(args.file, encoding="utf-8") as f:
                    text = f.read()
            except OSError as exc:
                print(f"读取消息文件失败：{exc}", file=sys.stderr)
                return 2
        elif args.text is not None:
            text = args.text
        elif args.message:
            text = " ".join(args.message)
        else:
            ap.error("请提供消息：位置参数 / --text / --stdin / --file / --test 之一")

    text = text.strip()
    if not text:
        print("消息为空，未发送", file=sys.stderr)
        return 2

    channel = args.channel or cfg.get("channel")
    if channel == "wechat":
        ok, info = send_wechat(cfg, text)
    elif channel == "feishu":
        ok, info = send_feishu(cfg, text)
    else:
        print(f"未知通知渠道：{channel}（应为 wechat / feishu）", file=sys.stderr)
        return 2

    print(info if ok else f"通知未发送：{info}", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
