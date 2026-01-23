from __future__ import annotations

import logging
import socket
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .config import resolve_printers_path, write_printers_file


@dataclass(frozen=True)
class AppConfig:
    printers: list[str]
    columns: int = 3
    timeout: float = 1.0
    title: str = "Управление принтерами"
    config_path: str | None = None


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

        self.printers = list(self.cfg.printers)
        self.printers_path = resolve_printers_path(self.cfg.config_path)
        self.buttons: dict[str, ttk.Button] = {}
        self.grid_frame: ttk.Frame | None = None

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

        ttk.Button(top, text="Настройки", command=self._open_settings).grid(row=0, column=2, padx=(8, 0))

        self.grid_frame = ttk.Frame(self.root)
        self.grid_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        self._render_printer_buttons()

        # первичная проверка
        self.refresh_all()

    def _render_printer_buttons(self) -> None:
        if not self.grid_frame:
            return

        for child in self.grid_frame.winfo_children():
            child.destroy()

        self.buttons = {}
        columns = max(1, int(self.cfg.columns))

        for i, printer_name in enumerate(self.printers):
            btn = ttk.Button(
                self.grid_frame,
                text=printer_name,
                style="Gray.TButton",
                command=lambda name=printer_name: self._open_printer_web_interface(name),
            )

            row = i // columns
            col = i % columns
            btn.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
            self.buttons[printer_name] = btn

            self.grid_frame.grid_rowconfigure(row, weight=1)
            self.grid_frame.grid_columnconfigure(col, weight=1)

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

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Настройка принтеров")
        dialog.transient(self.root)
        dialog.grab_set()

        printers = list(self.printers)

        def update_listbox() -> None:
            listbox.delete(0, tk.END)
            for item in printers:
                listbox.insert(tk.END, item)

        def add_printer() -> None:
            name = simpledialog.askstring("Добавить принтер", "Введите имя принтера:", parent=dialog)
            if name is None:
                return
            name = name.strip()
            if not name:
                messagebox.showwarning("Пустое имя", "Имя принтера не может быть пустым.", parent=dialog)
                return
            if name in printers:
                messagebox.showwarning("Дубликат", "Такой принтер уже есть в списке.", parent=dialog)
                return
            printers.append(name)
            update_listbox()

        def edit_printer() -> None:
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("Выбор", "Выберите принтер для редактирования.", parent=dialog)
                return
            idx = selection[0]
            current = printers[idx]
            name = simpledialog.askstring(
                "Редактировать принтер",
                "Введите новое имя принтера:",
                initialvalue=current,
                parent=dialog,
            )
            if name is None:
                return
            name = name.strip()
            if not name:
                messagebox.showwarning("Пустое имя", "Имя принтера не может быть пустым.", parent=dialog)
                return
            if name != current and name in printers:
                messagebox.showwarning("Дубликат", "Такой принтер уже есть в списке.", parent=dialog)
                return
            printers[idx] = name
            update_listbox()

        def remove_printer() -> None:
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("Выбор", "Выберите принтер для удаления.", parent=dialog)
                return
            idx = selection[0]
            name = printers[idx]
            if not messagebox.askyesno(
                "Удалить принтер", f"Удалить принтер «{name}»?", parent=dialog
            ):
                return
            printers.pop(idx)
            update_listbox()

        def save_and_close() -> None:
            cleaned: list[str] = []
            seen: set[str] = set()
            for item in printers:
                name = item.strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                cleaned.append(name)
            try:
                write_printers_file(self.printers_path, cleaned)
            except Exception as e:
                logging.error("Ошибка при сохранении списка принтеров: %s", e)
                messagebox.showerror("Ошибка", f"Не удалось сохранить список принтеров:\n{e}", parent=dialog)
                return
            self.printers = cleaned
            self._render_printer_buttons()
            self.refresh_all()
            self.status_var.set("Список принтеров сохранён.")
            dialog.destroy()

        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        list_frame = ttk.Frame(dialog)
        list_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, height=10)
        scrollbar.config(command=listbox.yview)

        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        actions = ttk.Frame(dialog)
        actions.grid(row=1, column=0, sticky="ew", padx=10)
        actions.columnconfigure(0, weight=1)

        ttk.Button(actions, text="Добавить", command=add_printer).grid(row=0, column=0, padx=(0, 6), pady=6)
        ttk.Button(actions, text="Редактировать", command=edit_printer).grid(row=0, column=1, padx=6, pady=6)
        ttk.Button(actions, text="Удалить", command=remove_printer).grid(row=0, column=2, padx=6, pady=6)

        controls = ttk.Frame(dialog)
        controls.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        controls.columnconfigure(0, weight=1)

        ttk.Button(controls, text="Сохранить", command=save_and_close).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(controls, text="Отмена", command=dialog.destroy).grid(row=0, column=1)

        update_listbox()
        dialog.wait_window()

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
