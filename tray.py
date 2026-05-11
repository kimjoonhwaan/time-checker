import threading
import webbrowser
from io import BytesIO

from PIL import Image, ImageDraw
import pystray

from tracker import TrackerState


def _make_icon(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=color)
    return img


ICONS = {
    TrackerState.TRACKING: _make_icon("#22c55e"),
    TrackerState.IDLE: _make_icon("#eab308"),
    TrackerState.PAUSED: _make_icon("#ef4444"),
}


class TrayApp:
    def __init__(self, tracker, db, config: dict,
                 shutdown_event: threading.Event, pause_event: threading.Event):
        self._tracker = tracker
        self._db = db
        self._config = config
        self._shutdown = shutdown_event
        self._pause = pause_event
        self._icon = pystray.Icon(
            name="timechecker",
            icon=ICONS[TrackerState.IDLE],
            title="Time Checker",
            menu=self._build_menu()
        )
        self._update_thread = threading.Thread(target=self._update_loop, daemon=True)

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda _: self._status_label(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("대시보드 열기", self._open_dashboard),
            pystray.MenuItem(
                lambda _: "재개" if self._pause.is_set() else "일시정지",
                self._toggle_pause
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", self._quit),
        )

    def _status_label(self) -> str:
        status = self._tracker.get_status()
        total = status["today_total_seconds"]
        h, rem = divmod(total, 3600)
        m = rem // 60
        state_labels = {
            TrackerState.TRACKING: "추적 중",
            TrackerState.IDLE: "유휴",
            TrackerState.PAUSED: "일시정지",
        }
        label = state_labels.get(status["state"], "")
        return f"오늘: {h}h {m:02d}m  ({label})"

    def _open_dashboard(self, icon, item):
        port = self._config.get("_actual_port", self._config.get("flask_port", 5000))
        webbrowser.open(f"http://localhost:{port}")

    def _toggle_pause(self, icon, item):
        if self._pause.is_set():
            self._pause.clear()
        else:
            self._pause.set()

    def _quit(self, icon, item):
        self._shutdown.set()
        icon.stop()

    def _update_loop(self):
        while not self._shutdown.is_set():
            try:
                status = self._tracker.get_status()
                state = status["state"]
                self._icon.icon = ICONS.get(state, ICONS[TrackerState.IDLE])
                self._icon.title = self._status_label()
            except Exception:
                pass
            self._shutdown.wait(timeout=10)

    def run(self):
        self._update_thread.start()
        self._icon.run()
