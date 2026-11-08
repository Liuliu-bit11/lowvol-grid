#!/usr/bin/env bash
# 一键推送到 GitHub。用法：
#   bash deploy.sh <你的GitHub用户名> [仓库名]
# 例：bash deploy.sh zhangsan lowvol-grid
set -e

USER_NAME="${1:?请传入 GitHub 用户名，例如: bash deploy.sh zhangsan lowvol-grid}"
REPO="${2:-lowvol-grid}"

echo "==> 检查环境"
command -v git >/dev/null || { echo "未安装 git，请先安装"; exit 1; }
git config user.name  >/dev/null || { echo "请先设置 git config user.name"; exit 1; }
git config user.email >/dev/null || { echo "请先设置 git config user.email"; exit 1; }

echo "==> 初始化仓库"
if [ ! -d .git ]; then
  git init
  git branch -M main
fi

echo "==> 提交文件"
git add -A
if git diff --staged --quiet; then
  echo "没有新变更，跳过提交"
else
  git commit -m "feat: 低波网格选股 全市场版"
fi

echo "==> 配置远程"
REMOTE="https://github.com/${USER_NAME}/${REPO}.git"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE"
else
  git remote add origin "$REMOTE"
fi
echo "    origin -> $REMOTE"

echo "==> 推送"
echo "    提示：密码处请填 Personal Access Token（不是登录密码）"
git push -u origin main

echo
echo "✅ 推送完成。接下来手动做两件事："
echo "   1. 打开 https://github.com/${USER_NAME}/${REPO}/settings/pages"
echo "      Source 选 Deploy from a branch → 分支 main → 目录 /docs → Save"
echo "   2. 等 1~2 分钟，访问："
echo "      https://${USER_NAME}.github.io/${REPO}/"
