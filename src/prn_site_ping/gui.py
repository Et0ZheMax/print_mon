from __future__ import annotations

import json
import logging
import re
import subprocess
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .config import resolve_printers_path, write_printers_file
from .models import CardSeverity, PrinterStatus, SnmpConfig
from .monitoring import PrinterMonitor
from .print_server import fetch_printers_from_server


@dataclass(frozen=True)
class AppConfig:
    printers: list[str]
    columns: int = 3
    timeout: float = 1.0
    title: str = "Управление принтерами"
    config_path: str | None = None
    print_server: str | None = "dc02"
    sync_interval: int = 300




def _compose_card_summary(summary: str, diagnostic: str | None) -> str:
    base_summary = summary.strip() if summary else ""
    if not base_summary:
        base_summary = "SNMP: недоступен" if diagnostic else "Нет данных"
    if not diagnostic:
        return base_summary
    return f"{base_summary}\ndiag: {diagnostic}"


class PrinterCard(ttk.Frame):
    COLOR_BY_SEVERITY = {
        CardSeverity.OK: "#38a169",
        CardSeverity.OFFLINE: "#e53e3e",
        CardSeverity.WARNING: "#dd6b20",
        CardSeverity.CRITICAL: "#c53030",
        CardSeverity.UNKNOWN: "#718096",
    }

    def __init__(self, parent: ttk.Frame, printer_name: str, on_click) -> None:
        super().__init__(parent, padding=8, relief="solid", borderwidth=1, height=72)
        self.printer_name = printer_name
        self.on_click = on_click

        self.columnconfigure(0, weight=1)
        self.grid_propagate(False)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        self.name_lbl = ttk.Label(top, text=printer_name, font=("TkDefaultFont", 10, "bold"))
        self.name_lbl.grid(row=0, column=0, sticky="w")

        self.dot = tk.Canvas(top, width=12, height=12, highlightthickness=0, bd=0)
        self.dot.grid(row=0, column=1, sticky="e")
        self._dot_id = self.dot.create_oval(1, 1, 11, 11, fill="#718096", outline="")

        self.summary_lbl = ttk.Label(self, text="Проверка…", foreground="#4a5568", justify="left")
        self.summary_lbl.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._status_hint: str | None = None
        self.bind("<Configure>", self._on_resize, add="+")

        self._bind_click_recursive(self)

    def _on_resize(self, _event) -> None:
        card_width = max(140, self.winfo_width() - 16)
        self.summary_lbl.configure(wraplength=card_width)

    def _bind_click_recursive(self, widget) -> None:
        widget.bind("<Button-1>", self._on_click, add="+")
        widget.bind("<Enter>", lambda e: widget.configure(cursor="hand2"), add="+")
        for child in widget.winfo_children():
            self._bind_click_recursive(child)

    def _on_click(self, _event) -> None:
        self.on_click(self.printer_name)

    def set_status(self, status: PrinterStatus) -> None:
        dot_severity = CardSeverity.OK if status.reachable and status.severity == CardSeverity.UNKNOWN else status.severity
        color = self.COLOR_BY_SEVERITY[dot_severity]
        self.dot.itemconfigure(self._dot_id, fill=color)
        self.summary_lbl.configure(text=_compose_card_summary(status.summary_text, status.diagnostic))
        self._status_hint = status.diagnostic

        if status.severity == CardSeverity.OFFLINE:
            self.state(["disabled"])
            self.summary_lbl.configure(foreground="#a0aec0")
        else:
            self.state(["!disabled"])
            self.summary_lbl.configure(foreground="#4a5568")


class PrinterDashboard:
    def __init__(self, cfg: AppConfig, state_dir: Path):
        self.cfg = cfg
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.log_path = self.state_dir / "printer_manager.log"
        self.window_pos_path = self.state_dir / "window_position.txt"
        self.settings_path = self.state_dir / "settings.json"

        logging.basicConfig(
            filename=str(self.log_path),
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        )

        self.root = tk.Tk()
        self.root.title(self.cfg.title)

        self.printers = self._sort_printers(self.cfg.printers)
        self.printers_path = resolve_printers_path(self.cfg.config_path)
        self.cards: dict[str, PrinterCard] = {}
        self.grid_canvas: tk.Canvas | None = None
        self.grid_frame: ttk.Frame | None = None
        self.grid_scrollbar: ttk.Scrollbar | None = None
        self._grid_canvas_window: int | None = None
        self._sync_in_progress = False
        self._status_by_printer: dict[str, PrinterStatus] = {}

        self.snmp_config = self._load_snmp_settings()
        self.monitor = PrinterMonitor(timeout=self.cfg.timeout, snmp_config=self.snmp_config)

        worker_count = max(4, min(16, len(self.printers) or 4))
        self.executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="printer-monitor")
        self._render_revision = 0
        self._snmp_refresh_job: str | None = None

        self._build_ui()
        self._load_window_position()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_snmp_settings(self) -> SnmpConfig:
        if not self.settings_path.is_file():
            return SnmpConfig()
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            snmp = raw.get("snmp", {})
            return SnmpConfig(
                enabled=bool(snmp.get("enabled", True)),
                community=str(snmp.get("community", "public")),
                port=int(snmp.get("port", 161)),
                timeout=float(snmp.get("timeout", 1.2)),
                retries=int(snmp.get("retries", 1)),
                refresh_interval=int(snmp.get("refresh_interval", 300)),
                warning_threshold=int(snmp.get("warning_threshold", 20)),
                critical_threshold=int(snmp.get("critical_threshold", 10)),
            )
        except Exception as exc:
            logging.error("Failed to load settings: %s", exc)
            return SnmpConfig()

    def _save_snmp_settings(self) -> None:
        payload = {"snmp": asdict(self.snmp_config)}
        self.settings_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        top.columnconfigure(1, weight=1)

        ttk.Button(top, text="Обновить все", command=lambda: self.refresh_all(force_snmp=True)).grid(row=0, column=0, padx=(0, 8))
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=1, sticky="w")
        ttk.Button(top, text="Настройки", command=self._open_settings).grid(row=0, column=2, padx=(8, 0))

        cards_container = ttk.Frame(self.root)
        cards_container.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        cards_container.rowconfigure(0, weight=1)
        cards_container.columnconfigure(0, weight=1)

        self.grid_canvas = tk.Canvas(cards_container, highlightthickness=0, borderwidth=0)
        self.grid_canvas.grid(row=0, column=0, sticky="nsew")
        self.grid_scrollbar = ttk.Scrollbar(cards_container, orient="vertical", command=self.grid_canvas.yview)
        self.grid_scrollbar.grid(row=0, column=1, sticky="ns")
        self.grid_canvas.configure(yscrollcommand=self.grid_scrollbar.set)

        self.grid_frame = ttk.Frame(self.grid_canvas)
        self._grid_canvas_window = self.grid_canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", self._on_cards_frame_configure, add="+")
        self.grid_canvas.bind("<Configure>", self._on_cards_canvas_configure, add="+")

        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        self._render_printer_cards()
        self.refresh_all(force_snmp=True)
        self._schedule_snmp_refresh()
        self._schedule_server_sync(initial=True)

    def _render_printer_cards(self) -> None:
        if not self.grid_frame:
            return
        self._render_revision += 1

        for child in self.grid_frame.winfo_children():
            child.destroy()

        self.cards = {}
        columns = max(1, int(self.cfg.columns))

        for i, printer_name in enumerate(self._sort_printers(self.printers)):
            card = PrinterCard(self.grid_frame, printer_name, self._open_printer_web_interface)
            row = i // columns
            col = i % columns
            card.grid(row=row, column=col, sticky="ew", padx=5, pady=5)
            self.cards[printer_name] = card
            self.grid_frame.grid_rowconfigure(row, weight=0, minsize=72)
            self.grid_frame.grid_columnconfigure(col, weight=1)

        self._on_cards_frame_configure()

    def _on_cards_frame_configure(self, _event=None) -> None:
        if not self.grid_canvas or not self.grid_frame:
            return
        self.grid_canvas.configure(scrollregion=self.grid_canvas.bbox("all"))

    def _on_cards_canvas_configure(self, event) -> None:
        if not self.grid_canvas or self._grid_canvas_window is None:
            return
        self.grid_canvas.itemconfigure(self._grid_canvas_window, width=event.width)

    @staticmethod
    def _sort_printers(printers: list[str]) -> list[str]:
        return sorted(printers, key=lambda name: name.casefold())

    def _open_printer_web_interface(self, printer_name: str) -> None:
        status = self.monitor.build_status(printer_name, include_snmp=False)
        if not status.resolved_ip:
            messagebox.showerror("Ошибка", f"Не удалось открыть веб-интерфейс {printer_name}: DNS ошибка")
            return
        webbrowser.open(f"http://{status.resolved_ip}")

    def refresh_all(self, force_snmp: bool) -> None:
        self.status_var.set("Проверяю…")
        revision = self._render_revision
        for name in list(self.cards.keys()):
            self._submit_status_refresh(name, include_snmp=force_snmp, revision=revision)
        self.root.after(1200, lambda: self.status_var.set(""))

    def _submit_status_refresh(self, printer_name: str, include_snmp: bool, revision: int) -> None:
        future = self.executor.submit(self.monitor.build_status, printer_name, include_snmp)

        def done_callback(done_future) -> None:
            try:
                status = done_future.result()
            except Exception as exc:
                logging.error("Monitor task crashed for %s: %s", printer_name, exc)
                return
            self.root.after(0, lambda: self._apply_status(printer_name, status, revision))

        future.add_done_callback(done_callback)

    def _apply_status(self, printer_name: str, status: PrinterStatus, revision: int) -> None:
        if revision != self._render_revision:
            return
        card = self.cards.get(printer_name)
        if not card:
            return
        self._status_by_printer[printer_name] = status
        card.set_status(status)

    def _schedule_snmp_refresh(self) -> None:
        if self._snmp_refresh_job is not None:
            self.root.after_cancel(self._snmp_refresh_job)
        if self.snmp_config.refresh_interval <= 0:
            return

        def periodic() -> None:
            self.refresh_all(force_snmp=True)
            self._schedule_snmp_refresh()

        self._snmp_refresh_job = self.root.after(int(self.snmp_config.refresh_interval * 1000), periodic)

    def _schedule_server_sync(self, initial: bool = False) -> None:
        if not self.cfg.print_server or self.cfg.sync_interval <= 0:
            return
        delay_ms = 1000 if initial else int(self.cfg.sync_interval * 1000)
        self.root.after(delay_ms, self._sync_printers_from_server_async)

    def _sync_printers_from_server_async(self) -> None:
        if self._sync_in_progress:
            self._schedule_server_sync()
            return
        self._sync_in_progress = True

        def worker() -> None:
            try:
                names = fetch_printers_from_server(self.cfg.print_server or "")
                if names:
                    self.root.after(0, lambda: self._apply_server_printers(names))
            except subprocess.CalledProcessError as e:
                logging.error("Print server sync failed for %s: %s", self.cfg.print_server, (e.stderr or "").strip() or e)
            except Exception as e:
                logging.error("Print server sync error for %s: %s", self.cfg.print_server, e)
            finally:
                self._sync_in_progress = False
                self.root.after(0, self._schedule_server_sync)

        self.executor.submit(worker)

    def _apply_server_printers(self, names: list[str]) -> None:
        sorted_names = self._sort_printers(names)
        if sorted_names == self.printers:
            return
        self.printers = sorted_names
        self._render_printer_cards()
        self.refresh_all(force_snmp=True)
        self.status_var.set("Список принтеров синхронизирован с сервером.")
        try:
            write_printers_file(self.printers_path, self.printers)
        except Exception as e:
            logging.error("Не удалось сохранить список принтеров после синхронизации: %s", e)

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Настройка принтеров")
        dialog.transient(self.root)
        dialog.grab_set()

        printers = self._sort_printers(self.printers)

        def update_listbox() -> None:
            listbox.delete(0, tk.END)
            for item in printers:
                listbox.insert(tk.END, item)

        def add_printer() -> None:
            raw = simpledialog.askstring("Добавить принтеры", "Введите имена принтеров через запятую или пробел:", parent=dialog)
            if raw is None:
                return
            names = [item.strip() for item in re.split(r"[,\s;]+", raw) if item.strip()]
            if not names:
                messagebox.showwarning("Пустое имя", "Имя принтера не может быть пустым.", parent=dialog)
                return
            existing = set(printers)
            printers.extend([name for name in names if name not in existing])
            printers[:] = self._sort_printers(printers)
            update_listbox()

        def edit_printer() -> None:
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("Выбор", "Выберите принтер для редактирования.", parent=dialog)
                return
            idx = selection[0]
            current = printers[idx]
            name = simpledialog.askstring("Редактировать принтер", "Введите новое имя принтера:", initialvalue=current, parent=dialog)
            if not name:
                return
            name = name.strip()
            if not name:
                return
            if name != current and name in printers:
                messagebox.showwarning("Дубликат", "Такой принтер уже есть в списке.", parent=dialog)
                return
            printers[idx] = name
            printers[:] = self._sort_printers(printers)
            update_listbox()

        def remove_printer() -> None:
            selection = listbox.curselection()
            if not selection:
                return
            idx = selection[0]
            printers.pop(idx)
            update_listbox()

        snmp_enabled = tk.BooleanVar(value=self.snmp_config.enabled)
        snmp_community = tk.StringVar(value=self.snmp_config.community)
        snmp_port = tk.StringVar(value=str(self.snmp_config.port))
        snmp_timeout = tk.StringVar(value=str(self.snmp_config.timeout))
        snmp_retries = tk.StringVar(value=str(self.snmp_config.retries))
        snmp_interval = tk.StringVar(value=str(self.snmp_config.refresh_interval))
        snmp_warning = tk.StringVar(value=str(self.snmp_config.warning_threshold))
        snmp_critical = tk.StringVar(value=str(self.snmp_config.critical_threshold))

        def save_and_close() -> None:
            cleaned = self._sort_printers([item.strip() for item in printers if item.strip()])
            try:
                write_printers_file(self.printers_path, cleaned)
                self.snmp_config = SnmpConfig(
                    enabled=bool(snmp_enabled.get()),
                    community=snmp_community.get().strip() or "public",
                    port=int(snmp_port.get()),
                    timeout=float(snmp_timeout.get()),
                    retries=int(snmp_retries.get()),
                    refresh_interval=int(snmp_interval.get()),
                    warning_threshold=int(snmp_warning.get()),
                    critical_threshold=int(snmp_critical.get()),
                )
                self.monitor = PrinterMonitor(timeout=self.cfg.timeout, snmp_config=self.snmp_config)
                self._save_snmp_settings()
            except Exception as e:
                logging.error("Ошибка сохранения настроек: %s", e)
                messagebox.showerror("Ошибка", f"Не удалось сохранить настройки:\n{e}", parent=dialog)
                return

            self.printers = self._sort_printers(cleaned)
            self._render_printer_cards()
            self.refresh_all(force_snmp=True)
            self._schedule_snmp_refresh()
            dialog.destroy()

        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)

        list_frame = ttk.Frame(dialog)
        list_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, height=9)
        scrollbar.config(command=listbox.yview)
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        actions = ttk.Frame(dialog)
        actions.grid(row=1, column=0, sticky="ew", padx=10)
        ttk.Button(actions, text="Добавить", command=add_printer).grid(row=0, column=0, padx=(0, 6), pady=6)
        ttk.Button(actions, text="Редактировать", command=edit_printer).grid(row=0, column=1, padx=6, pady=6)
        ttk.Button(actions, text="Удалить", command=remove_printer).grid(row=0, column=2, padx=6, pady=6)

        snmp_frame = ttk.LabelFrame(dialog, text="SNMP")
        snmp_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        for c in range(4):
            snmp_frame.columnconfigure(c, weight=1)
        ttk.Checkbutton(snmp_frame, text="Включить SNMP мониторинг", variable=snmp_enabled).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)

        fields = [
            ("Community", snmp_community),
            ("Port", snmp_port),
            ("Timeout", snmp_timeout),
            ("Retries", snmp_retries),
            ("SNMP interval (sec)", snmp_interval),
            ("Warning %", snmp_warning),
            ("Critical %", snmp_critical),
        ]
        for i, (title, var) in enumerate(fields, start=1):
            ttk.Label(snmp_frame, text=title).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(snmp_frame, textvariable=var).grid(row=i, column=1, sticky="ew", pady=2)

        controls = ttk.Frame(dialog)
        controls.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        ttk.Button(controls, text="Сохранить", command=save_and_close).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(controls, text="Отмена", command=dialog.destroy).grid(row=0, column=1)

        update_listbox()
        dialog.wait_window()

    def _on_close(self) -> None:
        self._save_window_position()
        self.executor.shutdown(wait=False, cancel_futures=True)
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

    def run(self) -> None:
        self.root.mainloop()
