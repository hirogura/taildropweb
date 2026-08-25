#!/usr/bin/env bash
# =============================================================================
#  Taildrop Web UI — インストールスクリプト (GitHub 版)
#  - GitHub (https://github.com/hirogura/taildropweb) からソースを取得してインストール
#  - ファイルのドラッグ＆ドロップ / クリック選択で送信（複数可）
#  - サーバ上の /opt/lxd-data/taildrop/ からチェックボックスで選択送信
#  - Tailscale IP のみにバインド（LAN/外部には非公開）
#
#  使い方:
#    sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/hirogura/taildropweb/main/install-taildropweb.sh)"
#  または
#    curl -fsSL -O https://raw.githubusercontent.com/hirogura/taildropweb/main/install-taildropweb.sh
#    sudo bash install-taildropweb.sh
# =============================================================================
set -euo pipefail

# ── 設定（環境変数で上書き可能）──────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/hirogura/taildropweb.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/taildrop-web}"
SERVICE_NAME="${SERVICE_NAME:-taildrop-web}"
PORT="${PORT:-3349}"
TAILSCALE_HTTPS_PORT="${TAILSCALE_HTTPS_PORT:-3348}"
SERVER_FILE_DIR="${SERVER_FILE_DIR:-/opt/lxd-data/taildrop}"

info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

# ── 前提チェック ───────────────────────────────────────────────────────────────
info "前提確認..."
command -v python3 >/dev/null 2>&1 || die "python3 が見つかりません"
command -v git     >/dev/null 2>&1 || die "git が見つかりません"
command -v tailscale >/dev/null 2>&1 || die "tailscale が見つかりません"
tailscale status >/dev/null 2>&1    || die "tailscale が接続されていません"

TS_IP=$(tailscale ip -4 2>/dev/null | head -1) || die "Tailscale IP が取得できません"
TS_HOSTNAME=$(tailscale status --json \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))" \
  2>/dev/null) || TS_HOSTNAME="${TS_IP}"
ok "Tailscale IP: ${TS_IP} / hostname: ${TS_HOSTNAME}"

# ── ディレクトリ作成 ──────────────────────────────────────────────────────────
info "ディレクトリ作成..."
mkdir -p "${SERVER_FILE_DIR}"   # サーバファイル置き場（なければ作成）

# ── ソース取得（GitHub）────────────────────────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  info "リポジトリを更新中 (${REPO_URL})..."
  git -C "${INSTALL_DIR}" remote set-url origin "${REPO_URL}" 2>/dev/null \
    || git -C "${INSTALL_DIR}" remote add origin "${REPO_URL}"
  git -C "${INSTALL_DIR}" fetch origin || die "git fetch に失敗しました"
  git -C "${INSTALL_DIR}" reset --hard "origin/${BRANCH}" || die "git reset に失敗しました"
  ok "リポジトリ更新完了"
elif [[ -d "${INSTALL_DIR}" && -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null)" ]]; then
  # 旧スクリプト等で作られた git 管理外の既存インストール → git 管理に移行
  # （venv など未追跡ファイルはそのまま保持）
  warn "既存のインストールを git 管理に移行します（venv 等は保持）..."
  git -C "${INSTALL_DIR}" init -q || die "git init に失敗しました"
  git -C "${INSTALL_DIR}" remote add origin "${REPO_URL}" 2>/dev/null \
    || git -C "${INSTALL_DIR}" remote set-url origin "${REPO_URL}"
  git -C "${INSTALL_DIR}" fetch origin || die "git fetch に失敗しました"
  git -C "${INSTALL_DIR}" checkout -q -f -B "${BRANCH}" "origin/${BRANCH}" \
    || die "git checkout に失敗しました"
  ok "git 管理への移行完了"
else
  info "リポジトリをクローン中 (${REPO_URL})..."
  git clone -b "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}" || die "git clone に失敗しました"
  ok "リポジトリクローン完了"
fi

# ── venv 作成 & 依存パッケージインストール ────────────────────────────────────
info "Python venv を確認..."

if [[ -d "${INSTALL_DIR}/venv" ]]; then
  if ! "${INSTALL_DIR}/venv/bin/python" -c "import sys" >/dev/null 2>&1; then
    warn "既存のvenvが壊れています。削除して再作成します..."
    rm -rf "${INSTALL_DIR}/venv"
  fi
fi

if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
  # venv が使えるか確認
  TMPV=$(mktemp -d)
  if ! python3 -m venv "${TMPV}/test" 2>/dev/null; then
    rm -rf "${TMPV}"
    warn "python3-venv をインストールします..."
    PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    SUDO_CMD=""; [[ $EUID -ne 0 ]] && SUDO_CMD="sudo"
    ${SUDO_CMD} apt-get update -qq
    ${SUDO_CMD} apt-get install -y -qq "python3.${PYVER}-venv" 2>/dev/null || \
    ${SUDO_CMD} apt-get install -y -qq python3-venv || \
    die "python3-venv のインストールに失敗しました"
  else
    rm -rf "${TMPV}"
  fi
  python3 -m venv "${INSTALL_DIR}/venv" || die "venv の作成に失敗しました"
  ok "venv 作成完了"
else
  ok "venv は既に存在します（スキップ）"
fi

PIP="${INSTALL_DIR}/venv/bin/pip"
PYTHON="${INSTALL_DIR}/venv/bin/python"

info "依存パッケージを確認..."
REQS_OK=1
for pkg in flask werkzeug; do
  if ! "${PYTHON}" -c "import importlib.metadata; importlib.metadata.version('${pkg}')" >/dev/null 2>&1; then
    REQS_OK=0
    break
  fi
done

if [[ "${REQS_OK}" -eq 1 ]]; then
  ok "依存パッケージはインストール済みです（スキップ）"
else
  info "依存パッケージをインストール (requirements.txt)..."
  "${PIP}" install --quiet -r "${INSTALL_DIR}/requirements.txt" \
    || die "依存パッケージのインストールに失敗しました"
  ok "依存パッケージインストール完了"
fi

# ── systemd ユニットファイル生成 ───────────────────────────────────────────────
info "systemd ユニットファイルを生成..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=Taildrop Web UI
After=network.target tailscaled.service
Wants=tailscaled.service

[Service]
Type=simple
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/app.py
WorkingDirectory=${INSTALL_DIR}
Environment=PORT=${PORT}
Restart=on-failure
RestartSec=5
# Tailscale IP の取得が間に合わない場合のリトライ猶予
StartLimitIntervalSec=60
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
EOF
ok "systemd ユニットファイル生成完了"

# ── Tailscale Serve 設定（HTTPS化・Tailnet内のみ公開）────────────────────────
info "Tailscale Serve を設定中..."
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve --bg --https="${TAILSCALE_HTTPS_PORT}" http://127.0.0.1:${PORT} \
    || warn "tailscale serve の設定に失敗しました"
  TS_DOMAIN=$(tailscale status --json \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Self',{}).get('DNSName','').rstrip('.'))" \
    2>/dev/null || echo "")
  if [[ -n "${TS_DOMAIN}" ]]; then
    ok "Tailscale Serve: https://${TS_DOMAIN}:${TAILSCALE_HTTPS_PORT}"
  else
    warn "Tailscale ドメインの取得に失敗しました"
  fi
else
  warn "tailscale コマンドが見つかりません。Serve設定をスキップします"
fi

# ── サービス起動 ──────────────────────────────────────────────────────────────
info "サービスを起動..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

# 起動確認（アプリは 127.0.0.1 のみにバインドするためローカルで確認）
for i in $(seq 1 15); do
  if curl -s --max-time 1 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if curl -s --max-time 1 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
  ok "サービス起動完了"
else
  die "サービスの起動に失敗しました。journalctl -u ${SERVICE_NAME} -n 30 で確認してください"
fi

# ── 完了サマリー ──────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
ok "セットアップ完了！"
echo ""
echo "  Web UI : https://${TS_DOMAIN}:${TAILSCALE_HTTPS_PORT}  (Tailnet内のみ・HTTPS)"
if [[ -n "${TS_DOMAIN}" ]]; then
  echo "  Web UI : http://127.0.0.1:${PORT}  (サーバローカルのみ)"
fi
echo "  サーバファイル置き場: ${SERVER_FILE_DIR}"
echo "  インストール先     : ${INSTALL_DIR} (GitHub: ${REPO_URL})"
echo ""
echo "  ▶ Web UI にアクセスして送信先デバイスを選択し、ファイルを送信してください。"
echo ""
echo "  管理コマンド:"
echo "    systemctl status ${SERVICE_NAME}"
echo "    systemctl restart ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "  更新: このスクリプトを再実行するだけで最新版に更新されます。"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
