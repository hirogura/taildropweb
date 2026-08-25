# Taildrop Web

Tailscale ネットワーク内でファイルを送信するための Web UI です。

- Tailscale のデバイス一覧から送信先を選択
- ドラッグ＆ドロップ / クリック選択でファイルをアップロードして送信（複数可）
- サーバ上の `/opt/lxd-data/taildrop/` にあるファイルをチェックボックスで選択して送信（フォルダは zip 化）
- Tailscale IP のみにバインド（LAN / 外部には非公開）
- 送信は `tailscale file cp` を使用

## 必要環境

- Linux（systemd 使用）
- Tailscale がインストール済みかつ接続済み
- Python 3（`python3-venv` があると便利）
- git

## インストール

`install-taildropweb.sh` を実行すると、GitHub からソースを取得してインストールします。

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/hirogura/taildropweb/main/install-taildropweb.sh)"
```

または、スクリプトをダウンロードして実行：

```bash
curl -fsSL -O https://raw.githubusercontent.com/hirogura/taildropweb/main/install-taildropweb.sh
sudo bash install-taildropweb.sh
```

スクリプトが行うこと:

1. 前提条件の確認（python3 / git / tailscale）
2. GitHub からソースを `/opt/taildrop-web` に取得（初回はクローン、以降は更新）
3. Python 仮想環境 (`venv`) と依存パッケージをセットアップ
4. systemd サービス `taildrop-web` を作成して起動

インストール完了後、表示される URL にアクセスしてください:

- `http://<Tailscale IP>:3349`（サーバローカルのみ）
- `https://<MagicDNS名>:3349`（Tailnet 内・Tailscale Serve 経由）

### 設定（環境変数による上書き）

スクリプトの挙動は環境変数で変更できます（デフォルト値は括弧内）。

| 変数 | 説明 | デフォルト |
|------|------|------------|
| `REPO_URL` | 取得元の GitHub リポジトリ | `https://github.com/hirogura/taildropweb.git` |
| `BRANCH` | 取得するブランチ | `main` |
| `INSTALL_DIR` | インストール先ディレクトリ | `/opt/taildrop-web` |
| `SERVICE_NAME` | systemd サービス名 | `taildrop-web` |
| `PORT` | バインドするポート | `3349` |
| `TAILSCALE_HTTPS_PORT` | Tailscale Serve で公開する HTTPS ポート | `3349` |
| `SERVER_FILE_DIR` | サーバ上のファイル置き場 | `/opt/lxd-data/taildrop` |

例:

```bash
sudo PORT=8080 INSTALL_DIR=/opt/example bash install-taildropweb.sh
```

## 更新

最新版への更新は、インストールスクリプトを再実行するだけです（リポジトリを pull して再起動します）。

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/hirogura/taildropweb/main/install-taildropweb.sh)"
```

## アンインストール

```bash
# 1. サービスを停止・無効化
sudo systemctl stop taildrop-web
sudo systemctl disable taildrop-web

# 2. systemd ユニットを削除
sudo rm /etc/systemd/system/taildrop-web.service
sudo systemctl daemon-reload

# 3. インストール先を削除（アプリ本体）
sudo rm -rf /opt/taildrop-web

# 4. 必要に応じてサーバ上のファイル置き場も削除（中身のファイルも消えるので注意）
# sudo rm -rf /opt/lxd-data/taildrop
```

## ファイル構成

| ファイル | 説明 |
|----------|------|
| `app.py` | Flask アプリ本体 |
| `template.html` | Web UI のテンプレート |
| `favicon.svg` | ファビコン |
| `requirements.txt` | 依存パッケージ一覧 |
| `install-taildropweb.sh` | インストールスクリプト |

## ライセンス

このプロジェクトは [MIT License](LICENSE) の下で公開されています。
