"""
D-Launcher - configurable application launcher for Windows workstations.
UI: PySide6 (Qt, C++ engine).  Compile to C with Nuitka for a fast native .exe.

* Starts blank until an admin configures it.
* Settings are protected by a PIN (default 4700, changeable in Admin > General).
* Admin keeps a list of KNOWN APPS as full paths (C:\\temp\\app1.exe, ...\\start.bat,
  ...\\manual.pdf). The ones that exist on this PC are offered to tick and list.
* Admin can also add any file / program manually, add Edge links, rename,
  reorder, remove tiles, change the launcher title and turn "start with Windows" on/off.
* Tiles show the program's real Windows icon automatically (or a custom PNG/ICO).
* Config is stored per PC in %PROGRAMDATA%\\DLauncher\\config.json.

Install:   pip install PySide6
Run:       pythonw dlauncher.py
Build exe: pip install nuitka
           python -m nuitka --standalone --enable-plugin=pyside6 ^
                  --windows-console-mode=disable --output-filename=DLauncher.exe dlauncher.py
"""

import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys

from PySide6.QtCore import QFileInfo, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFileIconProvider,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

APP_NAME = "D-Launcher"
VERSION = "2.2.0"
DEFAULT_PIN = "4700"
ACCENT = "#2a9d9b"
ACCENT_HOVER = "#1d6f6e"
BG = "#f2f2f2"
DEFAULT_TITLE = "D-Launcher - {pc}"
FILE_FILTER = ("Programs & documents (*.exe *.bat *.cmd *.lnk *.url *.ps1 *.py *.pyw *.pdf "
               "*.xlsx *.xlsm *.docx *.txt);;All files (*.*)")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _config_file():
    """Prefer a machine-wide location so every user on the PC sees the same tiles."""
    candidates = [
        os.environ.get("PROGRAMDATA"),
        os.environ.get("APPDATA"),
        os.path.dirname(os.path.abspath(sys.argv[0])),
    ]
    for base in filter(None, candidates):
        folder = os.path.join(base, "DLauncher")
        try:
            os.makedirs(folder, exist_ok=True)
            probe = os.path.join(folder, ".write_test")
            with open(probe, "w"):
                pass
            os.remove(probe)
            return os.path.join(folder, "config.json")
        except OSError:
            continue
    return os.path.abspath("config.json")


CONFIG_FILE = _config_file()


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def default_config():
    return {
        "pin_hash": hash_pin(DEFAULT_PIN),
        "columns": 1,
        "protect_quit": False,
        "title": DEFAULT_TITLE,
        # one entry per line: "C:\\full\\path\\file.exe" or "Display name | C:\\full\\path\\file.exe"
        "known_apps": [
            r"C:\temp\app1.exe",
            r"Evidence Camera | C:\temp\EvidenceCamera\EvidenceCamera.exe",
            r"Start script | C:\temp\app2\start.bat",
            r"Work instructions | C:\temp\docs\instructions.pdf",
        ],
        # each app: name, type ("program" | "edge"), target, args, workdir, icon, edge_app_mode
        "apps": [],
    }


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read config {CONFIG_FILE}: {exc}", file=sys.stderr)
    return cfg


def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


# --------------------------------------------------------------------------- #
# Windows helpers
# --------------------------------------------------------------------------- #
_mutex = None


def already_running() -> bool:
    global _mutex
    if os.name != "nt":
        return False
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DLauncherSingleInstance")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def startup_command() -> str:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():  # PyInstaller / Nuitka
        return f'"{os.path.abspath(sys.argv[0])}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return f'"{exe}" "{os.path.abspath(sys.argv[0])}"'


def get_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except (ImportError, OSError):
        return False


def set_autostart(enabled: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def find_edge():
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(path):
                return path
    return shutil.which("msedge")


def launch(app):
    if app.get("type") == "edge":
        url = app["target"]
        edge = find_edge()
        if edge:
            args = [edge, f"--app={url}"] if app.get("edge_app_mode") else [edge, url]
            subprocess.Popen(args, close_fds=True)
        else:
            os.startfile("microsoft-edge:" + url)
        return

    target = os.path.expandvars(app["target"])
    if not os.path.exists(target):
        raise FileNotFoundError(f"Not found on this PC:\n{target}")
    workdir = os.path.expandvars(app.get("workdir") or "") or os.path.dirname(target)
    os.startfile(target, arguments=app.get("args", "").strip(), cwd=workdir or None)


# --------------------------------------------------------------------------- #
# Known apps (full paths)
# --------------------------------------------------------------------------- #
def parse_known(lines):
    """'C:\\x\\app.exe' or 'Name | C:\\x\\app.exe'  ->  [(name, path)]"""
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, path = (part.strip() for part in line.split("|", 1))
        else:
            name, path = "", line
        path = os.path.normpath(path.strip().strip('"'))
        out.append((name or os.path.splitext(os.path.basename(path))[0], path))
    return out


def display_title(cfg):
    pc = socket.gethostname().upper()
    title = (cfg.get("title") or DEFAULT_TITLE).strip() or DEFAULT_TITLE
    return title.replace("{pc}", pc).replace("{PC}", pc)


# --------------------------------------------------------------------------- #
# Qt helpers
# --------------------------------------------------------------------------- #
_icon_provider = None


def app_icon(app):
    """Custom icon if set, otherwise the program's own Windows icon (Edge icon for links)."""
    global _icon_provider
    custom = app.get("icon")
    if custom and os.path.isfile(custom):
        icon = QIcon(custom)
        if not icon.isNull():
            return icon
    path = find_edge() if app.get("type") == "edge" else os.path.expandvars(app.get("target", ""))
    if path and os.path.exists(path):
        if _icon_provider is None:
            _icon_provider = QFileIconProvider()
        icon = _icon_provider.icon(QFileInfo(path))
        if not icon.isNull():
            return icon
    return None


def window_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(ACCENT))
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    p.setFont(QFont("Segoe UI", 26, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "D")
    p.end()
    return QIcon(pm)


def ask_pin(parent, cfg, title="Admin access") -> bool:
    pin, ok = QInputDialog.getText(parent, title, "Enter PIN:", QLineEdit.EchoMode.Password)
    if not ok:
        return False
    if hash_pin(pin.strip()) == cfg.get("pin_hash"):
        return True
    QMessageBox.critical(parent, APP_NAME, "Wrong PIN.")
    return False


def confirm(parent, text) -> bool:
    return QMessageBox.question(parent, APP_NAME, text) == QMessageBox.StandardButton.Yes


# --------------------------------------------------------------------------- #
# Tile
# --------------------------------------------------------------------------- #
class Tile(QWidget):
    clicked = Signal()
    RING = 112
    ICON = 64

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.hover = False
        self.icon = app_icon(app)
        self.setFixedSize(200, 175)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(app.get("target", ""))

    def enterEvent(self, event):
        self.hover = True
        self.update()

    def leaveEvent(self, event):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        d = self.RING
        x, y = (self.width() - d) // 2, 4
        p.setPen(QPen(QColor(ACCENT_HOVER if self.hover else ACCENT), 4 if self.hover else 3))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(x, y, d, d)

        if self.icon:
            s = self.ICON
            self.icon.paint(p, QRect(x + (d - s) // 2, y + (d - s) // 2, s, s))
        else:
            p.setPen(QColor("#222222"))
            p.setFont(QFont("Segoe UI", 34, QFont.Weight.Bold))
            p.drawText(QRect(x, y, d, d), Qt.AlignmentFlag.AlignCenter,
                       (self.app["name"][:1] or "?").upper())

        p.setPen(QColor("#222222"))
        p.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        top = y + d + 6
        p.drawText(QRect(0, top, self.width(), self.height() - top),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                   self.app["name"])


# --------------------------------------------------------------------------- #
# Main launcher window
# --------------------------------------------------------------------------- #
class Launcher(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle(display_title(self.cfg))
        self.setWindowIcon(window_icon())
        self.resize(420, 880)
        self.setMinimumSize(300, 400)

        central = QWidget(objectName="central")
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(objectName="topbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(4, 2, 10, 2)
        for text, slot in (("Settings", self.open_settings), ("Quit", self.close)):
            btn = QPushButton(text, objectName="barbtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            h.addWidget(btn)
        h.addStretch()
        h.addWidget(QLabel(f"Version {VERSION}", objectName="version"))
        v.addWidget(bar)

        line = QFrame(objectName="sep")
        line.setFixedHeight(1)
        v.addWidget(line)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        v.addWidget(self.scroll)
        self.setCentralWidget(central)

        self.setStyleSheet(f"""
            #central, QScrollArea, #tiles {{ background: {BG}; }}
            #topbar {{ background: {BG}; }}
            #sep {{ background: #d6d6d6; }}
            #barbtn {{ border: none; background: transparent; padding: 6px 10px;
                       font: 10pt 'Segoe UI'; color: #222; }}
            #barbtn:hover {{ background: #e2e2e2; border-radius: 4px; }}
            #version {{ color: #666; font: 9pt 'Segoe UI'; }}
        """)
        self.render()

    def render(self):
        self.setWindowTitle(display_title(self.cfg))
        host = QWidget(objectName="tiles")
        outer = QVBoxLayout(host)
        outer.addStretch()
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(20)
        cols = max(1, int(self.cfg.get("columns", 1)))
        for i, app in enumerate(self.cfg.get("apps", [])):
            tile = Tile(app)
            tile.clicked.connect(lambda a=app: self.start_app(a))
            grid.addWidget(tile, i // cols, i % cols)
        outer.addWidget(grid_host, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        self.scroll.setWidget(host)  # old widget is deleted by Qt

    def start_app(self, app):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            launch(app)
        except Exception as exc:  # show any launch failure to the operator
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, f"Could not start '{app['name']}'.\n\n{exc}")
            return
        QTimer.singleShot(1000, QApplication.restoreOverrideCursor)

    def open_settings(self):
        if ask_pin(self, self.cfg):
            SettingsDialog(self).exec()

    def closeEvent(self, event):
        if self.cfg.get("protect_quit") and not ask_pin(self, self.cfg, "Quit launcher"):
            event.ignore()
        else:
            event.accept()


# --------------------------------------------------------------------------- #
# Add / edit dialog
# --------------------------------------------------------------------------- #
class AppDialog(QDialog):
    def __init__(self, parent, kind, app=None):
        super().__init__(parent)
        app = app or {}
        self.kind = kind
        self.app_data = None
        self.setWindowTitle("Edge link" if kind == "edge" else "Program")
        form = QFormLayout(self)
        self.fields = {}

        def add_row(label, key, browse=None, example=""):
            edit = QLineEdit(app.get(key, ""))
            edit.setPlaceholderText(example)
            edit.setMinimumWidth(400)
            self.fields[key] = edit
            if browse:
                w = QWidget()
                hl = QHBoxLayout(w)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.addWidget(edit)
                b = QToolButton(text="…")
                b.clicked.connect(browse)
                hl.addWidget(b)
                form.addRow(label, w)
            else:
                form.addRow(label, edit)

        if kind == "edge":
            add_row("Display name", "name", example="e.g. MES Portal")
            add_row("URL", "target", example="e.g. https://mes.company.local/station/12")
        else:
            add_row("Display name", "name", example="e.g. Evidence Camera")
            add_row("Full path to file", "target", self._browse_target,
                    example=r"e.g. C:\temp\app1\app1.exe")
            add_row("Arguments (optional)", "args", example="e.g. --station 12 --fullscreen")
            add_row("Working folder (optional)", "workdir", self._browse_dir,
                    example=r"empty = folder of the file")
        add_row("Icon PNG/ICO (optional)", "icon", self._browse_icon,
                example="empty = use the program's own icon")

        if kind == "edge":
            help_text = ("<b>Examples</b><br>"
                         "https://mes.company.local/station/12<br>"
                         "http://10.20.30.40:8080/dashboard<br>"
                         "intranet.company.local/reports &nbsp;<i>(https:// is added automatically)</i>")
        else:
            help_text = ("<b>Examples</b> — always the full path to the file itself, not just the folder:<br>"
                         "C:\\temp\\app1\\app1.exe<br>"
                         "C:\\temp\\app2\\start.bat<br>"
                         "C:\\temp\\docs\\instructions.pdf &nbsp;<i>(opens in the default PDF viewer)</i><br>"
                         "\\\\server\\share\\Tool\\Tool.exe &nbsp;<i>(network path)</i><br>"
                         "%USERPROFILE%\\Desktop\\Report.lnk &nbsp;<i>(environment variables work)</i>")
        help_label = QLabel(help_text)
        help_label.setStyleSheet("color:#555; background:#f6f6f6; border:1px solid #ddd; padding:8px;")
        help_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(help_label)

        self.app_mode = QCheckBox("Open as app window (no tabs / address bar)")
        self.app_mode.setChecked(bool(app.get("edge_app_mode")))
        if kind == "edge":
            form.addRow("", self.app_mode)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _get(self, key):
        edit = self.fields.get(key)
        return edit.text().strip() if edit else ""

    def _browse_target(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose program or file", "", FILE_FILTER,
            options=QFileDialog.Option.DontResolveSymlinks)
        if path:
            path = os.path.normpath(path)
            self.fields["target"].setText(path)
            if not self._get("name"):
                self.fields["name"].setText(os.path.splitext(os.path.basename(path))[0])

    def _browse_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Working folder")
        if path:
            self.fields["workdir"].setText(os.path.normpath(path))

    def _browse_icon(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose icon", "", "Images (*.png *.ico *.gif *.jpg)")
        if path:
            self.fields["icon"].setText(os.path.normpath(path))

    def _ok(self):
        name = self._get("name")
        target = self._get("target").strip('"')
        if not name or not target:
            QMessageBox.critical(self, APP_NAME, "Name and target are required.")
            return
        if self.kind == "edge":
            if "://" not in target:
                target = "https://" + target
        elif os.path.isdir(os.path.expandvars(target)):
            QMessageBox.critical(self, APP_NAME, "That is a folder. Enter the full path to the file, "
                                                 "e.g. C:\\temp\\app1\\app1.exe")
            return
        elif not os.path.exists(os.path.expandvars(target)):
            if not confirm(self, "That path does not exist on this PC.\nSave anyway?"):
                return
        self.app_data = {
            "name": name,
            "type": self.kind,
            "target": target,
            "args": self._get("args"),
            "workdir": self._get("workdir"),
            "icon": self._get("icon"),
            "edge_app_mode": self.app_mode.isChecked(),
        }
        self.accept()


# --------------------------------------------------------------------------- #
# Admin window
# --------------------------------------------------------------------------- #
class SettingsDialog(QDialog):
    def __init__(self, launcher: Launcher):
        super().__init__(launcher)
        self.launcher = launcher
        self.cfg = launcher.cfg
        self.setWindowTitle(f"{APP_NAME} - Admin - {socket.gethostname().upper()}")
        self.known = []  # [(name, path, exists, listed)]
        self.resize(880, 540)

        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._build_apps_tab()
        self._build_scan_tab()
        self._build_general_tab()

        self.refresh_apps()
        if self.run_scan() > 0:
            self.tabs.setCurrentWidget(self.scan_tab)  # tell the admin straight away

    # ---- shared --------------------------------------------------------
    def commit(self, select=None):
        try:
            save_config(self.cfg)
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not save config:\n{exc}")
        self.refresh_apps()
        self.mark_scan_results()
        self.launcher.render()
        if select is not None and 0 <= select < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(select))

    def listed_paths(self):
        return {os.path.normcase(os.path.expandvars(a["target"]))
                for a in self.cfg["apps"] if a.get("type") == "program"}

    # ---- tab 1: listed apps -------------------------------------------
    def _build_apps_tab(self):
        tab = QWidget()
        self.tabs.addTab(tab, "Listed apps")
        h = QHBoxLayout(tab)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Target"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 70)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit_app())
        h.addWidget(self.tree, 1)

        side = QVBoxLayout()
        buttons = [
            ("Add program…", lambda: self.add_app("program")),
            ("Add Edge link…", lambda: self.add_app("edge")),
            None,
            ("Rename…", self.rename_app),
            ("Edit…", self.edit_app),
            ("Move up", lambda: self.move_app(-1)),
            ("Move down", lambda: self.move_app(1)),
            ("Remove", self.remove_app),
            None,
            ("Test launch", self.test_app),
        ]
        for item in buttons:
            if item is None:
                side.addSpacing(14)
                continue
            btn = QPushButton(item[0])
            btn.setMinimumWidth(130)
            btn.clicked.connect(item[1])
            side.addWidget(btn)
        side.addStretch()
        h.addLayout(side)

    def refresh_apps(self):
        self.tree.clear()
        for i, app in enumerate(self.cfg["apps"]):
            target = app["target"]
            missing = app.get("type") == "program" and not os.path.exists(os.path.expandvars(target))
            if missing:
                target += "   (missing on this PC)"
            kind = "Edge" if app.get("type") == "edge" else "Program"
            item = QTreeWidgetItem([app["name"], kind, target])
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            icon = app_icon(app)
            if icon:
                item.setIcon(0, icon)
            if missing:
                item.setForeground(2, QColor("#c0392b"))
            self.tree.addTopLevelItem(item)

    def selected_index(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def add_app(self, kind):
        dlg = AppDialog(self, kind)
        if dlg.exec() and dlg.app_data:
            self.cfg["apps"].append(dlg.app_data)
            self.commit(select=len(self.cfg["apps"]) - 1)

    def edit_app(self):
        i = self.selected_index()
        if i is None:
            return
        app = self.cfg["apps"][i]
        dlg = AppDialog(self, app.get("type", "program"), app)
        if dlg.exec() and dlg.app_data:
            self.cfg["apps"][i] = dlg.app_data
            self.commit(select=i)

    def rename_app(self):
        i = self.selected_index()
        if i is None:
            return
        app = self.cfg["apps"][i]
        name, ok = QInputDialog.getText(self, "Rename tile", "Name shown on the launcher:",
                                        QLineEdit.EchoMode.Normal, app["name"])
        if ok and name.strip():
            app["name"] = name.strip()
            self.commit(select=i)

    def move_app(self, delta):
        i = self.selected_index()
        apps = self.cfg["apps"]
        if i is None or not 0 <= i + delta < len(apps):
            return
        apps[i], apps[i + delta] = apps[i + delta], apps[i]
        self.commit(select=i + delta)

    def remove_app(self):
        i = self.selected_index()
        if i is None:
            return
        if confirm(self, f"Remove '{self.cfg['apps'][i]['name']}' from the launcher?"):
            del self.cfg["apps"][i]
            self.commit()

    def test_app(self):
        i = self.selected_index()
        if i is not None:
            self.launcher.start_app(self.cfg["apps"][i])

    # ---- tab 2: known apps ---------------------------------------------
    def _build_scan_tab(self):
        tab = QWidget()
        self.scan_tab = tab
        self.tabs.addTab(tab, "Known apps")
        v = QVBoxLayout(tab)

        info = QLabel("Full path to each known file, one per line. Optional display name before a <b>|</b>.<br>"
                      "<span style='color:#555'>C:\\temp\\app1.exe<br>"
                      "Evidence Camera | C:\\temp\\EvidenceCamera\\EvidenceCamera.exe<br>"
                      "Work instructions | C:\\temp\\docs\\instructions.pdf</span>")
        v.addWidget(info)
        self.known_edit = QPlainTextEdit("\n".join(self.cfg.get("known_apps", [])))
        self.known_edit.setFont(QFont("Consolas", 10))
        self.known_edit.setMaximumHeight(120)
        v.addWidget(self.known_edit)

        row = QHBoxLayout()
        browse = QPushButton("Browse && add path…")
        browse.clicked.connect(self.browse_known)
        row.addWidget(browse)
        check = QPushButton("Save list && check this PC")
        check.clicked.connect(self.run_scan)
        row.addWidget(check)
        self.scan_status = QLabel()
        row.addWidget(self.scan_status, 1)
        v.addLayout(row)

        self.scan_tree = QTreeWidget()
        self.scan_tree.setHeaderLabels(["Name", "Full path", "Status on this PC"])
        self.scan_tree.setRootIsDecorated(False)
        self.scan_tree.setColumnWidth(0, 220)
        self.scan_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.scan_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.scan_tree, 1)

        row = QHBoxLayout()
        row.addStretch()
        tick_all = QPushButton("Tick all found")
        tick_all.clicked.connect(self.tick_all_new)
        row.addWidget(tick_all)
        add = QPushButton("Add ticked to launcher")
        add.clicked.connect(self.add_scanned)
        row.addWidget(add)
        v.addLayout(row)

    def browse_known(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose program or file", "", FILE_FILTER,
                                              options=QFileDialog.Option.DontResolveSymlinks)
        if path:
            text = self.known_edit.toPlainText().rstrip()
            self.known_edit.setPlainText((text + "\n" if text else "") + os.path.normpath(path))
            self.run_scan()

    def run_scan(self) -> int:
        lines = [ln.strip() for ln in self.known_edit.toPlainText().splitlines() if ln.strip()]
        if lines != self.cfg.get("known_apps"):
            self.cfg["known_apps"] = lines
            save_config(self.cfg)
        return self.mark_scan_results()

    def mark_scan_results(self) -> int:
        listed = self.listed_paths()
        self.scan_tree.clear()
        self.known = []
        new = 0
        for name, path in parse_known(self.cfg.get("known_apps", [])):
            full = os.path.expandvars(path)
            exists = os.path.isfile(full)
            is_listed = os.path.normcase(full) in listed
            idx = len(self.known)
            self.known.append((name, path))
            if is_listed:
                status, color = "Already listed", "#999999"
            elif exists:
                status, color = "Found", ACCENT_HOVER
                new += 1
            else:
                status, color = "Not on this PC", "#c0392b"
            item = QTreeWidgetItem([name, path, status])
            item.setData(0, Qt.ItemDataRole.UserRole, idx)
            item.setForeground(2, QColor(color))
            icon = app_icon({"type": "program", "target": path}) if exists else None
            if icon:
                item.setIcon(0, icon)
            if exists and not is_listed:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                              | Qt.ItemFlag.ItemIsSelectable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                for c in range(2):
                    item.setForeground(c, QColor("#999999"))
            self.scan_tree.addTopLevelItem(item)
        found = sum(1 for _, p in self.known if os.path.isfile(os.path.expandvars(p)))
        self.scan_status.setText(f"{found} of {len(self.known)} known app(s) found on this PC, "
                                 f"{new} not yet listed." if self.known else "List is empty.")
        idx = self.tabs.indexOf(self.scan_tab)
        self.tabs.setTabText(idx, f"Known apps ({new} new)" if new else "Known apps")
        return new

    def _scan_items(self):
        return [self.scan_tree.topLevelItem(i) for i in range(self.scan_tree.topLevelItemCount())]

    def tick_all_new(self):
        for item in self._scan_items():
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(0, Qt.CheckState.Checked)

    def add_scanned(self):
        listed = self.listed_paths()
        added = 0
        for item in self._scan_items():
            if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            name, path = self.known[item.data(0, Qt.ItemDataRole.UserRole)]
            if os.path.normcase(os.path.expandvars(path)) in listed:
                continue
            self.cfg["apps"].append({"name": name, "type": "program", "target": path, "args": "",
                                     "workdir": "", "icon": "", "edge_app_mode": False})
            added += 1
        if added:
            self.commit()
            self.scan_status.setText(f"Added {added} app(s). Rename or reorder them in 'Listed apps'.")

    # ---- tab 3: general -----------------------------------------------
    def _build_general_tab(self):
        tab = QWidget()
        self.tabs.addTab(tab, "General")
        v = QVBoxLayout(tab)
        form = QFormLayout()
        v.addLayout(form)

        self.title_edit = QLineEdit(self.cfg.get("title") or DEFAULT_TITLE)
        self.title_edit.setPlaceholderText(DEFAULT_TITLE)
        self.title_edit.setMinimumWidth(320)
        self.title_edit.editingFinished.connect(self._apply_title)
        title_hint = QLabel("{pc} is replaced by this computer's name, e.g. "
                            f"\"Line 3 - Evidence ({socket.gethostname().upper()})\"")
        title_hint.setStyleSheet("color:#666;")
        form.addRow("Launcher title", self.title_edit)
        form.addRow("", title_hint)

        self.columns_spin = QSpinBox()
        self.columns_spin.setRange(1, 6)
        self.columns_spin.setValue(int(self.cfg.get("columns", 1)))
        self.columns_spin.valueChanged.connect(self._apply_columns)
        form.addRow("Tile columns", self.columns_spin)

        self.autostart_cb = QCheckBox("Start launcher when Windows starts (current user)")
        self.autostart_cb.setChecked(get_autostart())
        self.autostart_cb.toggled.connect(self._apply_autostart)
        form.addRow(self.autostart_cb)

        self.protect_cb = QCheckBox("Require PIN to quit / close the launcher")
        self.protect_cb.setChecked(bool(self.cfg.get("protect_quit")))
        self.protect_cb.toggled.connect(self._apply_protect_quit)
        form.addRow(self.protect_cb)

        box = QGroupBox("Change PIN")
        bf = QFormLayout(box)
        self.pin1 = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.pin2 = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        bf.addRow("New PIN", self.pin1)
        bf.addRow("Confirm", self.pin2)
        save_pin = QPushButton("Save PIN")
        save_pin.clicked.connect(self._change_pin)
        bf.addRow("", save_pin)
        box.setMaximumWidth(320)
        v.addWidget(box)

        path_label = QLabel(f"Config file: {CONFIG_FILE}")
        path_label.setStyleSheet("color: #666;")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(path_label)
        v.addStretch()

    def _apply_title(self):
        title = self.title_edit.text().strip() or DEFAULT_TITLE
        if title != self.cfg.get("title"):
            self.cfg["title"] = title
            self.commit()

    def _apply_columns(self, value):
        self.cfg["columns"] = int(value)
        self.commit()

    def _apply_autostart(self, enabled):
        try:
            set_autostart(enabled)
        except (ImportError, OSError) as exc:
            self.autostart_cb.blockSignals(True)
            self.autostart_cb.setChecked(get_autostart())
            self.autostart_cb.blockSignals(False)
            QMessageBox.critical(self, APP_NAME, f"Could not change startup setting:\n{exc}")

    def _apply_protect_quit(self, enabled):
        self.cfg["protect_quit"] = bool(enabled)
        self.commit()

    def _change_pin(self):
        p1, p2 = self.pin1.text().strip(), self.pin2.text().strip()
        if len(p1) < 4 or not p1.isdigit():
            QMessageBox.critical(self, APP_NAME, "PIN must be at least 4 digits.")
            return
        if p1 != p2:
            QMessageBox.critical(self, APP_NAME, "PINs do not match.")
            return
        self.cfg["pin_hash"] = hash_pin(p1)
        self.commit()
        self.pin1.clear()
        self.pin2.clear()
        QMessageBox.information(self, APP_NAME, "PIN changed.")


# --------------------------------------------------------------------------- #
def main():
    if already_running():
        sys.exit(0)
    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setWindowIcon(window_icon())
    win = Launcher()
    win.show()
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
