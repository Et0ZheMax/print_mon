from __future__ import annotations

import logging
import socket
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox


@dataclass(frozen=True)
class AppConfig:
    printers: list[str]
    columns: int = 3
    timeout: float = 1.0
    title: str = "Управление принтерами"


class PrinterDashboard:
    def __init__(self, cfg: AppConfig, state_dir: Path):
        self.cfg = cfg
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.log_path = self.state_dir / "printer_manager.log"
        self.window_pos_path = self.state_dir / "window_position.txt"

        logging.basicConfig(
            filename=str(self.log_path),
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )

        self.root = tk.Tk()
        self.root.title(self.cfg.title)

        self.style = ttk.Style()
        self.style.configure("TButton", padding=5, relief="solid", borderwidth=2)
        self.style.configure("Green.TButton", background="#a8df65", foreground="black")
        self.style.configure("Red.TButton", background="#ff6961", foreground="white")
        self.style.configure("Gray.TButton", background="#d9d9d9", foreground="black")

        self.buttons: dict[str, ttk.Button] = {}

        self._build_ui()
        self._load_window_position()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        top.columnconfigure(1, weight=1)

        ttk.Button(top, text="Обновить все", command=self.refresh_all).grid(row=0, column=0, padx=(0, 8))

        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=1, sticky="w")

        grid = ttk.Frame(self.root)
        grid.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        printers = sorted(self.cfg.printers)
        columns = max(1, int(self.cfg.columns))

        for i, printer_name in enumerate(printers):
            btn = ttk.Button(
                grid,
                text=printer_name,
                style="Gray.TButton",
                command=lambda name=printer_name: self._open_printer_web_interface(name),
            )

            row = i // columns
            col = i % columns
            btn.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
            self.buttons[printer_name] = btn

            grid.grid_rowconfigure(row, weight=1)
            grid.grid_columnconfigure(col, weight=1)

        # первичная проверка
        self.refresh_all()

    # ---------- Actions ----------

    def _open_printer_web_interface(self, printer_name: str) -> None:
        try:
            printer_ip = socket.gethostbyname(printer_name)
            webbrowser.open(f"http://{printer_ip}")
        except Exception as e:
            logging.error("Не удалось открыть веб-интерфейс %s: %s", printer_name, e)
            messagebox.showerror("Ошибка", f"Не удалось открыть веб-интерфейс {printer_name}:\n{e}")

    def refresh_all(self) -> None:
        self.status_var.set("Проверяю…")
        for name in self.buttons.keys():
            self._check_printer_status_async(name)
        # убираем статус чуть позже
        self.root.after(800, lambda: self.status_var.set(""))

    def _check_printer_status_async(self, printer_name: str) -> None:
        def worker() -> None:
            available = self._check_printer_availability_by_name(printer_name)
            self.root.after(0, lambda: self._set_button_state(printer_name, available))

        threading.Thread(target=worker, daemon=True).start()

    def _check_printer_availability_by_name(self, printer_name: str) -> bool:
        try:
            printer_ip = socket.gethostbyname(printer_name)
            socket.setdefaulttimeout(float(self.cfg.timeout))

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                result = sock.connect_ex((printer_ip, 80))
            finally:
                sock.close()

            return result == 0

        except socket.gaierror as e:
            logging.error("Не удалось разрешить имя принтера %s: %s", printer_name, e)
            return False
        except Exception as e:
            logging.error("Ошибка при проверке принтера %s: %s", printer_name, e)
            return False

    def _set_button_state(self, printer_name: str, available: bool) -> None:
        btn = self.buttons.get(printer_name)
        if not btn:
            return
        btn.configure(style="Green.TButton" if available else "Red.TButton")

    # ---------- Window state ----------

    def _on_close(self) -> None:
        self._save_window_position()
        self.root.destroy()

    def _save_window_position(self) -> None:
        try:
            self.window_pos_path.write_text(self.root.geometry(), encoding="utf-8")
        except Exception as e:
            logging.error("Ошибка при сохранении позиции окна: %s", e)

    def _load_window_position(self) -> None:
        try:
            if self.window_pos_path.is_file():
                pos = self.window_pos_path.read_text(encoding="utf-8").strip()
                if pos:
                    self.root.geometry(pos)
        except Exception as e:
            logging.error("Ошибка при загрузке позиции окна: %s", e)

    # ---------- Run ----------

    def run(self) -> None:
        self.root.mainloop()
