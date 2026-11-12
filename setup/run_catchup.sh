#!/bin/bash
# launchd 调用的包装脚本：把补跑输出追加到日志。
# 单独成文件是为了避免在 plist 里写 shell 重定向（XML 转义坑）。
cd /Users/fox/modelProject/fate_wheel || exit 1
/usr/bin/python3 catchup.py >> logs/catchup.log 2>&1
