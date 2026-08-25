#!/usr/bin/env python3
"""
Taildrop Web UI
- Tailscale IP のみにバインド（LAN/外部には非公開）
- ファイルのドラッグ＆ドロップ / クリック選択で送信（複数可）
- サーバ上の /opt/lxd-data/taildrop/ からチェックボックスで選択送信
"""

from flask import Flask, request, render_template, jsonify, send_from_directory
from werkzeug.utils import secure_filename
import subprocess
import json
import os
import tempfile
import zipfile
import time
import threading
import collections

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=BASE_DIR)
SERVER_FILE_DIR = "/opt/lxd-data/taildrop"
APP_VERSION = "1.2.0"
SERVICE_NAME = os.environ.get("SERVICE_NAME", "taildrop-web")
BRANCH = os.environ.get("BRANCH", "main")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
OLD_CONFIG_FILE = "/opt/taildrop-auto/config.json"
LOG_MAXLEN = 200  # ログ最大行数

DEFAULT_AUTO_CONFIG = {
    "watch_folder":   "",
    "target_devices": [],
    "enabled":        False,
}


def run_tailscale_cp(src: str, device: str):
    try:
        subprocess.check_call(
            ['tailscale', 'file', 'cp', src, f'{device}:'],
            timeout=300,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return True, None
    except subprocess.TimeoutExpired:
        return False, 'タイムアウト'
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors='replace').strip() if e.stderr else ''
        return False, stderr or f'終了コード {e.returncode}'
    except Exception as e:
        return False, str(e)


def human_size(size: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024:
            return f'{size} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


def _git(args):
    return subprocess.check_output(
        ['git', '-C', BASE_DIR] + args, timeout=60
    ).decode().strip()


# ─── 自動送信: ログ ─────────────────────────────────────────────────────────
log_buf  = collections.deque(maxlen=LOG_MAXLEN)
log_lock = threading.Lock()


def log(level: str, msg: str):
    ts   = time.strftime('%H:%M:%S')
    line = f'[{ts}][{level}] {msg}'
    with log_lock:
        log_buf.append({'level': level, 'msg': line})
    print(line, flush=True)


# ─── 自動送信: 設定 ─────────────────────────────────────────────────────────
auto_config = {}
observer    = None


def load_auto_config():
    global auto_config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                auto_config = {**DEFAULT_AUTO_CONFIG, **json.load(f)}
            return
        except (json.JSONDecodeError, OSError) as e:
            log('ERR', f'config.json の読み込みに失敗しました（既定値で起動）: {e}')
    elif os.path.exists(OLD_CONFIG_FILE):
        try:
            # 旧 taildrop-auto から設定を引き継ぐ（port は引き継がない）
            with open(OLD_CONFIG_FILE, 'r') as f:
                old = json.load(f)
            auto_config = {k: old[k] for k in DEFAULT_AUTO_CONFIG if k in old}
            save_auto_config()
            log('INFO', '旧 taildrop-auto の設定を引き継ぎました')
            return
        except Exception as e:
            log('WARN', f'旧設定の引き継ぎに失敗しました: {e}')
    auto_config = DEFAULT_AUTO_CONFIG.copy()
    save_auto_config()


def save_auto_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(auto_config, f, indent=2, ensure_ascii=False)


# ─── 自動送信: ファイル監視ハンドラ ────────────────────────────────────────
class AutoSendHandler(FileSystemEventHandler):
    def __init__(self):
        self._pending = {}
        self._lock    = threading.Lock()

    def _handle(self, path: str):
        name = os.path.basename(path)
        if name.startswith('.') or name.endswith(('.tmp', '.part', '.crdownload')):
            return
        with self._lock:
            already = path in self._pending
            self._pending[path] = time.time()
            if already:
                return
        threading.Thread(target=self._delayed_send, args=(path,), daemon=True).start()

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def _delayed_send(self, path: str):
        # 書き込みが落ち着くまで待つ（最後のイベントから1.5秒）
        while True:
            with self._lock:
                last = self._pending.get(path, 0)
            if time.time() - last >= 1.5:
                break
            time.sleep(0.3)

        with self._lock:
            self._pending.pop(path, None)

        if not os.path.isfile(path):
            return

        devices = auto_config.get('target_devices', [])
        if not devices:
            log('WARN', f'送信先が未設定のためスキップ: {os.path.basename(path)}')
            return

        fname = os.path.basename(path)
        log('INFO', f'検知: {fname} → {", ".join(devices)} に送信開始')

        for dev in devices:
            ok_, err = run_tailscale_cp(path, dev)
            if ok_:
                log('OK',  f'✅ {fname} → {dev}')
            else:
                log('ERR', f'❌ {fname} → {dev} : {err}')


def start_watching():
    global observer
    try:
        if observer and observer.is_alive():
            observer.stop()
            observer.join(timeout=5)
        observer = None

        if not auto_config.get('enabled', False):
            log('INFO', '監視停止中')
            return

        watch_dir = auto_config.get('watch_folder', '').strip()
        if not watch_dir:
            log('WARN', '監視フォルダが未設定です')
            return
        if not os.path.isdir(watch_dir):
            log('WARN', f'監視フォルダが見つかりません: {watch_dir}')
            return

        observer = Observer()
        observer.schedule(AutoSendHandler(), watch_dir, recursive=False)
        observer.start()
        log('INFO', f'監視開始: {watch_dir}')
    except Exception as e:
        log('ERR', f'監視の開始に失敗しました: {e}')


def _schedule_service_restart(delay: float = 1.0):
    """レスポンス返却後にサービスを再起動する"""
    def worker():
        time.sleep(delay)
        try:
            subprocess.run(['systemctl', 'restart', SERVICE_NAME], timeout=30)
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()


@app.route('/api/version')
def api_version():
    up_to_date = None
    try:
        local  = _git(['rev-parse', 'HEAD'])
        remote = _git(['rev-parse', f'origin/{BRANCH}'])
        up_to_date = (local == remote)
    except Exception:
        pass
    return jsonify({'version': APP_VERSION, 'up_to_date': up_to_date})


@app.route('/api/update', methods=['POST'])
def api_update():
    steps = []
    try:
        _git(['fetch', 'origin', BRANCH])
        steps.append('GitHubから最新ソースを取得しました')
    except Exception as e:
        return jsonify({'ok': False, 'msg': f'❌ 更新失敗（fetch）: {e}'}), 500

    try:
        _git(['reset', '--hard', f'origin/{BRANCH}'])
        steps.append(f'origin/{BRANCH} に更新しました')
    except Exception as e:
        return jsonify({'ok': False, 'msg': f'❌ 更新失敗（reset）: {e}'}), 500

    pip = os.path.join(BASE_DIR, 'venv', 'bin', 'pip')
    if os.path.exists(pip):
        try:
            subprocess.run(
                [pip, 'install', '-q', '-r',
                 os.path.join(BASE_DIR, 'requirements.txt')],
                cwd=BASE_DIR, timeout=300,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                check=True,
            )
            steps.append('依存パッケージを確認しました')
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode(errors='replace').strip() if e.stderr else ''
            return jsonify({'ok': False,
                            'msg': f'❌ 依存パッケージのインストールに失敗: {err}'}), 500

    _schedule_service_restart()
    steps.append('まもなくサービスを再起動します...')
    return jsonify({'ok': True, 'msg': '\n'.join(steps)})


@app.route('/api/restart', methods=['POST'])
def api_restart():
    _schedule_service_restart()
    return jsonify({'ok': True, 'msg': 'まもなくサービスを再起動します...'})


# ─── 自動送信 API ───────────────────────────────────────────────────────────
@app.route('/api/auto-config', methods=['GET', 'POST'])
def api_auto_config():
    global auto_config
    if request.method == 'POST':
        try:
            data = request.get_json(force=True, silent=True) or {}
            for k in ('watch_folder', 'enabled', 'target_devices'):
                if k in data:
                    auto_config[k] = data[k]
            save_auto_config()
            start_watching()
            return jsonify({'ok': True})
        except Exception as e:
            log('ERR', f'/api/auto-config 処理中にエラー: {e}')
            return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify(auto_config)


@app.route('/api/log')
def api_log():
    with log_lock:
        lines = list(log_buf)
    return jsonify({'lines': lines})


@app.route('/api/log/clear', methods=['POST'])
def api_log_clear():
    with log_lock:
        log_buf.clear()
    return jsonify({'ok': True})


@app.route('/')
def index():
    return render_template('template.html')


@app.route('/favicon.svg')
def favicon():
    return send_from_directory(BASE_DIR, 'favicon.svg', mimetype='image/svg+xml')


@app.route('/devices')
def get_devices():
    try:
        result = subprocess.check_output(['tailscale', 'status', '--json'], timeout=10)
        data   = json.loads(result)
        peers  = []
        for peer in data.get('Peer', {}).values():
            # dns_name: tailscale file cp の宛先解決に使う実際の名前（DNSNameの先頭ラベル）
            # name:     UI表示用のわかりやすい名前（HostNameを優先、スペース等含んでもOK）
            dns_name = peer.get('DNSName', '').rstrip('.').split('.')[0]
            if not dns_name:
                continue
            host_name = peer.get('HostName') or ''
            # iOS/iPadOS等で HostName が "localhost" になるケースがあるため、その場合は dns_name を表示に使う
            name = host_name if host_name and host_name.lower() != 'localhost' else dns_name
            online   = peer.get('Online', False)
            lastseen = '接続中' if online else (peer.get('LastSeen') or '不明')[:16]
            peers.append({'name': name, 'dns_name': dns_name, 'online': online, 'lastseen': lastseen})
        peers.sort(key=lambda x: (not x['online'], x['name']))
        return jsonify(peers)
    except Exception as e:
        return jsonify([{'name': f'エラー: {e}', 'dns_name': '', 'online': False, 'lastseen': ''}])


@app.route('/server-files')
def server_files():
    req_dir = (request.args.get('dir') or '').strip() or SERVER_FILE_DIR
    base = os.path.realpath(req_dir)
    if not os.path.isdir(base):
        return jsonify({'error': f'フォルダが見つかりません: {req_dir}'}), 404
    items = []
    try:
        for entry in sorted(os.scandir(base), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                if entry.is_dir():
                    total    = sum(f.stat().st_size for f in os.scandir(entry.path) if f.is_file())
                    size_str = f'{human_size(total)} (フォルダ)'
                    is_dir   = True
                else:
                    size_str = human_size(entry.stat().st_size)
                    is_dir   = False
                items.append({'name': entry.name, 'size_str': size_str, 'is_dir': is_dir})
            except PermissionError:
                pass
    except FileNotFoundError:
        pass
    except PermissionError:
        return jsonify({'error': f'アクセス権がありません: {base}'}), 403
    return jsonify({'dir': base, 'items': items})


@app.route('/send-upload', methods=['POST'])
def send_upload():
    f = request.files.get('file')
    if not f:
        return jsonify({'results': [{'ok': False, 'msg': '❌ ファイルが見つかりません'}]})
    try:
        devices = json.loads(request.form.get('devices', '[]'))
    except Exception:
        return jsonify({'results': [{'ok': False, 'msg': '❌ 送信先パラメータが不正'}]})

    safe_name = secure_filename(f.filename) or 'upload'
    results   = []
    with tempfile.TemporaryDirectory() as tmp:
        save_path = os.path.join(tmp, safe_name)
        f.save(save_path)
        for dev in devices:
            ok, err = run_tailscale_cp(save_path, dev)
            msg = f'✅ {safe_name} → {dev}' if ok else f'❌ {safe_name} → {dev} : {err}'
            results.append({'ok': ok, 'msg': msg})
    return jsonify({'results': results})


@app.route('/send-server', methods=['POST'])
def send_server():
    body       = request.get_json(force=True, silent=True) or {}
    file_names = body.get('files', [])
    devices    = body.get('devices', [])
    base       = os.path.realpath(body.get('dir') or SERVER_FILE_DIR)
    results    = []

    if not os.path.isdir(base):
        return jsonify({'results': [{'ok': False, 'msg': f'⚠️ フォルダが見つかりません: {base}'}]})

    with tempfile.TemporaryDirectory() as tmp:
        for name in file_names:
            safe_name = os.path.basename(name)
            if not safe_name or safe_name != name:
                results.append({'ok': False, 'msg': f'⚠️ 不正なファイル名: {name}'})
                continue
            src = os.path.join(base, safe_name)
            if not os.path.realpath(src).startswith(base + os.sep):
                results.append({'ok': False, 'msg': f'⚠️ 不正なパス: {name}'})
                continue
            if not os.path.exists(src):
                results.append({'ok': False, 'msg': f'⚠️ 見つかりません: {safe_name}'})
                continue

            if os.path.isdir(src):
                # サーバ上のフォルダはzip化して送信
                zip_name = safe_name + '.zip'
                zip_path = os.path.join(tmp, zip_name)
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for root, dirs, ffiles in os.walk(src):
                        for ff in ffiles:
                            full = os.path.join(root, ff)
                            zf.write(full, os.path.relpath(full, os.path.dirname(src)))
                send_path, display_name = zip_path, zip_name
            else:
                send_path, display_name = src, safe_name

            for dev in devices:
                ok, err = run_tailscale_cp(send_path, dev)
                msg = f'✅ {display_name} → {dev}' if ok else f'❌ {display_name} → {dev} : {err}'
                results.append({'ok': ok, 'msg': msg})

    return jsonify({'results': results})


def get_ts_dns_name() -> str:
    try:
        data = json.loads(subprocess.check_output(
            ['tailscale', 'status', '--json'], timeout=5
        ))
        return (data.get('Self', {}).get('DNSName') or '').rstrip('.')
    except Exception:
        return ''


def main():
    template_path = os.path.join(BASE_DIR, 'template.html')
    if not os.path.exists(template_path):
        print(f'[ERR] テンプレートファイルが見つかりません: {template_path}')
        raise SystemExit(1)

    os.makedirs(SERVER_FILE_DIR, exist_ok=True)

    load_auto_config()
    start_watching()

    host  = os.environ.get('HOST', '127.0.0.1')
    port  = int(os.environ.get('PORT', 3349))
    https_port = int(os.environ.get('TAILSCALE_HTTPS_PORT', 3348))
    ts_dns = get_ts_dns_name()
    print('🚀 Taildrop Web 起動')
    print(f'   バインド  : {host}:{port}  ← Tailscale Serve 経由で公開')
    if ts_dns:
        print(f'   アクセスURL: https://{ts_dns}:{https_port}  (Tailnet内のみ)')
    print(f'   ローカルURL: http://{host}:{port}')
    print(f'   サーバファイル: {SERVER_FILE_DIR}')
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    main()
