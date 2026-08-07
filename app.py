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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=BASE_DIR)
SERVER_FILE_DIR = "/opt/lxd-data/taildrop"


def get_tailscale_ip() -> str:
    try:
        ip = subprocess.check_output(['tailscale', 'ip', '-4'], timeout=5).decode().strip()
        if ip:
            return ip.split('\n')[0]
    except Exception:
        pass
    return '0.0.0.0'


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
    items = []
    try:
        for entry in sorted(os.scandir(SERVER_FILE_DIR), key=lambda e: (not e.is_dir(), e.name.lower())):
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
    return jsonify(items)


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
    results    = []

    with tempfile.TemporaryDirectory() as tmp:
        for name in file_names:
            safe_name = os.path.basename(name)
            if not safe_name or safe_name != name:
                results.append({'ok': False, 'msg': f'⚠️ 不正なファイル名: {name}'})
                continue
            src = os.path.join(SERVER_FILE_DIR, safe_name)
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


def main():
    template_path = os.path.join(BASE_DIR, 'template.html')
    if not os.path.exists(template_path):
        print(f'[ERR] テンプレートファイルが見つかりません: {template_path}')
        raise SystemExit(1)

    os.makedirs(SERVER_FILE_DIR, exist_ok=True)

    port  = int(os.environ.get('PORT', 3349))
    ts_ip = get_tailscale_ip()
    print('🚀 Taildrop Web 起動')
    print(f'   バインド  : {ts_ip}:{port}  ← Tailscaleネットワーク内のみ')
    print(f'   アクセスURL: http://{ts_ip}:{port}')
    print(f'   サーバファイル: {SERVER_FILE_DIR}')
    app.run(host=ts_ip, port=port, debug=False)


if __name__ == '__main__':
    main()
