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

- Raspberry Pi Zero 2 W (the reference build; any Pi with Wi-Fi and
  Bluetooth works — a Pi 3/4 is fine, just hungrier on the power bank)
- GOOJPRT PT-210 (or clone) 58mm portable thermal printer — ESC/POS over
  Bluetooth, built-in battery
- USB power bank for the Pi (micro-USB cable for a Zero)
- 32GB+ microSD card
- 57x30mm thermal paper
- Your phone (the UI is a web page served by the Pi)

No printer handy? The app also works fully on-screen: a per-device toggle
switches between printing cards and displaying them in the browser.

**Printed cards are sleeve-sized by design:** 58mm paper fits a standard
card sleeve, and the default print is compact - name and cost, a small
Scryfall QR (scan for art and details), type, rules text, P/T. Card art
and set name show in the web app instead of on paper (`card_art_enabled`
/ `print_set_enabled` / `qr_code_enabled` / `qr_code_size` in config turn
things back on or resize them). The card overlay also has a **View on
Scryfall** link and a **Show QR** button that displays the QR on the
phone screen so others at the table can scan it - useful in screen-only
mode with no printer.

Tore a print wrong on the cutter? A **Reprint last card** button appears
under the grid once you've summoned; it reprints *your* last creature
(the Pi remembers one card per device, so someone else's summon can't
clobber yours). The page footer also shows how current the offline card
database is - "Cards through \<newest set\> - updated \<date\>" - so you
know at a glance whether it's time to update after a set release.

## Setup

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In the
   customization step set hostname `momir`, enable SSH, set your user, and
   enter your home Wi-Fi (2.4GHz). Boot, then `ssh <you>@momir.local` and
   clone this repo (Lite does not ship with git):

   ```shell
   sudo apt update && sudo apt install -y git
   git clone https://github.com/slade208/Public.git
   cd Public/momir-pocket-printer
   ```

2. Prep the printer (one time): take the battery out and **peel the
   insulating sticker off its contacts** (shipped that way; the printer
   is dead until you do). Load paper so it unwinds from under the roll
   toward you - if prints come out blank, the roll is backwards
   (fingernail-scratch the paper: the side that streaks dark is the
   thermal side and must face the lid). Then verify the hardware with
   the offline self-test: printer off, hold FEED, press POWER, release
   on the beep - a test page prints with no software involved.

   Video walkthroughs for the PT-210:
   [loading paper](https://manuals.plus/video/ebd15f59e4ef119ab339263144f1339a8536d25e9730e74b8a99e207d49a1c67),
   [battery + paper + setup](https://manuals.plus/video/33cd5539358247481431167d38556ad083032d24d07fd54c07453d7a101f72b5),
   [general setup and printing](https://manuals.plus/video/257c7244153a61e789292df33e14227bd3a49205dea957eb3836fb90add13ee7).

3. Pair the printer (one time). The device advertises as `PT210_xxxx`,
   `MTP-II`, or `PT200`, and you must pair **while the scan is still
   running** (BlueZ flushes unpaired devices when scanning stops):

   ```shell
   bluetoothctl
   power on          # if the adapter says NotReady
   scan on           # wait for the printer to appear, note its MAC
   pair <MAC>        # while scanning; PIN is 0000
   trust <MAC>
   quit
   ```

4. Create `src/config.local.ini` with your device-specific values (this
   file is gitignored and overrides `config.ini`, so `git pull` never
   conflicts with it - don't edit `config.ini` itself):

   ```ini
   [PRINTER]
   bluetooth_mac = AA:BB:CC:DD:EE:FF

   [ACCESS]
   admin_pin = your-pin

   [WIFI]
   ap_password = your-hotspot-password
   ```

5. Run setup (installs deps, binds the printer to `/dev/rfcomm0` at boot,
   installs the app service, the `momir` CLI, the login banner, and the
   rescue-hotspot watchdog):

   ```shell
   chmod +x setup.sh
   sudo ./setup.sh
   ```

6. Open `http://momir.local:8080` on your phone (or the Pi's IP). After
   a print, check the printer status line on the page ("Printer:
   connected/offline"), or `momir status` from a shell.

   **The first run downloads the whole card database - plan for it.** It
   pulls ~17,000 creatures' data and art from Scryfall (a few hundred MB,
   with polite rate-limiting on every image). On a Pi Zero over Wi-Fi
   expect **a few hours**; a Pi 4 on good Wi-Fi is faster. Keep the Pi on
   wall power for it. The web page shows "downloading card database"
   status the whole time, and printing works as soon as it finishes. Ways
   to watch from a terminal:

   ```shell
   momir logs                                    # live progress, a line per 1000 cards
   du -sh ~/Public/momir-pocket-printer/cards    # watch the size grow
   ```

   Tip: you can run the download *before* your printer arrives - do
   steps 1 and 5 (without a printer MAC, setup installs everything
   except the printer link and ends with pairing instructions - the web
   app is already up), then run `momir update` inside `tmux` and let it
   finish overnight. If the download dies partway, just run it again -
   it resumes where it left off. After the first sync, updates are
   incremental and take minutes.

### Hotspot mode (play anywhere, let friends print)

The Pi can be its own Wi-Fi access point so nobody needs your home network:
anyone at the table joins the Pi's Wi-Fi and gets the print page.

1. Do the first-time setup above **on your home network first** - the card
   database download needs internet, and in hotspot mode the Pi's Wi-Fi has
   none.
2. In `src/config.local.ini`, set:

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

While in hotspot mode the app skips card database refreshes and plays
from local data - once the database is downloaded, gameplay (draws,
prints, art, on-screen QRs) needs no internet at all, so a Scryfall
outage or a basement game store changes nothing. Phones connected to
the hotspot keep their own internet via cellular data automatically
(Android may ask "stay connected?" - say yes), which is how the
Scryfall QR links still open at the table. **After the first setup you never need SSH for mode switching:**
the host panel in the web app has a `Hotspot: on/off` toggle that flips the
Pi between hotspot and home Wi-Fi (heads-up prompts tell you where to
reconnect). `ap_enabled` in config just controls the state after running
setup.sh; day to day, use the toggle.

## What's in the creature pool

One entry per unique creature name (Scryfall `oracle_cards` - multiple
printings don't increase a card's odds), filtered to match how Momir
works officially:

- **In:** every paper-legal, black-border creature in Magic's history -
  regular sets, Commander products, Modern Horizons-style sets, promos
- **Out:** Un-sets and other `funny` sets (Unglued, Unhinged, Unstable,
  Unsanctioned, Unfinity, Mystery Booster playtest cards), gold-border
  memorabilia, Arena-only/Alchemy cards, and non-creature layouts
  (tokens, emblems, schemes, vanguards, ...)

The filters live in `src/config.ini` under `[SCRYFALL]` (`excluded_sets`
by Scryfall set type, `excluded_layouts`). If you loosen them, run
`momir update` to pull in the newly eligible cards. Rules note: Momir
summons are token copies - print-outs represent tokens, and anything
that cares about tokens vs. cards should treat them as tokens.

## Updating for new sets

The app checks Scryfall on every service start and syncs incrementally
(only new cards download - minutes, not hours). When a new set drops:
be in home mode (internet), then either tap **Update cards** on the host
panel or restart the service / power-cycle the Pi. In hotspot mode the
check is skipped gracefully and play continues on local data.

To confirm an update landed, check the "Cards through \<set\>" line at
the bottom of the web page (or the card-library line in the SSH banner) -
it names the newest set in the local database and when it was last
synced.

You don't have to track release dates yourself. Whenever the Pi is
online (home mode) it quietly asks Scryfall for its set list - shortly
after boot, roughly twice a day, and as soon as it's back online after
a hotspot/field session - and when a set newer than the local database
is out, everyone's page shows an amber "New set on Scryfall: ... - tap
Update cards" notice until the host updates.

Spoiler-season cards count: Scryfall doesn't mark new cards as
paper-playable until release day, but the app admits cards from
not-yet-released sets anyway, so a fully spoiled set is playable before
its release weekend (sets releasing more than ~3 weeks out are ignored,
since early previews are incomplete). Turn this off with
`include_spoilers = False` under `[SCRYFALL]` if you want strictly
released cards only.

### Controlling who can print

With `[ACCESS] access_control_enabled = True` (the default), new devices
can't print until the host approves them:

1. **Set `admin_pin` in `src/config.local.ini`** before game night.
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
instead of Bluetooth: set `connection_mode = usb` in
`src/config.local.ini`, fill in `vendor_id`/`product_id` from `lsusb`,
and re-run `sudo ./setup.sh`.

## Day-to-day admin

Setup installs a `momir` command and a login banner: every SSH login shows
live status (services, network mode, card count) plus any warnings
(default PIN, unpaired printer) and the command cheat sheet. The commands:

```shell
momir update           # fetch new cards after a set release (home mode)
momir update full      # re-download everything (hours; after art-setting changes)
momir pull             # git pull latest code + restart the service
momir hotspot on|off   # switch network mode (also on the web host panel)
momir wifi <ssid> [pw] # set the home Wi-Fi network (also on the host panel)
momir logs             # follow live logs
momir status           # service + hotspot status
```

### Stranded somewhere with no known Wi-Fi? (rescue hotspot)

If the Pi boots in home mode somewhere its home network doesn't exist, a
watchdog notices there's no Wi-Fi connection after ~4 minutes and raises
the hotspot automatically. So the recovery is: plug it in, wait a few
minutes, join the hotspot SSID, and use **Home Wi-Fi** / the hotspot
toggle as usual. The rescue hotspot is temporary - it doesn't change the
saved mode, so a reboot back home reconnects to home Wi-Fi normally.

### Moving to a new home network (no Linux needed)

The host panel has a **Home Wi-Fi** button: turn the hotspot on (or if it
already is, just join it), open the page as host, tap Home Wi-Fi, type the
new network's name and password, then toggle the hotspot off - the Pi
joins the new network. This is how a non-technical owner points their unit
at their own Wi-Fi: everything happens from the phone.

## Service management

```shell
sudo journalctl -u momir-pocket-printer.service -f   # live logs
sudo systemctl restart momir-pocket-printer.service  # after config changes
sudo systemctl status momir-rfcomm.service           # bluetooth binding
```

## Troubleshooting

**Wi-Fi stalls or timeouts (especially Pi Zero):** the Zero's Wi-Fi
power-saving mode is notorious for causing intermittent read timeouts and
dropped connections. `setup.sh` disables it system-wide (it writes
`/etc/NetworkManager/conf.d/momir-wifi-powersave.conf` with
`wifi.powersave = 2`). To check or apply it manually:

```shell
iw wlan0 get power_save              # should say: Power save: off
sudo iw wlan0 set power_save off     # immediate, until reboot
```

**Card downloads failing:** transient Scryfall/Wi-Fi errors retry
automatically; just re-run `momir update` if a run dies - it resumes
incrementally and skips everything already downloaded.

**Pairing fails with "not available":** BlueZ can only pair devices in
its live discovery cache, which it flushes when scanning stops. Run
`scan on`, wait for the printer (named PT210_xxxx / MTP-II / PT200) to
appear, and `pair <MAC>` **while the scan is still running**. If the
adapter says NotReady, run `power on` in bluetoothctl first (and
`sudo rfkill unblock bluetooth` if that fails).

**Printer won't print:** check connectivity anywhere it surfaces - the
"Printer: connected/offline" line on the web page, `momir status`, the
SSH login banner, or the printer's antenna LED (steady = connected).
The rfcomm binding auto-reconnects every 10s while the printer is off
or out of range, so powering the printer on is usually the whole fix.
`momir logs` shows both services live.

**Printer won't charge (no LED when plugged in):** cheap USB-C devices
often lack the resistors that make USB-C-to-C cables deliver power, so
a C-to-C cable from a modern charger silently does nothing. Charge with
a **USB-A to USB-C cable** (field-verified on the PT-210), or the
included AC adapter/battery cradle if your unit shipped one. Orange
LED steady = charging, flashing = charge error (check temperature and
reseat the battery). A deeply drained pack can take several minutes
before the LED lights.

**Prints come out blank:** the paper roll is in backwards - thermal
paper only prints on one side. Flip the roll (see the printer prep
step).

**Art prints too dark:** downloaded art gets a brightness/contrast
boost before dithering (`art_brightness` / `art_contrast` under
`[SCRYFALL]`). Changing them affects newly downloaded art; run
`momir update full` overnight to re-process the whole library.

## Momir Online (no Pi needed)

A companion static page lives at
[operations.dev/momir](https://operations.dev/momir) (also
`momir.operations.dev`): the same mana-value grid, but it asks the
Scryfall API for a random creature directly from the browser - no
backend, no database, full-color card images, and a Show QR button for
table sharing. Source is [`/momir/index.html`](../momir/index.html) in
this repo, served by GitHub Pages. Trade-off vs. the Pi: it needs
internet and depends on Scryfall being up; the Pi plays fully offline
from its local database.

## Momir Basic rules

- 2 players, 24 starting life, 60+ basic lands as your deck
- Each turn you may discard a land and pay X: get a random creature with
  mana value X (that's the print button)

## Disclaimer

Not associated with Hasbro, Wizards of the Coast, or Magic: The Gathering.
Card data and art come from [Scryfall](https://scryfall.com/docs/api).
