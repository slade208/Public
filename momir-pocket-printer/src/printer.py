"""Thermal printer interface for printing Magic: The Gathering cards.

Adapted from MoritzHayden/momir-basic-printer (MIT license) for
battery-powered ESC/POS receipt printers (PT-210 class) connected over
Bluetooth SPP (rfcomm) or USB. All GPIO/DTR flow-control code from the
original TTL-serial design has been removed - these printers handle
their own flow control.
"""

import json
import logging
import textwrap
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image

logger = logging.getLogger('momir.printer')


class Printer:
    """ESC/POS printer interface for a PT-210-class portable receipt printer.

    Supports two connection modes (set connection_mode in config.ini):
    - bluetooth: ESC/POS over a bound rfcomm serial device (/dev/rfcomm0)
    - usb: ESC/POS over USB using vendor/product IDs from lsusb
    """

    def __init__(self, printer_config, filesystem_config) -> None:
        self.connection_mode: str = printer_config.get(
            'connection_mode', fallback='bluetooth').strip().lower()
        self.serial_port: str = printer_config.get(
            'serial_port', fallback='/dev/rfcomm0')
        self.serial_baud_rate: int = printer_config.getint(
            'serial_baud_rate', fallback=9600)
        self.vendor_id: int = int(
            printer_config.get('vendor_id', fallback='0x0000'), 0)
        self.product_id: int = int(
            printer_config.get('product_id', fallback='0x0000'), 0)

        self.paper_width_chars: int = printer_config.getint(
            'paper_width_chars', fallback=32)
        self.card_art_enabled: bool = printer_config.getboolean(
            'card_art_enabled', fallback=True)
        self.qr_code_enabled: bool = printer_config.getboolean(
            'qr_code_enabled', fallback=True)
        self.qr_code_size: int = printer_config.getint(
            'qr_code_size', fallback=6)
        self.printer_profile: str = printer_config.get(
            'printer_profile', fallback='simple')
        self.printer_media_width_px: Optional[int] = printer_config.getint(
            'printer_media_width_px', fallback=None)
        self._min_title_spacing: int = printer_config.getint(
            'min_title_spacing', fallback=1)
        self._paragraph_spacing: str = printer_config.get(
            'paragraph_spacing', fallback='\\n\\n').encode('utf-8').decode('unicode_escape')

        text_replacements_json = printer_config.get(
            'text_replacements_json',
            fallback='{"\\u2014":"-","\\u2013":"-","\\u2019":"\'","\\u201c":"\\\"","\\u201d":"\\\""}',
        )
        try:
            self._text_replacements: Dict[str, str] = json.loads(
                text_replacements_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid text_replacements_json in config: {exc}") from exc

        if self.connection_mode not in ('bluetooth', 'usb'):
            raise ValueError(
                f"connection_mode must be 'bluetooth' or 'usb', got '{self.connection_mode}'")
        if self.connection_mode == 'usb' and not (self.vendor_id and self.product_id):
            raise ValueError(
                "connection_mode=usb requires vendor_id and product_id (see lsusb)")

        base_path = Path(__file__).resolve().parent.parent
        self.art_path: Path = base_path / filesystem_config.get('art_path')

    def __repr__(self) -> str:
        if self.connection_mode == 'usb':
            target = f"usb {self.vendor_id:#06x}:{self.product_id:#06x}"
        else:
            target = f"bluetooth {self.serial_port}@{self.serial_baud_rate}"
        return f"Printer({target}, profile='{self.printer_profile}')"

    def clean_text(self, text: str) -> str:
        """Replace special Unicode characters with printer-safe ASCII.

        Encodes to CP437 so python-escpos's MagicEncode never switches to a
        non-Latin codepage; characters outside CP437 become '?' instead of
        corrupting the print stream.
        """
        cleaned = text
        for old_char, new_char in self._text_replacements.items():
            cleaned = cleaned.replace(old_char, new_char)
        return cleaned.encode('cp437', errors='replace').decode('cp437')

    def _get_printer_connection(self):
        """Create and return an ESC/POS printer connection."""
        try:
            if self.connection_mode == 'usb':
                from escpos.printer import Usb
                printer = Usb(
                    idVendor=self.vendor_id,
                    idProduct=self.product_id,
                    profile=self.printer_profile,
                )
            else:
                from escpos.printer import Serial
                printer = Serial(
                    devfile=self.serial_port,
                    baudrate=self.serial_baud_rate,
                    profile=self.printer_profile,
                )

            if self.printer_media_width_px is not None:
                profile_data = printer.profile.profile_data
                media = profile_data.setdefault("media", {})
                width = media.setdefault("width", {})
                width["pixels"] = str(self.printer_media_width_px)

            return printer
        except Exception as e:
            logger.error(f"Failed to connect to printer ({self}): {e}")
            raise

    def is_available(self) -> bool:
        """Return True if the printer can be reached right now."""
        try:
            printer = self._get_printer_connection()
            try:
                printer.close()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _get_printer_max_width_px(self, printer) -> Optional[int]:
        try:
            pixels_value = printer.profile.profile_data["media"]["width"]["pixels"]
            return int(pixels_value)
        except (KeyError, TypeError, ValueError):
            return None

    def _print_card_art(self, printer, card_art_path: Path) -> None:
        max_width_px = self._get_printer_max_width_px(printer)

        if max_width_px is not None:
            with Image.open(card_art_path) as img:
                if img.width > max_width_px:
                    scale = max_width_px / float(img.width)
                    target_height = max(1, int(img.height * scale))
                    img = img.resize(
                        (max_width_px, target_height), Image.Resampling.LANCZOS)
                printer.image(img.copy())
            return

        printer.image(str(card_art_path))

    def print_wifi_ticket(self, url: str, ssid: str = '', password: str = '') -> None:
        """Print a join ticket: Wi-Fi QR code (if AP mode) and the app URL QR.

        Guests scan the first QR to join the hotspot and the second to open
        the print page - no typing required.
        """
        printer = self._get_printer_connection()
        try:
            printer.set(align='center', bold=True)
            printer.text("MOMIR POCKET PRINTER\n\n")

            if ssid:
                printer.set(align='center', bold=False)
                printer.text("1. Scan to join the Wi-Fi:\n\n")
                if password:
                    wifi_qr = f"WIFI:T:WPA;S:{ssid};P:{password};;"
                else:
                    wifi_qr = f"WIFI:T:nopass;S:{ssid};;"
                printer.qr(wifi_qr, size=self.qr_code_size)
                printer.set(align='center', bold=False)
                printer.text(f"\nNetwork: {ssid}\n")
                if password:
                    printer.text(f"Password: {password}\n")
                printer.text("\n2. Scan to open the printer:\n\n")
            else:
                printer.set(align='center', bold=False)
                printer.text("Scan to open the printer:\n\n")

            printer.qr(url, size=self.qr_code_size)
            printer.set(align='center', bold=False)
            printer.text(f"\n{url}\n")
            printer.text("\n\n\n")
            logger.info("Printed Wi-Fi join ticket")
        finally:
            try:
                printer.close()
            except Exception:
                pass

    def print_card(self, card: Dict[str, Any]) -> None:
        """Print a formatted Magic: The Gathering card.

        Layout: name + mana cost, art (optional), QR code to Scryfall
        (optional), type line, oracle text, power/toughness.
        """
        card_name = self.clean_text(card.get("name") or "Unknown Card")
        card_mana_cost = card.get("mana_cost") or ""
        card_scryfall_uri = card.get("scryfall_uri") or ""
        card_id = card.get("id")
        card_art_path: Optional[Path] = (
            self.art_path / f"{card_id}.jpg") if card_id else None
        card_type_line = self.clean_text(card.get("type_line") or "")
        card_oracle_text = self.clean_text(card.get("oracle_text") or "")
        card_power = card.get("power")
        card_toughness = card.get("toughness")

        logger.debug(f"Printing card: {card_name}...")

        printer = None
        try:
            printer = self._get_printer_connection()

            # Prefer a Latin/ASCII codepage; not all profiles support
            # explicit selection, so failures here are non-fatal.
            for codepage in ("CP437", "USA"):
                try:
                    printer.charcode(codepage)
                    break
                except Exception:
                    continue

            # NAME AND MANA COST
            printer.set(align='left', bold=True)
            combined_len = len(card_name) + \
                self._min_title_spacing + len(card_mana_cost)
            if combined_len > self.paper_width_chars:
                wrapped_name = textwrap.fill(
                    card_name, width=self.paper_width_chars, break_long_words=True)
                printer.text(f"{wrapped_name}\n")
                if card_mana_cost:
                    printer.set(align='right', bold=True)
                    printer.text(f"{card_mana_cost}\n")
            else:
                title_line_spaces = max(
                    self._min_title_spacing,
                    self.paper_width_chars -
                    (len(card_name) + len(card_mana_cost))
                )
                printer.text(
                    f"{card_name}{' ' * title_line_spaces}{card_mana_cost}\n")

            # ART
            printer.set(align='center', bold=False)
            if self.card_art_enabled and card_art_path and card_art_path.exists():
                printer.text("\n")
                self._print_card_art(printer, card_art_path)
                printer.text("\n")

            # QR CODE
            printer.set(align='center', bold=False)
            if self.qr_code_enabled and card_scryfall_uri:
                printer.qr(card_scryfall_uri, size=self.qr_code_size)

            # TYPE LINE
            if card_type_line:
                if len(card_type_line) > self.paper_width_chars and "-" in card_type_line:
                    type_prefix, type_suffix = card_type_line.split("-", 1)
                    type_prefix = type_prefix.rstrip()
                    type_suffix = type_suffix.lstrip()
                    if type_prefix and type_suffix:
                        printer.text(f"{type_prefix}\n{type_suffix}\n\n")
                    else:
                        printer.text(f"{card_type_line}\n\n")
                else:
                    printer.text(f"{card_type_line}\n\n")

            # ORACLE TEXT
            if card_oracle_text:
                printer.set(align='left', bold=False)
                for paragraph in card_oracle_text.split('\n'):
                    if paragraph.strip():
                        wrapped = textwrap.fill(
                            paragraph, width=self.paper_width_chars)
                        printer.text(wrapped + self._paragraph_spacing)
                    else:
                        printer.text("\n")

            # POWER / TOUGHNESS
            if card_power is not None and card_toughness is not None:
                printer.set(align='right', bold=False)
                printer.text(f"{card_power} / {card_toughness}\n")

            # SET
            card_set_name = self.clean_text(card.get("set_name") or "")
            if card_set_name:
                printer.set(align='center', bold=False)
                printer.text(f"{card_set_name}\n")

            # Feed past the tear bar; PT-210s have no cutter.
            printer.text("\n\n\n")
            logger.info(f"Successfully printed: {card_name}")

        except Exception as e:
            logger.error(f"Failed to print card {card_name}: {e}")
            raise
        finally:
            if printer is not None:
                try:
                    printer.close()
                except Exception:
                    pass
