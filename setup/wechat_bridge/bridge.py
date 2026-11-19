#!/usr/bin/env python3
"""微信个人号桥接服务（跑在 Windows 侧，供 WSL 里的 fate-wheel 调用）。

定位
----
fate-wheel 的定时任务在 WSL 里跑，碰不到 Windows 上的微信客户端；个人号机器人
（wxautox / ntchat / wcferry 等）又各自依赖特定微信版本，绑死某一个会让本项目
跟着失效。所以这里只固定「HTTP 协议」，真正的发送动作做成可插拔 sender：

    WSL: python3 main.py notify --text "..."           （POST JSON）
      ↓  http://<Windows>:8766/send
    Windows: bridge.py  →  sender（file / command / module）

sender 三种模式（bridge_config.json → sender.type）：
  - file    ：追加到 outbox.txt，零依赖，先验证链路通不通（默认）。
  - command ：执行一条命令，用 {text} 占位符替换消息正文；可接任意带 CLI 的机器人。
  - module  ：import 一个你自己的模块并调用其 send(text) 函数；适合 wxautox 等。

只用标准库，不装任何依赖即可启动。详见同目录 README.md。
"""
import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "bridge_config.json")
EXAMPLE_PATH = os.path.join(HERE, "bridge_config.example.json")
OUTBOX = os.path.join(HERE, "outbox.txt")

DEFAULT = {
    "host": "0.0.0.0",
    "port": 8766,
    "token": "",
    "sender": {"type": "file", "path": OUTBOX},
}


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_PATH
    if not os.path.exists(path):
        return dict(DEFAULT)
    try:
        with open(path, encoding="utf-8") as f:
            return _merge(DEFAULT, json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取配置失败（{exc}），使用默认值", file=sys.stderr)
        return dict(DEFAULT)


def send_file(text, cfg):
    path = cfg.get("path") or OUTBOX
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    return True, f"已写入 {path}（未真正发微信；把 sender.type 改成 command/module）"


def send_command(text, cfg):
    tpl = cfg.get("command") or ""
    if not tpl:
        return False, "sender.command 未配置"
    # 用 shell 的字符串替换，交给用户自行保证命令安全（本机自用服务）
    cmd = tpl.replace("{text}", json.dumps(text, ensure_ascii=False))
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return False, f"命令执行失败：{exc}"
    if proc.returncode != 0:
        return False, f"命令返回 {proc.returncode}：{(proc.stderr or proc.stdout or '').strip()[:200]}"
    return True, "命令发送完成"


def send_module(text, cfg):
    name = cfg.get("module") or ""
    func = cfg.get("func") or "send"
    if not name:
        return False, "sender.module 未配置"
    try:
        import importlib
        mod = importlib.import_module(name)
        fn = getattr(mod, func)
    except Exception as exc:
        return False, f"加载发送模块 {name}.{func} 失败：{exc}"
    try:
        result = fn(text)
    except Exception as exc:
        return False, f"发送函数异常：{exc}"
    if result is False:
        return False, "发送函数返回 False"
    return True, f"已通过 {name}.{func} 发送"


SENDERS = {"file": send_file, "command": send_command, "module": send_module}


class Handler(BaseHTTPRequestHandler):
    cfg = None

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "service": "fate-wheel-wechat-bridge"})
        else:
            self._reply(404, {"ok": False, "message": "not found"})

    def do_POST(self):
        if self.path != "/send":
            self._reply(404, {"ok": False, "message": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._reply(400, {"ok": False, "message": f"bad json: {exc}"})
            return
        expected = (self.cfg.get("token") or "").strip()
        if expected and (data.get("token") or "") != expected:
            self._reply(401, {"ok": False, "message": "token mismatch"})
            return
        text = (data.get("text") or "").strip()
        if not text:
            self._reply(400, {"ok": False, "message": "empty text"})
            return
        sender = self.cfg.get("sender") or {}
        fn = SENDERS.get(sender.get("type"))
        if not fn:
            self._reply(500, {"ok": False, "message": f"unknown sender type: {sender.get('type')}"})
            return
        print(f"[{datetime.now():%H:%M:%S}] 收到消息（{len(text)} 字），sender={sender.get('type')}")
        ok, info = fn(text, sender)
        print(f"    → {'OK' if ok else 'FAIL'}: {info}")
        self._reply(200 if ok else 502, {"ok": ok, "message": info})

    def log_message(self, fmt, *args):
        pass  # 静音访问日志，只打印上面的业务行


def main():
    ap = argparse.ArgumentParser(description="fate-wheel 微信个人号桥接服务（Windows 侧）")
    ap.add_argument("--selftest", action="store_true", help="自检：用当前 sender 发一条测试消息")
    args = ap.parse_args()

    cfg = load_config()
    if args.selftest:
        sender = cfg.get("sender") or {}
        fn = SENDERS.get(sender.get("type"))
        if not fn:
            print(f"未知 sender：{sender.get('type')}", file=sys.stderr)
            return 2
        ok, info = fn("fate-wheel 桥接自检：这是一条测试消息。", sender)
        print(f"{'OK' if ok else 'FAIL'}: {info}")
        return 0 if ok else 1

    Handler.cfg = cfg
    host, port = cfg.get("host") or "0.0.0.0", int(cfg.get("port") or 8766)
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"fate-wheel 微信桥接已启动：http://{host}:{port}/send  （sender={cfg['sender']['type']}）")
    print("Ctrl+C 停止。WSL 侧用 python3 main.py notify --text '...' 测试。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
