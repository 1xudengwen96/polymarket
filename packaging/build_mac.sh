#!/usr/bin/env bash
# ============================================================
# pm5hft macOS 版构建脚本（必须在 Mac 上运行，不能跨平台打包）
# 用法（在项目根目录）:
#   chmod +x packaging/build_mac.sh
#   ./packaging/build_mac.sh
# 产物: dist/pm5hft  （双击运行 = 机器人；`./pm5hft dashboard` = 监控面板）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] 创建虚拟环境..."
python3 -m venv .venv-mac
source .venv-mac/bin/activate

echo "[2/4] 安装依赖 + PyInstaller..."
pip install --upgrade pip
pip install -e . pyinstaller

echo "[3/4] 打包（onefile）..."
pyinstaller packaging/pm5hft.spec --noconfirm --distpath dist --workpath build

echo "[4/4] 组装交付目录 dist/..."
cp -r config artifacts dist/
cp .env.example README.md dist/
cat > dist/使用说明.txt <<'EOF'
pm5hft macOS 版
  双击运行 ./pm5hft        → 启动机器人（默认 paper 模式）
  ./pm5hft dashboard       → 启动监控面板，浏览器打开 http://127.0.0.1:8090
  实盘：复制 .env.example 为 .env 填私钥 + config/live.yaml 开 allow_live:true
  （config/、artifacts/ 必须和 pm5hft 放同一文件夹）
EOF

echo ""
echo "✅ 完成！产物在 dist/ 目录："
echo "    dist/pm5hft           （Mac 可执行文件）"
echo ""
echo "首次运行可能被 Gatekeeper 拦截，右键点 pm5hft → 打开 即可；"
echo "或终端执行:  xattr -d com.apple.quarantine dist/pm5hft"
