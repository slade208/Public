# Momir Pocket Printer

A zero-solder, fully portable [Momir Basic](https://magic.wizards.com/en/formats/momir-basic) card printer:
tap a mana value on your phone, and a random Magic: The Gathering creature
with that mana value prints on a battery-powered thermal receipt printer.

Adapted from [MoritzHayden/momir-basic-printer](https://github.com/MoritzHayden/momir-basic-printer)
(MIT license). The original uses a TTL-serial printer module, OLED display,
and rotary encoder wired through a breadboard; this version replaces all of
that with a PT-210 Bluetooth receipt printer and a phone web UI, so there is
no GPIO, no breadboard, and no soldering.

## Hardware

- Raspberry Pi 4 (or any Pi with Bluetooth/USB and network)
- GOOJPRT PT-210 (or clone) 58mm portable thermal printer — ESC/POS over
  Bluetooth, built-in battery
- USB power bank for the Pi
- 57x30mm thermal paper
- Your phone (the UI is a web page served by the Pi)

## Setup

1. Flash Raspberry Pi OS Lite, boot the Pi, clone this repo onto it.

2. Pair the printer (one time):

   ```shell
   bluetoothctl
   scan on           # wait for the PT-210 to appear, note its MAC
   pair <MAC>        # PIN is usually 0000 or 1234
   trust <MAC>
   quit
   ```

3. Put the printer's MAC in `src/config.ini` under `[PRINTER] bluetooth_mac`.

4. Run setup (installs deps, binds the printer to `/dev/rfcomm0` at boot,
   installs a systemd service that starts the app on boot):

   ```shell
   cd momir-pocket-printer
   chmod +x setup.sh
   sudo ./setup.sh
   ```

5. Open `http://<pi-address>:8080` on your phone. The first boot downloads
   the Scryfall card database (creature JSON + dithered art), which takes a
   while; the page shows progress. After that, updates are incremental.

### Hotspot mode (play anywhere, let friends print)

The Pi can be its own Wi-Fi access point so nobody needs your home network:
anyone at the table joins the Pi's Wi-Fi and gets the print page.

1. Do the first-time setup above **on your home network first** - the card
   database download needs internet, and in hotspot mode the Pi's Wi-Fi has
   none.
2. In `src/config.ini`, set:

   ```ini
   [WIFI]
   ap_enabled = True
   ap_ssid = MomirPrinter
   ap_password = <8-63 characters>
   ```

3. Re-run `sudo ./setup.sh`. Heads up: if you're SSHed in over Wi-Fi, your
   session drops when the hotspot comes up.
4. Join the `MomirPrinter` network and open `http://10.42.0.1:8080`.
5. Tap **Print Wi-Fi join ticket** in the app: it prints a receipt with two
   QR codes - scan to join the Wi-Fi, scan to open the page. Hand it to the
   table.

While in hotspot mode the app skips card database refreshes and plays from
local data (plug in Ethernet if you want updates without leaving AP mode).
To go back to normal Wi-Fi, set `ap_enabled = False` and re-run setup.

### Controlling who can print

With `[ACCESS] access_control_enabled = True` (the default), new devices
can't print until the host approves them:

1. **Change `admin_pin` in `src/config.ini`** before game night.
2. On your own phone, tap **I'm the host** and enter the PIN. Your device
   becomes the host and gets a Players panel.
3. Friends open the page, enter their name, and tap **Ask to join**. They
   sit in a waiting screen until you tap **Approve** next to their name.
4. Tap **Kick** any time to revoke someone; they can re-request.

Approvals are remembered across restarts (in `devices.json`), so regulars
only ask once. Set `access_control_enabled = False` for a fully open
printer.

### USB mode

If your PT-210's USB port does data (many clones do), you can use a cable
instead of Bluetooth: set `connection_mode = usb` in `src/config.ini`, fill
in `vendor_id`/`product_id` from `lsusb`, and re-run `sudo ./setup.sh`.

## Service management

```shell
sudo journalctl -u momir-pocket-printer.service -f   # live logs
sudo systemctl restart momir-pocket-printer.service  # after config changes
sudo systemctl status momir-rfcomm.service           # bluetooth binding
```

## Momir Basic rules

- 2 players, 24 starting life, 60+ basic lands as your deck
- Each turn you may discard a land and pay X: get a random creature with
  mana value X (that's the print button)

## Disclaimer

Not associated with Hasbro, Wizards of the Coast, or Magic: The Gathering.
Card data and art come from [Scryfall](https://scryfall.com/docs/api).
