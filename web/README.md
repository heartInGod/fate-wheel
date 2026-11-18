# 看板前端资源（web/）

持仓看板的前端资源目录：样式 `web/style.css`、交互逻辑 `web/app.js`。
HTML 入口已上移到项目根目录的 `dashboard.html`，与 `main.py` 同级，作为控制台入口——直接双击即可打开。

## 文件分工

- `dashboard.html`（根目录）：页面骨架，引用 `web/style.css` 与 `web/app.js`，本身不含 CSS/JS。
- `web/style.css`：页面样式。
- `web/app.js`：渲染与交互逻辑（含实时行情 JSONP 拉取）。
- `data/derived/dashboard.js`：由 Python 生成的数据，用 `<script src>` 注入 `window.FW_DASHBOARD_DATA`。

## 数据从哪来

页面打开后优先用 `<script src>` 加载 `data/derived/dashboard.js`（该文件把数据赋给 `window.FW_DASHBOARD_DATA`）；若 JS 不存在，再通过 HTTP fetch `data/derived/dashboard.json` 回退。两份数据都由 Python 侧聚合生成：

```bash
python3 main.py dashboard            # 用现有缓存聚合（约 1~3 秒）
python3 main.py dashboard fresh=true # 先联网重抓快照再聚合
```

生成逻辑在 `agent/build_dashboard.py`——估值分区、政策分、长线建议表、归属判断、日志解析等计算**全部在 Python 侧完成**（`core_value.py` / `signals_engine.py` 等），前端只负责渲染。因此改计算逻辑只需改 Python，不用动前端。

`dashboard.json` / `dashboard.js` 含账户/持仓等隐私数据，已加入 `.gitignore`，不入库；随时可用上面的命令再生。两份内容相同，JS 只是把 JSON 包装成全局变量，方便 `file://` 下加载。

## 怎么打开

**直接双击根目录的 `dashboard.html` 即可**（`file://` 协议）。数据由 `<script src>` 加载，不受 `fetch` 在 `file://` 下的 CORS 限制，因此无需后台服务；行情是外链 JSONP（同为 script 标签），联网时会每 30 秒自动刷新。

如果只生成了 JSON、没有 JS（例如旧版本数据），则需要本地 HTTP 服务，`fetch` 回退才会生效：

```bash
# 方式一：安装过 fate 命令（setup.sh 会自动装）
fate wheel        # 生成数据 + 打开看板
fate serve        # 只启动本地 HTTP 服务（不生成、不打开浏览器）

# 方式二：直接起服务
cd 项目根目录
python3 -m http.server 8765 --bind 127.0.0.1
# 浏览器打开 http://localhost:8765/dashboard.html
```

> 服务必须从**项目根目录**启动，这样 `data/derived/dashboard.json` 的相对路径才能正确解析。正常情况下双击打开即可，不需要服务。

## 页面功能

顶部 6 个标签页：持仓总览、个股详情、自选股、长线 & 政策、分析日志、操作日志。页面加载后还会定时（30 秒）拉取腾讯财经实时行情刷新价格——这部分是外链 JSONP，与本地数据加载相互独立。
