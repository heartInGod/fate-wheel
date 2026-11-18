# 看板前端（web/）

`web/dashboard.html` 是持仓看板的**前端入口**，一个纯静态页面：HTML/CSS/交互逻辑都在这一个文件里，不内嵌任何数据。

## 数据从哪来

页面打开后通过 HTTP 请求 `../data/derived/dashboard.json` 加载数据。这份 JSON 由 Python 侧聚合生成：

```bash
python3 main.py dashboard            # 用现有缓存聚合（约 1~3 秒）
python3 main.py dashboard fresh=true # 先联网重抓快照再聚合
```

生成逻辑在 `agent/build_dashboard.py`——估值分区、政策分、长线建议表、归属判断、日志解析等计算**全部在 Python 侧完成**（`core_value.py` / `signals_engine.py` 等），前端只负责渲染。因此改计算逻辑只需改 Python，不用动前端。

`dashboard.json` 含账户/持仓等隐私数据，已加入 `.gitignore`，不入库；随时可用上面的命令再生。

## 怎么打开

浏览器用 `file://` 双击打开时，出于安全限制无法 fetch 本地 JSON，**必须通过本地 HTTP 服务**访问：

```bash
# 方式一：安装过 fate 命令（setup.sh 会自动装）
fate wheel        # 生成数据 + 启动服务 + 打开浏览器
fate serve        # 只启动服务（不生成、不打开浏览器）

# 方式二：直接起服务
cd 项目根目录
python3 -m http.server 8765 --bind 127.0.0.1
# 浏览器打开 http://localhost:8765/web/dashboard.html
```

> 服务必须从**项目根目录**启动，这样 `../data/derived/dashboard.json` 的相对路径才能正确解析。

## 页面功能

顶部 6 个标签页：持仓总览、个股详情、自选股、长线 & 政策、分析日志、操作日志。页面加载后还会定时（30 秒）拉取腾讯财经实时行情刷新价格——这部分是外链 JSONP，与本地数据加载相互独立。
