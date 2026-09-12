"""Momir Pocket Printer - phone-controlled Momir Basic card printer.

A Flask web app that replaces the OLED + rotary encoder UI of the original
momir-basic-printer (MoritzHayden, MIT license) with a mobile web page.
Open the Pi's address on your phone, tap a mana value, and a random
creature with that CMC prints on the thermal printer.
"""

import configparser
import logging
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from printer import Printer
from scryfall import Scryfall

# Configuration
config = configparser.ConfigParser()
config_file = Path(__file__).resolve().with_name('config.ini')
if not config_file.exists():
    raise FileNotFoundError(f"Configuration file not found: {config_file}")
config.read(config_file, encoding='utf-8')

required_sections = ['APP', 'FILESYSTEM', 'LOGGING', 'PRINTER', 'SCRYFALL']
missing_sections = [s for s in required_sections if s not in config]
if missing_sections:
    raise ValueError(
        f"Missing required configuration sections: {', '.join(missing_sections)}")

app_config = config['APP']
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


def _startup_refresh() -> None:
    """Refresh the local Scryfall card database in the background on boot."""
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
            _set_state('ready', 'Using existing card data (refresh failed).')
        else:
            _set_state('error', f'Card database download failed: {e}')
    finally:
        with _state_lock:
            _state['refresh_done'] = True


@app.route('/')
def index():
    return render_template('index.html', cmc_min=CMC_MIN, cmc_max=CMC_MAX)


@app.route('/status')
def status():
    with _state_lock:
        payload = dict(_state)
    return jsonify(payload)


@app.route('/print', methods=['POST'])
def print_random_card():
    data = request.get_json(silent=True) or {}
    try:
        cmc = int(data.get('cmc'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'cmc must be an integer'}), 400

    if not (CMC_MIN <= cmc <= CMC_MAX):
        return jsonify({'ok': False,
                        'error': f'cmc must be between {CMC_MIN} and {CMC_MAX}'}), 400

    with _state_lock:
        if not _state['refresh_done'] and scryfall.get_total_card_count() == 0:
            return jsonify({'ok': False,
                            'error': 'Card database is still downloading'}), 503

    if not _print_lock.acquire(blocking=False):
        return jsonify({'ok': False, 'error': 'Already printing'}), 409

    try:
        card = scryfall.get_random_card_by_cmc(cmc)
        if card is None:
            return jsonify({'ok': False,
                            'error': f'No creatures exist with mana value {cmc}'}), 404

        _set_state('printing', card.get('name', ''))
        printer.print_card(card)

        with _state_lock:
            _state['last_card'] = {
                'name': card.get('name'),
                'type_line': card.get('type_line'),
                'cmc': cmc,
                'scryfall_uri': card.get('scryfall_uri'),
            }
        _set_state('ready', '')
        return jsonify({'ok': True, 'card': {
            'name': card.get('name'),
            'type_line': card.get('type_line'),
            'power': card.get('power'),
            'toughness': card.get('toughness'),
        }})
    except Exception as e:
        logger.error(f"Print failed: {e}")
        _set_state('ready', '')
        return jsonify({'ok': False,
                        'error': 'Printer error - is it on and paired?'}), 502
    finally:
        _print_lock.release()


def main() -> None:
    threading.Thread(target=_startup_refresh, daemon=True).start()
    host = app_config.get('listen_host', fallback='0.0.0.0')
    port = app_config.getint('listen_port', fallback=8080)
    logger.info(f"Momir Pocket Printer listening on http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)


if __name__ == '__main__':
    main()
