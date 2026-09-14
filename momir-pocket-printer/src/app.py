"""Momir Pocket Printer - phone-controlled Momir Basic card printer.

A Flask web app that replaces the OLED + rotary encoder UI of the original
momir-basic-printer (MoritzHayden, MIT license) with a mobile web page.
Open the Pi's address on your phone, tap a mana value, and a random
creature with that CMC prints on the thermal printer.
"""

import configparser
import json
import logging
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import re

from flask import (Flask, abort, jsonify, make_response, render_template,
                   request, send_from_directory)

from printer import Printer
from scryfall import Scryfall

# Configuration
config = configparser.ConfigParser()
config_file = Path(__file__).resolve().with_name('config.ini')
if not config_file.exists():
    raise FileNotFoundError(f"Configuration file not found: {config_file}")
# config.local.ini (gitignored) overrides config.ini - put device-specific
# values there (bluetooth_mac, admin_pin, Wi-Fi passwords) so git pull
# never conflicts with local edits.
local_config_file = config_file.with_name('config.local.ini')
config.read([config_file, local_config_file], encoding='utf-8')

required_sections = ['APP', 'FILESYSTEM', 'LOGGING', 'PRINTER', 'SCRYFALL']
missing_sections = [s for s in required_sections if s not in config]
if missing_sections:
    raise ValueError(
        f"Missing required configuration sections: {', '.join(missing_sections)}")

app_config = config['APP']
wifi_config = config['WIFI'] if 'WIFI' in config else None
access_config = config['ACCESS'] if 'ACCESS' in config else None
filesystem_config = config['FILESYSTEM']
logging_config = config['LOGGING']
printer_config = config['PRINTER']
scryfall_config = config['SCRYFALL']

logging.basicConfig(
    level=logging_config.get('log_level', fallback='INFO').upper(),
    format=logging_config.get(
        'log_format', fallback='[%(levelname)s] [%(asctime)s] %(name)s: %(message)s'),
    datefmt=logging_config.get('log_date_format', fallback='%Y-%m-%d %H:%M:%S'),
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger('momir.app')

CMC_MIN = app_config.getint('cmc_min', fallback=0)
CMC_MAX = app_config.getint('cmc_max', fallback=16)

app = Flask(__name__)

scryfall = Scryfall(scryfall_config, filesystem_config)
printer = Printer(printer_config, filesystem_config)

# Shared state, guarded by _state_lock
_state_lock = threading.Lock()
_state = {
    'status': 'starting',       # starting | refreshing | ready | printing | error
    'detail': 'Booting...',
    'last_card': None,
    'refresh_done': False,
}
_print_lock = threading.Lock()


def _set_state(status: str, detail: str = '') -> None:
    with _state_lock:
        _state['status'] = status
        _state['detail'] = detail


# ===== Device access control =====
# Devices are identified by a random cookie. New devices register as
# 'pending' and can print only after the host (unlocked via admin_pin)
# approves them. The device list persists across restarts.

ACCESS_ENABLED = (access_config is not None
                  and access_config.getboolean('access_control_enabled', fallback=True))
ADMIN_PIN = access_config.get('admin_pin', fallback='') if access_config else ''
DEVICE_COOKIE = 'momir_device'

_base_path = Path(__file__).resolve().parent.parent
_devices_path = _base_path / (access_config.get('devices_path', fallback='./devices.json')
                              if access_config else './devices.json')
_devices_lock = threading.Lock()

if ACCESS_ENABLED and (len(ADMIN_PIN) < 4 or len(ADMIN_PIN) > 32):
    raise ValueError("admin_pin must be 4-32 characters when access control is enabled")


def _load_devices() -> dict:
    try:
        with open(_devices_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_devices = _load_devices()


def _save_devices() -> None:
    with open(_devices_path, 'w', encoding='utf-8') as f:
        json.dump(_devices, f, indent=2)


def _get_device_id():
    return request.cookies.get(DEVICE_COOKIE)


def _get_device(device_id):
    if not device_id:
        return None
    with _devices_lock:
        return _devices.get(device_id)


def _device_role(device) -> str:
    """Role for this request: host | approved | pending | denied | new."""
    if device is None:
        return 'new'
    return device.get('status', 'pending')


def _require(*roles):
    """Return an error response if the requesting device lacks the role, else None."""
    if not ACCESS_ENABLED:
        return None
    role = _device_role(_get_device(_get_device_id()))
    if role in roles:
        return None
    if role in ('pending', 'denied'):
        return jsonify({'ok': False, 'error': 'Waiting for host approval'}), 403
    return jsonify({'ok': False, 'error': 'Register this device first'}), 401


@app.route('/me')
def me():
    device_id = _get_device_id()
    device = _get_device(device_id)
    payload = {
        'access_control': ACCESS_ENABLED,
        'role': 'approved' if not ACCESS_ENABLED else _device_role(device),
        'name': (device or {}).get('name', ''),
    }
    resp = make_response(jsonify(payload))
    if not device_id:
        resp.set_cookie(DEVICE_COOKIE, secrets.token_urlsafe(16),
                        max_age=365 * 24 * 3600, samesite='Lax')
    return resp


@app.route('/register', methods=['POST'])
def register():
    device_id = _get_device_id()
    if not device_id:
        return jsonify({'ok': False, 'error': 'No device cookie; reload the page'}), 400
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or 'Player').strip()[:24] or 'Player'
    with _devices_lock:
        existing = _devices.get(device_id)
        if existing is None or existing.get('status') == 'denied':
            _devices[device_id] = {'name': name, 'status': 'pending',
                                   'first_seen': time.time()}
        else:
            existing['name'] = name
        _save_devices()
    logger.info(f"Device registered: {name}")
    return jsonify({'ok': True})


@app.route('/host/login', methods=['POST'])
def host_login():
    device_id = _get_device_id()
    if not device_id:
        return jsonify({'ok': False, 'error': 'No device cookie; reload the page'}), 400
    data = request.get_json(silent=True) or {}
    pin = str(data.get('pin') or '')
    if not secrets.compare_digest(pin, ADMIN_PIN):
        time.sleep(1)  # slow down PIN guessing
        return jsonify({'ok': False, 'error': 'Wrong PIN'}), 403
    data_name = str(data.get('name') or 'Host').strip()[:24] or 'Host'
    with _devices_lock:
        _devices[device_id] = {'name': data_name, 'status': 'host',
                               'first_seen': time.time()}
        _save_devices()
    logger.info("Host device unlocked")
    return jsonify({'ok': True})


@app.route('/host/devices')
def host_devices():
    err = _require('host')
    if err:
        return err
    with _devices_lock:
        listing = [
            {'id': did, 'name': d.get('name', 'Player'), 'status': d.get('status')}
            for did, d in sorted(_devices.items(),
                                 key=lambda kv: kv[1].get('first_seen', 0))
        ]
    return jsonify({'ok': True, 'devices': listing})


@app.route('/host/set-status', methods=['POST'])
def host_set_status():
    err = _require('host')
    if err:
        return err
    data = request.get_json(silent=True) or {}
    target_id = data.get('id')
    new_status = data.get('status')
    if new_status not in ('approved', 'denied'):
        return jsonify({'ok': False, 'error': 'status must be approved or denied'}), 400
    with _devices_lock:
        target = _devices.get(target_id)
        if target is None:
            return jsonify({'ok': False, 'error': 'Unknown device'}), 404
        if target.get('status') == 'host':
            return jsonify({'ok': False, 'error': 'Cannot change the host device'}), 400
        target['status'] = new_status
        _save_devices()
    logger.info(f"Device '{target.get('name')}' set to {new_status}")
    return jsonify({'ok': True})


_refresh_lock = threading.Lock()


def _run_refresh() -> None:
    """Refresh the local Scryfall card database (runs in a worker thread)."""
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        if scryfall.get_total_card_count() == 0:
            _set_state('refreshing',
                       'First run: downloading card database from Scryfall. '
                       'This takes a while - leave it plugged in.')
        else:
            _set_state('refreshing', 'Checking Scryfall for card updates...')
        scryfall.refresh_card_data()
        _set_state('ready', '')
        logger.info("Card data ready (%d cards).",
                    scryfall.get_total_card_count())
    except Exception as e:
        logger.error(f"Card data refresh failed: {e}")
        # Stale data is still playable; only hard-fail with an empty library.
        if scryfall.get_total_card_count() > 0:
            _set_state('ready', 'Using existing card data (refresh failed - '
                                'no internet in hotspot mode is normal).')
        else:
            _set_state('error', f'Card database download failed: {e}')
    finally:
        with _state_lock:
            _state['refresh_done'] = True
        _refresh_lock.release()


# ===== Hotspot control =====
# The hotspot is switched by a root-owned helper (installed by setup.sh)
# that sudoers allows this app's user to run without a password. State is
# read live from NetworkManager rather than from config.

HOTSPOT_HELPER = '/usr/local/bin/momir-hotspot'


def _hotspot_active():
    """Return True/False if hotspot state can be determined, else None."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME', 'con', 'show', '--active'],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        return 'momir-ap' in result.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None


def _apply_hotspot(enabled: bool) -> None:
    """Toggle the hotspot after a short delay so the HTTP response escapes
    the network before it changes underneath the client."""
    time.sleep(1.5)
    action = 'on' if enabled else 'off'
    try:
        result = subprocess.run(
            ['sudo', '-n', HOTSPOT_HELPER, action],
            capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            logger.info(f"Hotspot switched {action}")
        else:
            logger.error(
                f"Hotspot {action} failed: {result.stderr.strip() or result.stdout.strip()}")
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"Hotspot {action} failed: {e}")


@app.route('/')
def index():
    return render_template('index.html', cmc_min=CMC_MIN, cmc_max=CMC_MAX)


@app.route('/status')
def status():
    with _state_lock:
        payload = dict(_state)
    payload['printer'] = printer.is_connected()
    return jsonify(payload)


@app.route('/host/update-cards', methods=['POST'])
def host_update_cards():
    err = _require('host')
    if err:
        return err
    if _refresh_lock.locked():
        return jsonify({'ok': False, 'error': 'An update is already running'}), 409
    threading.Thread(target=_run_refresh, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/host/hotspot')
def host_hotspot_status():
    err = _require('host')
    if err:
        return err
    active = _hotspot_active()
    ssid = wifi_config.get('ap_ssid', fallback='') if wifi_config else ''
    return jsonify({'ok': True, 'supported': active is not None,
                    'active': bool(active), 'ssid': ssid})


@app.route('/host/hotspot', methods=['POST'])
def host_hotspot_toggle():
    err = _require('host')
    if err:
        return err
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        return jsonify({'ok': False, 'error': 'enabled must be true or false'}), 400
    threading.Thread(target=_apply_hotspot, args=(enabled,), daemon=True).start()
    return jsonify({'ok': True, 'applying': 'on' if enabled else 'off'})


def _apply_wifi_join(ssid: str, password: str) -> None:
    """Store home Wi-Fi credentials after a short delay (same reason as the
    hotspot toggle: let the HTTP response out before any network change)."""
    time.sleep(1.5)
    try:
        result = subprocess.run(
            ['sudo', '-n', HOTSPOT_HELPER, 'wifi-join', ssid, password],
            capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            logger.info(f"Home Wi-Fi set to '{ssid}': {result.stdout.strip()}")
        else:
            logger.error(
                f"Wi-Fi join failed: {result.stderr.strip() or result.stdout.strip()}")
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"Wi-Fi join failed: {e}")


@app.route('/host/wifi', methods=['POST'])
def host_set_wifi():
    err = _require('host')
    if err:
        return err
    data = request.get_json(silent=True) or {}
    ssid = str(data.get('ssid') or '').strip()
    password = str(data.get('password') or '')
    if not ssid or len(ssid) > 32:
        return jsonify({'ok': False, 'error': 'SSID must be 1-32 characters'}), 400
    if password and not (8 <= len(password) <= 63):
        return jsonify({'ok': False,
                        'error': 'Password must be 8-63 characters (or empty for an open network)'}), 400
    threading.Thread(target=_apply_wifi_join, args=(ssid, password),
                     daemon=True).start()
    return jsonify({'ok': True})


def _card_payload(card):
    """Displayable card fields for the web UI."""
    card_id = card.get('id')
    art_url = None
    if card_id and (scryfall.art_path / f"{card_id}.jpg").exists():
        art_url = f"/art/{card_id}"
    return {
        'name': card.get('name'),
        'mana_cost': card.get('mana_cost'),
        'type_line': card.get('type_line'),
        'oracle_text': card.get('oracle_text'),
        'power': card.get('power'),
        'toughness': card.get('toughness'),
        'scryfall_uri': card.get('scryfall_uri'),
        'set_name': card.get('set_name'),
        'set': (card.get('set') or '').upper(),
        'art_url': art_url,
    }


def _draw_card(data):
    """Validate a cmc request and pick a random creature.

    Returns (card, None) on success or (None, error_response) on failure.
    """
    try:
        cmc = int(data.get('cmc'))
    except (TypeError, ValueError):
        return None, (jsonify({'ok': False, 'error': 'cmc must be an integer'}), 400)

    if not (CMC_MIN <= cmc <= CMC_MAX):
        return None, (jsonify({'ok': False,
                               'error': f'cmc must be between {CMC_MIN} and {CMC_MAX}'}), 400)

    with _state_lock:
        if not _state['refresh_done'] and scryfall.get_total_card_count() == 0:
            return None, (jsonify({'ok': False,
                                   'error': 'Card database is still downloading'}), 503)

    card = scryfall.get_random_card_by_cmc(cmc)
    if card is None:
        return None, (jsonify({'ok': False,
                               'error': f'No creatures exist with mana value {cmc}'}), 404)

    with _state_lock:
        _state['last_card'] = {
            'name': card.get('name'),
            'type_line': card.get('type_line'),
            'cmc': cmc,
            'scryfall_uri': card.get('scryfall_uri'),
        }
    return card, None


@app.route('/art/<card_id>')
def card_art(card_id):
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', card_id):
        abort(404)
    return send_from_directory(scryfall.art_path, f"{card_id}.jpg",
                               max_age=86400)


@app.route('/draw', methods=['POST'])
def draw_random_card():
    """Pick a random creature and return it for on-screen display (no print)."""
    err = _require('host', 'approved')
    if err:
        return err
    card, fail = _draw_card(request.get_json(silent=True) or {})
    if fail:
        return fail
    logger.info(f"Drew (screen only): {card.get('name')}")
    return jsonify({'ok': True, 'card': _card_payload(card)})


@app.route('/print-qr', methods=['POST'])
def print_qr_slip():
    """Print a standalone Scryfall QR slip for the most recent creature."""
    err = _require('host', 'approved')
    if err:
        return err
    with _state_lock:
        last = dict(_state['last_card']) if _state['last_card'] else None
    if not last or not last.get('scryfall_uri'):
        return jsonify({'ok': False, 'error': 'Nothing summoned yet'}), 404
    if not _print_lock.acquire(blocking=False):
        return jsonify({'ok': False, 'error': 'Already printing'}), 409
    try:
        printer.print_qr(last.get('name') or 'Card', last['scryfall_uri'])
        return jsonify({'ok': True})
    except Exception as e:
        logger.error(f"QR print failed: {e}")
        return jsonify({'ok': False,
                        'error': 'Printer error - is it on and paired?'}), 502
    finally:
        _print_lock.release()


@app.route('/print', methods=['POST'])
def print_random_card():
    err = _require('host', 'approved')
    if err:
        return err

    if not _print_lock.acquire(blocking=False):
        return jsonify({'ok': False, 'error': 'Already printing'}), 409

    try:
        card, fail = _draw_card(request.get_json(silent=True) or {})
        if fail:
            return fail

        _set_state('printing', card.get('name', ''))
        try:
            printer.print_card(card)
        except Exception as e:
            # The summon still resolved - hand the card back for the screen.
            logger.error(f"Print failed: {e}")
            _set_state('ready', '')
            return jsonify({'ok': False,
                            'error': 'Printer unavailable - card shown on screen instead',
                            'card': _card_payload(card)}), 502

        _set_state('ready', '')
        return jsonify({'ok': True, 'card': _card_payload(card)})
    finally:
        _print_lock.release()


@app.route('/print-ticket', methods=['POST'])
def print_wifi_ticket():
    """Print a receipt with QR codes for joining the hotspot and opening this page."""
    err = _require('host', 'approved')
    if err:
        return err
    ssid = ''
    password = ''
    # Include Wi-Fi join info when the hotspot is actually active right now
    # (falling back to the config flag when NetworkManager can't be queried).
    active = _hotspot_active()
    hotspot_on = (active if active is not None else
                  (wifi_config is not None
                   and wifi_config.getboolean('ap_enabled', fallback=False)))
    if hotspot_on and wifi_config is not None:
        ssid = wifi_config.get('ap_ssid', fallback='')
        password = wifi_config.get('ap_password', fallback='')

    # In AP mode clients reach us at the shared-mode gateway address; either
    # way the address the requester used is the address that works.
    url = request.host_url.rstrip('/')

    if not _print_lock.acquire(blocking=False):
        return jsonify({'ok': False, 'error': 'Already printing'}), 409
    try:
        printer.print_wifi_ticket(url, ssid=ssid, password=password)
        return jsonify({'ok': True})
    except Exception as e:
        logger.error(f"Ticket print failed: {e}")
        return jsonify({'ok': False,
                        'error': 'Printer error - is it on and paired?'}), 502
    finally:
        _print_lock.release()


def main() -> None:
    threading.Thread(target=_run_refresh, daemon=True).start()
    host = app_config.get('listen_host', fallback='0.0.0.0')
    port = app_config.getint('listen_port', fallback=8080)
    logger.info(f"Momir Pocket Printer listening on http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)


if __name__ == '__main__':
    main()
