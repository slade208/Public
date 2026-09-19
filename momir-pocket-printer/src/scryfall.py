"""Scryfall API client for downloading and managing Magic: The Gathering card data."""

import json
import logging
import random
import shutil
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from time import sleep
from typing import Any, Dict, List, Optional, Set

import gzip
import ijson
import requests
from PIL import Image, ImageEnhance

logger = logging.getLogger('momir.scryfall')


class Scryfall:
    """Scryfall API client for downloading and managing Magic: The Gathering card data.

    This class provides methods for:
    - Downloading card data from Scryfall's bulk API
    - Processing and storing card information locally
    - Managing incremental updates to minimize bandwidth usage
    - Filtering cards based on Momir Basic format rules
    """

    # File and format constants
    METADATA_FILENAME = "metadata.json"
    CARD_FILE_EXTENSION = ".json"
    ART_FILE_EXTENSION = ".jpg"

    # Processing constants
    PROGRESS_LOG_INTERVAL = 1000
    MONOCHROME_MODE = "1"
    HTTP_OK = 200
    REQUEST_TIMEOUT = 30
    JSON_STREAM_PATH = "item"  # Path for ijson to parse array items

    # Momir Basic validation constants
    PAPER_FORMAT = "paper"
    CREATURE_TYPE = "creature"

    # Set types that count as "a new set came out" for the update check
    # (leaves out tokens, promos, memorabilia and other side products).
    MAIN_SET_TYPES = {"expansion", "core", "masters", "commander",
                      "draft_innovation", "remastered"}

    def __init__(self, scryfall_config, filesystem_config) -> None:
        """Initialize Scryfall client with configuration.

        Args:
            scryfall_config: Configuration section for Scryfall API settings
            filesystem_config: Configuration section for filesystem paths
        """
        # API configuration
        self.base_url: str = scryfall_config.get('base_url')
        self.bulk_data_endpoint: str = scryfall_config.get(
            'bulk_data_endpoint')
        self.header_accept: str = scryfall_config.get('header_accept')
        self.header_user_agent: str = scryfall_config.get('header_user_agent')
        self.header_accept_encoding: str = scryfall_config.get(
            'header_accept_encoding')

        # Processing configuration
        self.request_delay_seconds: float = scryfall_config.getfloat(
            'request_delay_seconds')
        self.max_retries: int = scryfall_config.getint('max_retries')
        self.art_width_px: int = scryfall_config.getint('art_width_px')
        self.art_brightness: float = scryfall_config.getfloat(
            'art_brightness', fallback=1.0)
        self.art_contrast: float = scryfall_config.getfloat(
            'art_contrast', fallback=1.0)
        self.include_spoilers: bool = scryfall_config.getboolean(
            'include_spoilers', fallback=True)

        # Filter configuration
        self.excluded_sets: List[str] = [
            s.strip() for s in scryfall_config.get('excluded_sets').replace('\n', '').split(',') if s.strip()
        ]
        self.excluded_layouts: List[str] = [
            layout.strip() for layout in scryfall_config.get('excluded_layouts').replace('\n', '').split(',') if layout.strip()
        ]

        # Filesystem paths (use __file__ for reliability)
        base_path = Path(__file__).resolve().parent.parent
        self.cards_path: Path = base_path / filesystem_config.get('cards_path')
        self.art_path: Path = base_path / filesystem_config.get('art_path')
        self.default_card_art_path: Path = base_path / \
            filesystem_config.get('default_card_art_path')
        self.access_rights: int = int(
            filesystem_config.get('access_rights'), 0)

        # Ensure runtime directories exist so app can boot on a fresh install.
        self.cards_path.mkdir(parents=True, exist_ok=True, mode=self.access_rights)
        self.art_path.mkdir(parents=True, exist_ok=True, mode=self.access_rights)

        # Validate critical configuration
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate critical configuration values on initialization.

        Raises:
            ValueError: If critical configuration is missing or invalid
        """
        if not self.base_url or not self.bulk_data_endpoint:
            raise ValueError("Scryfall API URL configuration is missing")

        if self.max_retries < 0:
            raise ValueError(
                f"max_retries must be non-negative, got {self.max_retries}")

        if self.art_width_px <= 0:
            raise ValueError(
                f"art_width_px must be positive, got {self.art_width_px}")

        if not self.default_card_art_path.exists():
            logger.warning(
                f"Default card art not found at {self.default_card_art_path}. "
                "Fallback art will be generated when needed."
            )

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (f"Scryfall(base_url='{self.base_url}', "
                f"cards_path='{self.cards_path}', "
                f"max_retries={self.max_retries})")

    # ===== Public API Methods =====

    def get_valid_cmcs(self) -> List[int]:
        """Get a sorted list of all CMC directories that exist.

        Returns:
            Sorted list of CMC values (integers)
        """
        if not self.cards_path.exists():
            return []

        cmc_dirs = [int(d.name) for d in self.cards_path.iterdir()
                    if d.is_dir() and d.name.isdigit()]
        return sorted(cmc_dirs)

    def get_total_card_count(self) -> int:
        """Count the total number of card JSON files in CMC directories.

        Returns:
            Total number of card files
        """
        if not self.cards_path.exists():
            return 0

        total_count = 0
        for cmc_dir in self.cards_path.iterdir():
            if not (cmc_dir.is_dir() and cmc_dir.name.isdigit()):
                continue

            total_count += sum(
                1
                for card_file in cmc_dir.iterdir()
                if card_file.is_file() and card_file.suffix == self.CARD_FILE_EXTENSION
            )

        return total_count

    def get_card_count_by_cmc(self, cmc: int) -> int:
        """Count the number of cards with a specific CMC.

        Args:
            cmc: Converted mana cost to count

        Returns:
            Number of cards with the specified CMC
        """
        path = self.cards_path / str(cmc)
        if not path.exists():
            return 0
        return sum(
            1
            for card in path.iterdir()
            if card.is_file() and card.suffix == self.CARD_FILE_EXTENSION
        )

    def get_card_art_by_card_id(self, card_id: str) -> Optional[Image.Image]:
        """Load card art from disk by card ID.

        Args:
            card_id: Unique Scryfall card ID

        Returns:
            PIL Image object or None if not found

        Raises:
            OSError: If image file exists but cannot be opened
        """
        card_art_path = self._get_art_path(card_id)

        if not card_art_path.is_file():
            logger.warning(f"Card art not found: {card_id}")
            return None

        try:
            return Image.open(card_art_path)
        except OSError as e:
            logger.error(f"Failed to open card art {card_id}: {e}")
            raise

    def get_random_card_by_cmc(self, cmc: int) -> Optional[Dict[str, Any]]:
        """Get a random card with the specified CMC.

        Args:
            cmc: Converted mana cost to search for (must be non-negative)

        Returns:
            Card data dictionary or None if no cards found

        Raises:
            ValueError: If cmc is negative
            json.JSONDecodeError: If card JSON is malformed
        """
        if cmc < 0:
            raise ValueError(f"CMC must be non-negative, got {cmc}")

        path = self.cards_path / str(cmc)

        if not path.exists():
            logger.warning(f"CMC directory not found: {cmc}")
            return None

        cards = [card for card in path.iterdir() if card.is_file()]

        if not cards:
            logger.warning(f"No cards found for CMC: {cmc}")
            return None

        random_card_path = random.choice(cards)
        with open(random_card_path, 'r', encoding='utf-8') as card_file:
            return json.load(card_file)

    def is_valid_momir_basic_card(self, card: Dict[str, Any]) -> bool:
        """Check if a card is valid for Momir Basic format.

        Validates based on:
        - Card layout (excludes special layouts like tokens, vanguards)
        - Set type (excludes memorabilia, promo sets, etc.)
        - Paper availability (must be available in paper format)
        - Card type (must be a creature)

        Args:
            card: Card data dictionary from Scryfall

        Returns:
            True if card is valid for Momir Basic, False otherwise
        """
        # Check layout
        if card.get('layout') in self.excluded_layouts:
            return False

        # Check set type
        if card.get('set_type') in self.excluded_sets:
            return False

        # Check if available in paper format. Scryfall doesn't flag
        # spoiler-season cards as 'paper' until release day, so cards
        # whose set hasn't released yet are admitted too - a fully
        # spoiled set can join the pool before its paper release.
        if self.PAPER_FORMAT not in (card.get('games') or []):
            if not self.include_spoilers:
                return False
            released = card.get('released_at') or ''
            if released <= date.today().isoformat():
                return False

        # Check if it's a creature (handle double-faced cards)
        if 'card_faces' in card:
            front_type = card['card_faces'][0].get('type_line', '').lower()
            return self.CREATURE_TYPE in front_type

        type_line = card.get('type_line', '').lower()
        return self.CREATURE_TYPE in type_line

    def download_bulk_metadata(self) -> Dict[str, Any]:
        """Download bulk data metadata from Scryfall.

        Returns:
            Dictionary containing bulk data metadata

        Raises:
            requests.RequestException: If the request fails
        """
        headers = self._get_request_headers()
        url = f"{self.base_url}{self.bulk_data_endpoint}"

        for attempt in range(self.max_retries + 1):
            try:
                response = requests.get(
                    url, headers=headers, timeout=self.REQUEST_TIMEOUT)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e:
                if attempt < self.max_retries:
                    wait = 2 ** attempt  # 1s, 2s, 4s...
                    logger.warning(
                        f"Bulk metadata fetch failed ({type(e).__name__}). "
                        f"Retry {attempt + 1}/{self.max_retries} in {wait}s: {url}")
                    sleep(wait)
                else:
                    logger.error(f"Error fetching bulk metadata from {url}: {e}")
                    raise

    def get_newest_remote_set(self, max_days_ahead: int = 21) -> Optional[Dict[str, Any]]:
        """Ask Scryfall which main paper set is newest (one small request).

        Powers the app's "card update available" notice. A single attempt
        with the normal timeout so it fails fast when there's no internet
        (hotspot mode). Sets releasing more than max_days_ahead days from
        now are ignored - previews trickle in early, but a set is only
        fully spoiled a few weeks before release.

        Returns:
            {'name', 'code', 'released_at', 'card_count', 'set_type'} for
            the newest qualifying set, or None if none was found.
        """
        url = f"{self.base_url}/sets"
        response = requests.get(url, headers=self._get_request_headers(),
                                timeout=self.REQUEST_TIMEOUT)
        response.raise_for_status()
        horizon = (date.today() + timedelta(days=max_days_ahead)).isoformat()
        best_key = None
        best = None
        for s in response.json().get('data', []):
            if s.get('digital') or s.get('set_type') not in self.MAIN_SET_TYPES:
                continue
            if not s.get('card_count'):
                continue
            released = s.get('released_at') or ''
            if not released or released > horizon:
                continue
            # Prefer the later release; on a tie (e.g. a set and its
            # Commander decks share a date) prefer the main expansion.
            key = (released, 1 if s.get('set_type') == 'expansion' else 0)
            if best_key is None or key > best_key:
                best_key = key
                best = {'name': s.get('name'), 'code': s.get('code'),
                        'released_at': released,
                        'card_count': s.get('card_count', 0),
                        'set_type': s.get('set_type')}
        return best

    def _get_bulk_download(self, bulk_metadata: Dict[str, Any]) -> tuple:
        """Resolve the bulk file download URI, preferring the JSONL format.

        Scryfall retired the JSON-array bulk files (and their download_uri
        field) on 2026-07-20; current responses expose jsonl_download_uri,
        a gzipped JSON Lines file with one card object per line.

        Returns:
            Tuple of (uri, is_jsonl)

        Raises:
            KeyError: If no known download URI field is present
        """
        uri = bulk_metadata.get('jsonl_download_uri')
        if uri:
            return uri, True
        uri = bulk_metadata.get('download_uri')
        if uri:
            return uri, False
        raise KeyError(
            "Bulk metadata contains neither jsonl_download_uri nor download_uri; "
            f"got fields: {sorted(bulk_metadata.keys())}")

    def filter_bulk_data_by_cmc(self, bulk_data: List[Dict[str, Any]], cmc: float) -> List[Dict[str, Any]]:
        """Filter a list of cards by CMC.

        Args:
            bulk_data: List of card dictionaries
            cmc: Converted mana cost to filter by

        Returns:
            Filtered list of cards matching the CMC
        """
        return [card for card in bulk_data if card.get('cmc') == cmc]

    def delete_directory(self, path: Path) -> None:
        """Delete a directory and all its contents.

        Args:
            path: Path to directory to delete
        """
        path_obj = Path(path)
        if path_obj.exists():
            shutil.rmtree(path_obj, ignore_errors=True)
            logger.debug(f"Deleted directory: {path}")

    def create_directory(self, path: Path) -> None:
        """Create a directory if it doesn't already exist.

        Args:
            path: Path to directory to create
        """
        Path(path).mkdir(parents=True, exist_ok=True, mode=self.access_rights)

    def save_card(self, path: Path, card: Dict[str, Any]) -> None:
        """Save card data as JSON to the filesystem.

        Args:
            path: Filesystem path to save to
            card: Card data dictionary
        """
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(card, f, indent=2, ensure_ascii=False)

    # ===== Private Helper Methods =====

    # Configuration and Utility Helpers

    def _get_request_headers(self, include_encoding: bool = False) -> Dict[str, str]:
        """Build request headers for Scryfall API calls.

        Args:
            include_encoding: Whether to include Accept-Encoding header

        Returns:
            Dictionary of HTTP headers
        """
        headers = {
            'Accept': self.header_accept,
            'User-Agent': self.header_user_agent
        }
        if include_encoding:
            headers['Accept-Encoding'] = self.header_accept_encoding
        return headers

    # Path Resolution Helpers

    def _get_card_path(self, card_id: str, cmc: int) -> Path:
        """Get the filesystem path for a card JSON file.

        Args:
            card_id: Unique Scryfall card ID
            cmc: Converted mana cost

        Returns:
            Path object for card JSON file
        """
        return self.cards_path / str(cmc) / f"{card_id}{self.CARD_FILE_EXTENSION}"

    def _get_art_path(self, card_id: str) -> Path:
        """Get the filesystem path for card art.

        Args:
            card_id: Unique Scryfall card ID

        Returns:
            Path object for card art file
        """
        return self.art_path / f"{card_id}{self.ART_FILE_EXTENSION}"

    # Card Data Extraction Helpers

    def _get_card_art_uri(self, card: Dict[str, Any]) -> Optional[str]:
        """Extract art crop URI from card data.

        Args:
            card: Card data dictionary

        Returns:
            Art crop URI string or None if not found
        """
        return (card.get('card_faces', [{}])[0].get('image_uris', {}).get('art_crop')
                or card.get('image_uris', {}).get('art_crop'))

    # Image Processing Helpers

    def _process_image(self, image_bytes: bytes) -> Image.Image:
        """Process image: resize, enhance, and convert to grayscale.

        Art is stored as enhanced grayscale rather than pre-dithered 1-bit:
        the ESC/POS layer dithers at print time, and grayscale both dithers
        better and displays better in the web UI. Brightness/contrast are
        boosted (configurable) because thermal prints tend to run dark.

        Args:
            image_bytes: Raw image bytes

        Returns:
            Processed PIL Image object
        """
        img = Image.open(BytesIO(image_bytes))

        # Calculate new dimensions maintaining aspect ratio
        w_percent = self.art_width_px / float(img.size[0])
        h_size = int(float(img.size[1]) * w_percent)

        img = img.resize((self.art_width_px, h_size), Image.Resampling.LANCZOS)
        img = img.convert('L')
        if self.art_brightness != 1.0:
            img = ImageEnhance.Brightness(img).enhance(self.art_brightness)
        if self.art_contrast != 1.0:
            img = ImageEnhance.Contrast(img).enhance(self.art_contrast)
        return img

    # Data Processing and Cleanup Helpers

    def _ensure_fallback_art(self, path: Path) -> None:
        """Write fallback art to disk when remote art download fails.

        Prefers configured default art image, but if it is missing, generates a
        blank monochrome placeholder so processing can continue.

        Args:
            path: Filesystem path for fallback art output
        """
        if self.default_card_art_path.exists():
            shutil.copy(self.default_card_art_path, path)
            return

        placeholder = Image.new(self.MONOCHROME_MODE, (self.art_width_px, self.art_width_px), 1)
        placeholder.save(path)
        logger.warning(f"Generated placeholder fallback art: {path.name}")

    def _log_progress(self, stats: Dict[str, int], is_full_refresh: bool) -> None:
        """Log progress during card processing.

        Args:
            stats: Dictionary containing processing statistics
            is_full_refresh: Whether this is a full refresh operation
        """
        if is_full_refresh:
            logger.info(
                f"Processed {stats['total_processed']} cards. "
                f"Found {stats['total_creatures']} valid creatures so far."
            )
        else:
            logger.info(
                f"Processed {stats['total_processed']} cards "
                f"({stats['new_cards']} new, {stats['skipped_cards']} existing)."
            )

    def _try_download_art(self, path: Path, uri: str, headers: Dict[str, str]) -> bool:
        """Attempt one download-and-save of card art from a URI, with retries.

        Returns:
            True if the art was saved, False if all attempts failed.
        """
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.get(
                    uri, headers=headers, timeout=self.REQUEST_TIMEOUT,
                    allow_redirects=True)

                if response.status_code == self.HTTP_OK:
                    img = self._process_image(response.content)
                    img.save(path)
                    logger.debug(f"Saved card art: {path.name}")
                    sleep(self.request_delay_seconds)
                    return True

                # 4xx means the URI itself is bad (stale bulk-data URL or
                # rejected request); retrying the same URI won't help.
                if 400 <= response.status_code < 500:
                    logger.warning(
                        f"Download rejected (HTTP {response.status_code}): {uri}")
                    return False

                if attempt < self.max_retries:
                    logger.warning(
                        f"Download failed (HTTP {response.status_code}). "
                        f"Retry {attempt + 1}/{self.max_retries}: {uri}"
                    )
                    sleep(self.request_delay_seconds)

            except requests.RequestException as e:
                if attempt < self.max_retries:
                    logger.warning(
                        f"Request exception ({type(e).__name__}). "
                        f"Retry {attempt + 1}/{self.max_retries}: {uri}"
                    )
                    sleep(self.request_delay_seconds)

        return False

    def save_card_art(self, path: Path, card_art_uri: str,
                      card_id: Optional[str] = None) -> None:
        """Download and save card art, falling back to the API image redirect.

        Tries the direct art URI from bulk data first (with proper
        User-Agent/Accept headers - Scryfall rejects bare requests). If that
        fails and a card_id is available, asks the API for the card's current
        art_crop image, which redirects to the up-to-date file. Only after
        both fail is the default placeholder art used.

        Args:
            path: Filesystem path to save art to
            card_art_uri: URI of the card art from bulk data
            card_id: Scryfall card ID for the API image fallback
        """
        headers = {
            'User-Agent': self.header_user_agent,
            'Accept': '*/*',
        }

        candidates = [card_art_uri]
        if card_id:
            candidates.append(
                f"{self.base_url}/cards/{card_id}?format=image&version=art_crop")

        for uri in candidates:
            if self._try_download_art(path, uri, headers):
                return

        logger.error(f"All art sources failed. Using default art for: {path.name}")
        self._ensure_fallback_art(path)

    def card_exists_locally(self, card_id: str, cmc: int) -> bool:
        """Check if a card already exists locally without loading all IDs into memory.

        Args:
            card_id: Unique Scryfall card ID
            cmc: Converted mana cost

        Returns:
            True if both card JSON and art exist locally
        """
        return self._get_card_path(card_id, cmc).exists() and self._get_art_path(card_id).exists()

    def needs_refresh(self) -> bool:
        """Check if a refresh is needed by comparing metadata timestamps.

        Returns:
            True if refresh is needed, False if data is up to date
        """
        metadata = self.get_metadata()
        if not metadata or not metadata.get('updated_at'):
            logger.info("No metadata found or no timestamp. Refresh needed.")
            return True

        try:
            bulk_metadata = self.download_bulk_metadata()
            local_updated_at = metadata.get('updated_at')
            remote_updated_at = bulk_metadata.get('updated_at')

            if local_updated_at != remote_updated_at:
                logger.info(
                    f"Update available. Local: {local_updated_at}, Remote: {remote_updated_at}")
                return True

            logger.info(
                f"Card data is up to date (last updated: {local_updated_at})")
            return False
        except requests.RequestException:
            logger.warning(
                "Unable to check for updates. Assuming no refresh needed.")
            return False

    def generate_metadata(self, bulk_metadata: Optional[Dict[str, Any]] = None,
                          newest_set_name: Optional[str] = None,
                          newest_released_at: Optional[str] = None) -> None:
        """Generate metadata file with card counts and update timestamp.

        Args:
            bulk_metadata: Optional bulk metadata from Scryfall
            newest_set_name: Name of the newest set seen in the pool
            newest_released_at: Its release date (ISO)
        """
        metadata_path = self.cards_path / self.METADATA_FILENAME

        # Preserve previously recorded values when regenerating without
        # fresh stream stats / bulk metadata (e.g. the get_metadata side
        # effect) - counts are recomputed, provenance is kept.
        old = {}
        if (newest_set_name is None or bulk_metadata is None) and metadata_path.exists():
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    old = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        if newest_set_name is None:
            newest_set_name = old.get('newest_set_name')
            newest_released_at = old.get('newest_released_at')

        metadata = {
            'newest_set_name': newest_set_name,
            'newest_released_at': newest_released_at,
            'updated_at': (bulk_metadata.get('updated_at') if bulk_metadata
                           else old.get('updated_at')),
            'download_uri': ((bulk_metadata.get('jsonl_download_uri')
                              or bulk_metadata.get('download_uri'))
                             if bulk_metadata else old.get('download_uri')),
            'total_card_count': self.get_total_card_count(),
            'cmc_card_count': {str(cmc): self.get_card_count_by_cmc(cmc)
                               for cmc in self.get_valid_cmcs()}
        }

        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        logger.debug(f"Generated metadata at {metadata_path}")

    def get_metadata(self) -> Dict[str, Any]:
        """Load metadata from disk.

        Note: This method has a side effect - it will generate a new metadata file
        if one doesn't exist. Use with caution in read-only contexts.

        Returns:
            Metadata dictionary containing:
            - updated_at: Timestamp of last database update
            - download_uri: URI used for bulk download
            - total_card_count: Total number of cards stored
            - cmc_card_count: Dictionary mapping CMC to card count
        """
        metadata_path = self.cards_path / self.METADATA_FILENAME

        if not metadata_path.exists():
            logger.warning(
                "Metadata file not found. Generating new metadata...")
            self.generate_metadata()

        with open(metadata_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def process_and_save_card(self, card: Dict[str, Any]) -> None:
        """Process and save a single card's JSON data and artwork.

        This method:
        1. Determines the card's CMC and creates the directory if needed
        2. Saves the card JSON data to the appropriate CMC folder
        3. Downloads and saves the card artwork if available

        Args:
            card: Card data dictionary from Scryfall (must contain 'id' and 'cmc')

        Raises:
            KeyError: If card is missing required fields
            OSError: If filesystem operations fail
        """
        card_id = card['id']
        cmc = int(card.get('cmc', 0))

        # Ensure CMC directory exists
        cmc_path = self.cards_path / str(cmc)
        cmc_path.mkdir(parents=True, exist_ok=True, mode=self.access_rights)

        # Save card JSON
        card_path = self._get_card_path(card_id, cmc)
        self.save_card(card_path, card)

        # Save card art if available
        card_art_uri = self._get_card_art_uri(card)
        if card_art_uri:
            art_path = self._get_art_path(card_id)
            self.save_card_art(art_path, card_art_uri, card_id=card_id)

    def _remove_obsolete_cards(self, valid_card_ids: Set[str]) -> int:
        """Remove cards that are no longer valid in the Scryfall database.

        Args:
            valid_card_ids: Set of card IDs that are still valid

        Returns:
            Number of cards removed
        """
        logger.info("Scanning for obsolete cards to remove...")
        removed_count = 0

        for cmc_dir in self.cards_path.iterdir():
            if not (cmc_dir.is_dir() and cmc_dir.name.isdigit()):
                continue

            for card_file in cmc_dir.iterdir():
                if card_file.is_file() and card_file.suffix == self.CARD_FILE_EXTENSION:
                    card_id = card_file.stem
                    if card_id not in valid_card_ids:
                        # Remove card JSON and art
                        card_file.unlink()
                        art_file = self._get_art_path(card_id)
                        if art_file.exists():
                            art_file.unlink()
                        removed_count += 1

        if removed_count > 0:
            logger.info(f"Removed {removed_count} obsolete cards")

        return removed_count

    def _stream_and_process_cards(self, bulk_metadata: Dict[str, Any], force_full_refresh: bool) -> Dict[str, Any]:
        """Stream cards from Scryfall and process them incrementally.

        Args:
            bulk_metadata: Metadata containing download URI
            force_full_refresh: Whether to reprocess all cards

        Returns:
            Dictionary containing processing statistics:
            - total_processed: Total cards processed
            - total_creatures: Valid creature cards found
            - new_cards: Number of new cards added
            - skipped_cards: Number of existing cards skipped
            - valid_card_ids: Set of all valid card IDs

        Raises:
            requests.RequestException: If streaming fails
        """
        headers = self._get_request_headers(include_encoding=True)
        stats = {
            'total_processed': 0,
            'total_creatures': 0,
            'new_cards': 0,
            'skipped_cards': 0,
            'valid_card_ids': set(),
            'newest_released_at': '',
            'newest_set_name': '',
        }

        logger.info("Streaming bulk data from Scryfall...")
        download_uri, is_jsonl = self._get_bulk_download(bulk_metadata)

        try:
            with requests.get(download_uri, headers=headers, stream=True,
                              timeout=self.REQUEST_TIMEOUT) as response:
                response.raise_for_status()

                with gzip.GzipFile(fileobj=response.raw) as unzipped_stream:
                    if is_jsonl:
                        # JSONL: one card object per line, no wrapping array.
                        parser = (json.loads(line) for line in unzipped_stream
                                  if line.strip())
                    else:
                        parser = ijson.items(
                            unzipped_stream, self.JSON_STREAM_PATH, use_float=True)

                    for card in parser:
                        stats['total_processed'] += 1

                        if self.is_valid_momir_basic_card(card):
                            stats['total_creatures'] += 1
                            card_id = card['id']
                            cmc = int(card.get('cmc', 0))
                            stats['valid_card_ids'].add(card_id)

                            # Track the newest set in the pool (ISO dates
                            # compare correctly as strings).
                            released = card.get('released_at') or ''
                            if released > stats['newest_released_at']:
                                stats['newest_released_at'] = released
                                stats['newest_set_name'] = card.get('set_name') or ''

                            # Check if card already exists locally
                            if force_full_refresh or not self.card_exists_locally(card_id, cmc):
                                self.process_and_save_card(card)
                                stats['new_cards'] += 1
                            else:
                                stats['skipped_cards'] += 1

                        # Progress logging
                        if stats['total_processed'] % self.PROGRESS_LOG_INTERVAL == 0:
                            self._log_progress(stats, force_full_refresh)
        except requests.RequestException as e:
            logger.error(f"Error streaming bulk data: {e}")
            raise

        return stats

    def refresh_card_data(self, force_full_refresh: bool = False) -> None:
        """Refresh card data, downloading only new/updated cards unless force_full_refresh is True.

        Args:
            force_full_refresh: If True, delete and re-download all data. 
                              If False, perform incremental update.

        Raises:
            requests.RequestException: If API requests fail
            Exception: If processing fails for any other reason
        """
        # Check if refresh is needed
        if not force_full_refresh and not self.needs_refresh():
            logger.info("No refresh needed. Card data is already up to date.")
            return

        # Ensure directories exist
        self.create_directory(self.cards_path)
        self.create_directory(self.art_path)

        # Clear existing data if doing full refresh
        if force_full_refresh:
            logger.info(
                "Performing full refresh - all cards will be re-downloaded")
            self.delete_directory(self.cards_path)
            self.create_directory(self.cards_path)
            self.delete_directory(self.art_path)
            self.create_directory(self.art_path)
        else:
            logger.info("Performing incremental update...")

        # Download metadata and stream cards
        try:
            bulk_metadata = self.download_bulk_metadata()
            stats = self._stream_and_process_cards(
                bulk_metadata, force_full_refresh)

            # Clean up obsolete cards (incremental update only)
            removed_cards = 0
            if not force_full_refresh:
                removed_cards = self._remove_obsolete_cards(
                    stats['valid_card_ids'])

            # Log final statistics
            if force_full_refresh:
                logger.info(f"Finished processing {stats['total_processed']} cards. "
                            f"Found {stats['total_creatures']} valid creatures.")
            else:
                logger.info(f"Finished incremental update. Processed {stats['total_processed']} cards, "
                            f"added {stats['new_cards']} new cards, removed {removed_cards} obsolete cards.")

            # Update metadata
            self.generate_metadata(
                bulk_metadata,
                newest_set_name=stats.get('newest_set_name') or None,
                newest_released_at=stats.get('newest_released_at') or None)

        except Exception as e:
            logger.error(f"Error during card refresh: {e}")
            raise
