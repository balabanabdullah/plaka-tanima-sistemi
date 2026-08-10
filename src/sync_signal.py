"""OCR sureci ile Sync Manager arasinda hafif, dosya tabanli wakeup sinyali."""

import threading
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SYNC_SIGNAL_PATH = PROJECT_ROOT / "data" / ".cloud_sync_wakeup"
_signal_lock = threading.Lock()


def request_immediate_sync(signal_path: Path | str = DEFAULT_SYNC_SIGNAL_PATH) -> bool:
    """Network kullanmadan LOCAL -> CLOUD worker icin wakeup sinyali birakir."""
    path = Path(signal_path)
    try:
        with _signal_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(time.time_ns()), encoding="ascii")
        return True
    except OSError:
        # Sync Manager kapali veya dosya sistemi gecici olarak kullanilamaz olsa
        # bile OCR, SQLite ve bariyer akisi kesinlikle etkilenmez.
        return False


def get_sync_signal_version(signal_path: Path | str = DEFAULT_SYNC_SIGNAL_PATH) -> int:
    """Son wakeup sinyalinin surumunu salt okunur olarak dondurur."""
    path = Path(signal_path)
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0
