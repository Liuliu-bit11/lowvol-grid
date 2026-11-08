# 低波网格选股 · 全市场版

按「高股息 + 低位 + 技术面反转」扫描全 A 股，给出建仓/观察/减仓/卖出四色区间与自适应网格档位。

数据源：东方财富公开行情与分红接口（无需密钥）。每个交易日收盘后由 GitHub Actions 自动重算并提交快照。

## 目录结构

```
fetch.py                      数据管线：抓全市场 → 算因子 → 写快照
requirements.txt              仅 requests
docs/index.html               前端（纯静态单页，无构建步骤）
docs/data/full.json           因子快照（5521 只）
docs/data/klines.json         近 120 日收盘价快照，供详情页画 K 线（规避跨域）
.github/workflows/update.yml  每交易日 16:30（北京时间）自动更新
cache/                        本地抓取缓存，不入库
```

## 本地运行

```bash
pip install -r requirements.txt
python fetch.py                 # 首次约 10-15 分钟（约 5600 只 K 线）
python -m http.server 8080 -d docs
# 打开 http://localhost:8080
```

再跑一次会命中 `cache/`，几十秒完成。

## 部署成可分享链接（GitHub Pages）

### 方式一：一键脚本（推荐）

先在 GitHub 网页端新建一个空仓库（不要勾选初始化 README），然后按系统选择：

**Windows**（本包已提供 `deploy.bat`）

直接双击 `deploy.bat` 即可，无需输入参数。默认使用用户名 `Liuliu-bit11`、仓库名 `lowvol-grid`。

脚本内置了这些处理，不需要你预先配置 Git 身份：

- 自动用 `-c user.name` / `-c user.email` 传入提交身份，避免 `unable to auto-detect email address`
- 提交后校验 HEAD 是否存在，避免出现 `src refspec main does not match any`
- 推送前重建 origin 远程，避免上次失败残留的配置干扰
- 脚本为纯 ASCII 编码，不会在中文字符控制台出现乱码命令

**macOS / Linux**

```bash
bash deploy.sh <你的GitHub用户名> lowvol-grid
```

注意 `<你的用户名>` 是占位符，要替换成真实用户名、**不要连尖括号一起输入**。

两种脚本都会自动完成 init / commit / 配置远程 / push。推送时密码处填 **Personal Access Token**，不是账号登录密码。

### 方式二：手动命令

```bash
git init
git add -A
git commit -m "feat: 低波网格选股 全市场版"
git branch -M main
git remote add origin https://github.com/<你的用户名>/lowvol-grid.git
git push -u origin main
```

2. 仓库 **Settings → Pages**：Source 选 `Deploy from a branch`，分支 `main`，目录 `/docs`，保存。

3. 一两分钟后访问：`https://<你的用户名>.github.io/lowvol-grid/`

## 自动更新

`.github/workflows/update.yml` 已配置：

- 触发：每周一至周五 UTC 08:30（北京时间 16:30），A 股 15:00 收盘后留 30 分钟结算
- 手动触发：仓库 **Actions → 每日更新行情快照 → Run workflow**
- 行为：`python fetch.py` → 有变化则提交 `docs/data/full.json` 与 `docs/data/klines.json`
- 权限：workflow 内已声明 `permissions: contents: write`，无需额外配置 token

GitHub Actions 对公开仓库免费。若要改时间，编辑 cron（注意是 UTC）。

> 首次运行需抓约 5600 只 K 线，`timeout-minutes: 40` 已留足余量。

## 口径说明

**股息率**：`最近一个完整财年 DPS ÷ 现价`。分红按财年归集（不把中报与年报挤进同一个 12 个月窗口），送转按比例前复权。

**分红中断**：若最近分红财年早于「当前年 − 2」，判定为已停发，股息率置 0 —— 否则会出现万科式的「用早已停发的旧分红除以大跌后的现价」得到 20% 假红利。

**股息率分位**：当前股息率在该股自身近 3 年股息率序列中的百分位。分位 100% 表示处于自身历史最高档，通常对应「股价在一年低位 + 分红未减」。

**仓位区间**：分位 ≥75% 建仓区；50%~75% 观察区；25%~50% 减仓区；<25% 卖出区。

**技术共振信号**（布林带位置 × MACD 动能，日/周/月三周期独立计算，下轨区定义为 %B ≤ 0.15）：

| 信号 | 条件 |
|---|---|
| 反转 | 三周期均在下轨区，且 MACD 多头 ≥1 |
| 筑底 | ≥2 周期在下轨区，且 MACD 多头 ≥1 |
| 探底 | ≥1 周期在下轨区 |
| 无机会 | 无周期在下轨区 |

**网格步长**：`自动` 按该股股息率极值定范围（约 6 格），用 P75/P50/P25 分位切四色区间；也可切 `1%` / `0.5%` 固定步长。档位价 = 每股派息 ÷ 目标股息率。

## 已知限制

- 接口为东财公开接口，可能限流或字段变更；管线对失败做了跳过处理（`skipped` 计数），不会整体崩溃
- 连续分红年数受数据窗口限制（FY2020 起），前端显示上限为「6+」
- 快照为收盘后静态数据，盘中不刷新
- 本工具仅做数据整理与技术指标计算，不构成投资建议
