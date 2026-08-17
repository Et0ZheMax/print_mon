from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import tempfile
import time
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .config import resolve_printers_path, write_printers_file
from .models import CardSeverity, PrinterStatus, SnmpConfig, SupplyLevel
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


PALETTE = {
    "page": "#F1F5F9",
    "surface": "#FFFFFF",
    "surface_muted": "#F8FAFC",
    "header": "#0F172A",
    "header_muted": "#94A3B8",
    "text": "#0F172A",
    "muted": "#64748B",
    "line": "#E2E8F0",
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "ok": "#16A34A",
    "warning": "#D97706",
    "critical": "#DC2626",
    "offline": "#64748B",
    "unknown": "#7C3AED",
}

STATUS_META = {
    CardSeverity.OK: ("В сети", PALETTE["ok"], "#DCFCE7"),
    CardSeverity.WARNING: ("Внимание", PALETTE["warning"], "#FEF3C7"),
    CardSeverity.CRITICAL: ("Критично", PALETTE["critical"], "#FEE2E2"),
    CardSeverity.OFFLINE: ("Не в сети", PALETTE["offline"], "#E2E8F0"),
    CardSeverity.UNKNOWN: ("Без данных", PALETTE["unknown"], "#EDE9FE"),
}

DIAGNOSTIC_LABELS = {
    "dns failed": "DNS-имя не найдено",
    "web unavailable": "веб-интерфейс недоступен",
    "SNMP disabled": "SNMP выключен",
    "SNMP library missing": "модуль SNMP не установлен",
    "SNMP timeout": "SNMP не ответил",
    "SNMP auth/community failed": "ошибка community SNMP",
    "standard printer mib supplies not available": "данные расходников не поддерживаются",
    "invalid supplies table data": "принтер вернул некорректные данные",
    "partial data only": "получены неполные данные",
}


def _compose_card_summary(summary: str, diagnostic: str | None) -> str:
    """Compatibility helper used by integrations and older tests."""
    base_summary = summary.strip() if summary else ""
    if not base_summary:
        base_summary = "SNMP: недоступен" if diagnostic else "Нет данных"
    if not diagnostic:
        return base_summary
    return f"{base_summary}\ndiag: {diagnostic}"


def _humanize_diagnostic(diagnostic: str | None) -> str:
    if not diagnostic:
        return ""
    parts = [part.strip() for part in diagnostic.split(";") if part.strip()]
    return " · ".join(DIAGNOSTIC_LABELS.get(part, part) for part in parts)


def _responsive_columns(width: int, configured_max: int, card_min_width: int = 300) -> int:
    usable = max(1, int(width) - 32)
    return max(1, min(max(1, int(configured_max)), usable // max(1, card_min_width)))


def _reachability_worker_count(printer_count: int) -> int:
    """Run the lightweight first stage in one wave for typical installations."""
    return max(4, min(64, int(printer_count) or 4))


def _validated_snmp_config(
    *,
    enabled: bool,
    community: str,
    port: str,
    timeout: str,
    retries: str,
    refresh_interval: str,
    warning_threshold: str,
    critical_threshold: str,
) -> SnmpConfig:
    parsed_port = int(port)
    parsed_timeout = float(timeout.replace(",", "."))
    parsed_retries = int(retries)
    parsed_interval = int(refresh_interval)
    parsed_warning = int(warning_threshold)
    parsed_critical = int(critical_threshold)

    if not 1 <= parsed_port <= 65535:
        raise ValueError("SNMP-порт должен быть от 1 до 65535")
    if not 0.1 <= parsed_timeout <= 30:
        raise ValueError("Таймаут SNMP должен быть от 0,1 до 30 секунд")
    if not 0 <= parsed_retries <= 5:
        raise ValueError("Число повторов должно быть от 0 до 5")
    if not 0 <= parsed_interval <= 86_400:
        raise ValueError("Интервал обновления должен быть от 0 до 86400 секунд")
    if not 0 <= parsed_critical <= parsed_warning <= 100:
        raise ValueError("Порог должен удовлетворять: 0 ≤ критический ≤ предупреждение ≤ 100")

    return SnmpConfig(
        enabled=enabled,
        community=community.strip() or "public",
        port=parsed_port,
        timeout=parsed_timeout,
        retries=parsed_retries,
        refresh_interval=parsed_interval,
        warning_threshold=parsed_warning,
        critical_threshold=parsed_critical,
    )


class PrinterCard(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        printer_name: str,
        on_click: Callable[[str], None],
    ) -> None:
        super().__init__(
            parent,
            bg=PALETTE["surface"],
            highlightthickness=1,
            highlightbackground=PALETTE["line"],
            highlightcolor=PALETTE["primary"],
            padx=16,
            pady=14,
            cursor="hand2",
        )
        self.printer_name = printer_name
        self.on_click = on_click
        self._accent = PALETTE["offline"]

        self.columnconfigure(1, weight=1)
        self.accent = tk.Frame(self, bg=self._accent, width=4)
        self.accent.grid(row=0, column=0, rowspan=5, sticky="ns", padx=(0, 12))

        header = tk.Frame(self, bg=PALETTE["surface"])
        header.grid(row=0, column=1, sticky="ew")
        header.columnconfigure(0, weight=1)

        self.name_label = tk.Label(
            header,
            text=printer_name,
            bg=PALETTE["surface"],
            fg=PALETTE["text"],
            font=("Segoe UI Semibold", 11),
            anchor="w",
        )
        self.name_label.grid(row=0, column=0, sticky="w")

        self.badge = tk.Label(
            header,
            text="Ожидание",
            bg="#E2E8F0",
            fg=PALETTE["offline"],
            font=("Segoe UI Semibold", 8),
            padx=8,
            pady=3,
        )
        self.badge.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.meta_label = tk.Label(
            self,
            text="Ещё не проверен",
            bg=PALETTE["surface"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.meta_label.grid(row=1, column=1, sticky="ew", pady=(5, 0))

        self.supplies_frame = tk.Frame(self, bg=PALETTE["surface"])
        self.supplies_frame.grid(row=2, column=1, sticky="ew", pady=(12, 2))

        self.detail_label = tk.Label(
            self,
            text="Подготавливаем проверку…",
            bg=PALETTE["surface"],
            fg=PALETTE["muted"],
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
        )
        self.detail_label.grid(row=3, column=1, sticky="ew", pady=(6, 0))

        self._bind_interactions(self)

    def _bind_interactions(self, widget: tk.Misc) -> None:
        widget.bind("<Button-1>", self._on_click, add="+")
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        for child in widget.winfo_children():
            self._bind_interactions(child)

    def _on_click(self, _event: tk.Event) -> None:
        self.on_click(self.printer_name)

    def _on_enter(self, _event: tk.Event) -> None:
        # Keep border width constant so moving the pointer between child widgets
        # does not change the card geometry and cause visible jitter.
        self.configure(highlightbackground=self._accent)

    def _on_leave(self, _event: tk.Event) -> None:
        self.configure(highlightbackground=PALETTE["line"])

    def set_loading(self, loading: bool) -> None:
        if loading:
            self.detail_label.configure(text="Проверка доступности…", fg=PALETTE["primary"])

    def set_status(self, status: PrinterStatus) -> None:
        severity = status.severity
        if status.reachable and severity == CardSeverity.UNKNOWN:
            severity = CardSeverity.UNKNOWN
        label, color, badge_bg = STATUS_META[severity]
        self._accent = color
        self.accent.configure(bg=color)
        self.badge.configure(text=label, fg=color, bg=badge_bg)

        ip_text = status.resolved_ip or "IP не определён"
        updated = status.updated_at.astimezone().strftime("%H:%M:%S")
        self.meta_label.configure(text=f"{ip_text}   •   обновлено {updated}")
        self._render_supplies(
            status.supplies,
            enabled=status.snmp_enabled,
            pending=status.snmp_pending,
        )

        diagnostic = _humanize_diagnostic(status.diagnostic)
        if status.snmp_pending:
            availability = diagnostic or (
                "Доступность определена" if status.reachable else "Веб-интерфейс недоступен"
            )
            detail = f"{availability} · получаем данные SNMP…"
        else:
            detail = diagnostic or status.summary_text or (
                "Веб-интерфейс доступен" if status.reachable else "Устройство недоступно"
            )
        self.detail_label.configure(
            text=detail,
            fg=(
                PALETTE["primary"]
                if status.snmp_pending
                else PALETTE["critical"] if not status.reachable else PALETTE["muted"]
            ),
        )

    def _render_supplies(
        self,
        supplies: tuple[SupplyLevel, ...],
        *,
        enabled: bool = True,
        pending: bool = False,
    ) -> None:
        for child in self.supplies_frame.winfo_children():
            child.destroy()

        if not enabled:
            self.supplies_frame.grid_remove()
            return
        self.supplies_frame.grid()

        if pending:
            loading = tk.Label(
                self.supplies_frame,
                text="Расходники: опрос SNMP…",
                bg=PALETTE["surface"],
                fg=PALETTE["primary"],
                font=("Segoe UI", 9),
                anchor="w",
            )
            loading.pack(fill="x")
            self._bind_interactions(loading)
            return

        target = [item for item in supplies if item.color in {"K", "C", "M", "Y"}]
        if not target:
            target = list(supplies[:4])
        if not target:
            empty = tk.Label(
                self.supplies_frame,
                text="Расходники: нет данных",
                bg=PALETTE["surface"],
                fg=PALETTE["muted"],
                font=("Segoe UI", 9),
                anchor="w",
            )
            empty.pack(fill="x")
            self._bind_interactions(empty)
            return

        colors = {"K": "#334155", "C": "#0891B2", "M": "#DB2777", "Y": "#CA8A04"}
        for supply in target[:4]:
            item = tk.Frame(self.supplies_frame, bg=PALETTE["surface"])
            item.pack(side="left", fill="x", expand=True, padx=(0, 8))
            label = supply.color or supply.name[:10]
            percent = "—" if supply.percent is None else f"{supply.percent}%"
            text = tk.Label(
                item,
                text=f"{label}  {percent}",
                bg=PALETTE["surface"],
                fg=PALETTE["text"],
                font=("Segoe UI Semibold", 8),
                anchor="w",
            )
            text.pack(fill="x")
            track = tk.Frame(item, bg=PALETTE["line"], height=4)
            track.pack(fill="x", pady=(4, 0))
            track.pack_propagate(False)
            fill = tk.Frame(track, bg=colors.get(supply.color or "", PALETTE["primary"]))
            fill.place(relx=0, rely=0, relheight=1, relwidth=max(0.02, (supply.percent or 0) / 100))
            self._bind_interactions(item)


class PrinterDashboard:
    def __init__(self, cfg: AppConfig, state_dir: Path):
        self.cfg = cfg
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.log_path = self.state_dir / "printer_manager.log"
        self.window_pos_path = self.state_dir / "window_position.txt"
        self.settings_path = self.state_dir / "settings.json"
        self._configure_logging()

        self.root = tk.Tk()
        self.root.title(self.cfg.title)
        self.root.configure(bg=PALETTE["page"])
        self.root.minsize(780, 520)
        self.root.geometry("1120x720")
        self._configure_styles()

        self.printers = self._sort_printers(self.cfg.printers)
        self.printers_path = resolve_printers_path(self.cfg.config_path)
        self.cards: dict[str, PrinterCard] = {}
        self._status_by_printer: dict[str, PrinterStatus] = {}
        self.grid_canvas: tk.Canvas | None = None
        self.grid_frame: tk.Frame | None = None
        self._grid_canvas_window: int | None = None

        self.snmp_config = self._load_snmp_settings()
        self.monitor = PrinterMonitor(timeout=self.cfg.timeout, snmp_config=self.snmp_config)
        worker_count = _reachability_worker_count(len(self.printers))
        self.executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="printer-monitor")
        snmp_worker_count = max(2, min(8, len(self.printers) or 2))
        self.snmp_executor = ThreadPoolExecutor(
            max_workers=snmp_worker_count,
            thread_name_prefix="printer-snmp",
        )
        self._events: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self._closing = False
        self._render_revision = 0
        self._active_refresh_revision = 0
        self._refresh_pending: set[str] = set()
        self._reachability_pending: set[str] = set()
        self._snmp_pending: set[str] = set()
        self._refresh_total = 0
        self._refresh_completed = 0
        self._reachability_completed = 0
        self._snmp_completed = 0
        self._snmp_requested = False
        self._refresh_started = 0.0
        self._queued_refresh = False
        self._sync_in_progress = False
        self._last_columns = 0
        self._reflow_job: str | None = None
        self._poll_job: str | None = None
        self._snmp_refresh_job: str | None = None
        self._server_sync_job: str | None = None

        self._build_ui()
        self._load_window_position()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.root.after(50, self._poll_worker_events)

    def _configure_logging(self) -> None:
        logging.basicConfig(
            filename=str(self.log_path),
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        )

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.root.option_add("*Font", ("Segoe UI", 10))
        style.configure("TButton", padding=(12, 7), borderwidth=0, focuscolor="")
        style.configure("Accent.TButton", background=PALETTE["primary"], foreground="white")
        style.map("Accent.TButton", background=[("active", PALETTE["primary_hover"]), ("disabled", "#93C5FD")])
        style.configure("Toolbar.TButton", background=PALETTE["surface"], foreground=PALETTE["text"])
        style.map("Toolbar.TButton", background=[("active", PALETTE["surface_muted"])])
        style.configure("Filter.TRadiobutton", background=PALETTE["surface"], padding=(10, 6), indicatorcolor=PALETTE["surface"])
        style.map(
            "Filter.TRadiobutton",
            background=[("selected", "#DBEAFE"), ("active", PALETTE["surface_muted"])],
            foreground=[("selected", PALETTE["primary"])],
        )
        style.configure("Horizontal.TProgressbar", troughcolor=PALETTE["line"], background=PALETTE["primary"], borderwidth=0)

    def _load_snmp_settings(self) -> SnmpConfig:
        if not self.settings_path.is_file():
            return SnmpConfig()
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            snmp = raw.get("snmp", {})
            return _validated_snmp_config(
                enabled=bool(snmp.get("enabled", True)),
                community=str(snmp.get("community", "public")),
                port=str(snmp.get("port", 161)),
                timeout=str(snmp.get("timeout", 1.2)),
                retries=str(snmp.get("retries", 1)),
                refresh_interval=str(snmp.get("refresh_interval", 300)),
                warning_threshold=str(snmp.get("warning_threshold", 20)),
                critical_threshold=str(snmp.get("critical_threshold", 10)),
            )
        except Exception as exc:
            logging.error("Failed to load settings: %s", exc)
            return SnmpConfig()

    def _save_snmp_settings(self) -> None:
        payload = {"snmp": asdict(self.snmp_config)}
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.settings_path.parent, delete=False
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            temp_path = Path(tmp.name)
        temp_path.replace(self.settings_path)

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=PALETTE["header"], padx=24, pady=18)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="PRINT MONITOR",
            bg=PALETTE["header"],
            fg="white",
            font=("Segoe UI Semibold", 17),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            text="Состояние устройств и расходных материалов",
            bg=PALETTE["header"],
            fg=PALETTE["header_muted"],
            font=("Segoe UI", 9),
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.refresh_button = ttk.Button(header, text="↻  Обновить", style="Accent.TButton", command=lambda: self.refresh_all(True))
        self.refresh_button.grid(row=0, column=1, rowspan=2, padx=(16, 8))
        ttk.Button(header, text="⚙  Настройки", style="Toolbar.TButton", command=self._open_settings).grid(row=0, column=2, rowspan=2)

        toolbar = tk.Frame(self.root, bg=PALETTE["surface"], padx=20, pady=12)
        toolbar.grid(row=1, column=0, sticky="ew")
        toolbar.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        search = ttk.Entry(toolbar, textvariable=self.search_var, font=("Segoe UI", 10))
        search.grid(row=0, column=0, sticky="ew", padx=(0, 18), ipady=5)
        self.search_var.trace_add("write", lambda *_: self._reflow_cards())

        self.filter_var = tk.StringVar(value="all")
        filters = (("Все", "all"), ("В сети", "online"), ("Внимание", "attention"), ("Не в сети", "offline"))
        for index, (title, value) in enumerate(filters, start=1):
            ttk.Radiobutton(
                toolbar,
                text=title,
                value=value,
                variable=self.filter_var,
                command=self._reflow_cards,
                style="Filter.TRadiobutton",
            ).grid(row=0, column=index, padx=(0, 3))

        summary = tk.Frame(self.root, bg=PALETTE["page"], padx=20, pady=14)
        summary.grid(row=2, column=0, sticky="ew")
        for column in range(4):
            summary.columnconfigure(column, weight=1, uniform="stats")
        self.stat_vars: dict[str, tk.StringVar] = {}
        for column, (key, title, color) in enumerate(
            (
                ("total", "Всего устройств", PALETTE["primary"]),
                ("online", "В сети", PALETTE["ok"]),
                ("attention", "Требуют внимания", PALETTE["warning"]),
                ("offline", "Не в сети", PALETTE["offline"]),
            )
        ):
            tile = tk.Frame(summary, bg=PALETTE["surface"], highlightbackground=PALETTE["line"], highlightthickness=1, padx=14, pady=10)
            tile.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0 if column == 3 else 5))
            var = tk.StringVar(value="0")
            self.stat_vars[key] = var
            tk.Label(tile, textvariable=var, bg=PALETTE["surface"], fg=color, font=("Segoe UI Semibold", 18)).pack(anchor="w")
            tk.Label(tile, text=title, bg=PALETTE["surface"], fg=PALETTE["muted"], font=("Segoe UI", 8)).pack(anchor="w")

        cards_container = tk.Frame(self.root, bg=PALETTE["page"])
        cards_container.grid(row=3, column=0, sticky="nsew", padx=(20, 12))
        cards_container.rowconfigure(0, weight=1)
        cards_container.columnconfigure(0, weight=1)
        self.grid_canvas = tk.Canvas(cards_container, bg=PALETTE["page"], highlightthickness=0, borderwidth=0)
        self.grid_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(cards_container, orient="vertical", command=self.grid_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.grid_canvas.configure(yscrollcommand=scrollbar.set)
        self.grid_frame = tk.Frame(self.grid_canvas, bg=PALETTE["page"])
        self._grid_canvas_window = self.grid_canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", self._on_cards_frame_configure, add="+")
        self.grid_canvas.bind("<Configure>", self._on_cards_canvas_configure, add="+")
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        footer = tk.Frame(self.root, bg=PALETTE["surface"], padx=20, pady=9, highlightbackground=PALETTE["line"], highlightthickness=1)
        footer.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Готово к проверке")
        tk.Label(footer, textvariable=self.status_var, bg=PALETTE["surface"], fg=PALETTE["muted"], font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=1, value=0, length=180)
        self.progress.grid(row=0, column=1, sticky="e")

        self.root.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)
        self._render_printer_cards()
        self._update_summary()
        self.refresh_all(force_snmp=True)
        self._schedule_snmp_refresh()
        self._schedule_server_sync(initial=True)

    def _render_printer_cards(self) -> None:
        if not self.grid_frame:
            return
        self._render_revision += 1
        wanted = set(self.printers)
        for name in list(self.cards):
            if name not in wanted:
                self.cards.pop(name).destroy()
                self._status_by_printer.pop(name, None)
        for name in self.printers:
            if name not in self.cards:
                self.cards[name] = PrinterCard(self.grid_frame, name, self._open_printer_web_interface)
                status = self._status_by_printer.get(name)
                if status:
                    self.cards[name].set_status(status)
        self._last_columns = 0
        self._reflow_cards()

    def _visible_printers(self) -> list[str]:
        query = self.search_var.get().strip().casefold() if hasattr(self, "search_var") else ""
        selected_filter = self.filter_var.get() if hasattr(self, "filter_var") else "all"
        visible: list[str] = []
        for name in self.printers:
            status = self._status_by_printer.get(name)
            if query and query not in name.casefold() and (not status or query not in (status.resolved_ip or "")):
                continue
            if selected_filter == "online" and (not status or not status.reachable or status.severity in {CardSeverity.WARNING, CardSeverity.CRITICAL}):
                continue
            if selected_filter == "attention" and (not status or status.severity not in {CardSeverity.WARNING, CardSeverity.CRITICAL}):
                continue
            if selected_filter == "offline" and (not status or status.reachable):
                continue
            visible.append(name)
        return visible

    def _reflow_cards(self) -> None:
        if not self.grid_frame or not self.grid_canvas:
            return
        columns = _responsive_columns(self.grid_canvas.winfo_width(), self.cfg.columns)
        visible = self._visible_printers()
        for card in self.cards.values():
            card.grid_forget()
        for column in range(max(self._last_columns, columns)):
            self.grid_frame.grid_columnconfigure(column, weight=0, minsize=0)
        for index, name in enumerate(visible):
            row, column = divmod(index, columns)
            self.cards[name].grid(row=row, column=column, sticky="nsew", padx=6, pady=6)
            self.grid_frame.grid_columnconfigure(column, weight=1, uniform="printer-cards")
        self._last_columns = columns
        self.root.after_idle(self._on_cards_frame_configure)

    def _on_cards_frame_configure(self, _event: tk.Event | None = None) -> None:
        if self.grid_canvas:
            bbox = self.grid_canvas.bbox("all")
            self.grid_canvas.configure(scrollregion=bbox or (0, 0, 0, 0))

    def _on_cards_canvas_configure(self, event: tk.Event) -> None:
        if not self.grid_canvas or self._grid_canvas_window is None:
            return
        self.grid_canvas.itemconfigure(self._grid_canvas_window, width=event.width)
        if self._reflow_job:
            self.root.after_cancel(self._reflow_job)
        self._reflow_job = self.root.after(80, self._reflow_cards)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if not self.grid_canvas:
            return
        widget = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
        while widget is not None:
            if widget == self.grid_canvas or widget == self.grid_frame:
                self.grid_canvas.yview_scroll(int(-event.delta / 120), "units")
                return
            widget = getattr(widget, "master", None)

    @staticmethod
    def _sort_printers(printers: list[str]) -> list[str]:
        unique: dict[str, str] = {}
        for name in printers:
            cleaned = name.strip()
            if cleaned:
                unique.setdefault(cleaned.casefold(), cleaned)
        return sorted(unique.values(), key=lambda name: name.casefold())

    def _open_printer_web_interface(self, printer_name: str) -> None:
        status = self._status_by_printer.get(printer_name)
        url = self.monitor.web_url(printer_name, status.resolved_ip if status else None)
        if not webbrowser.open(url, new=2):
            messagebox.showerror("Не удалось открыть", f"Откройте адрес вручную:\n{url}", parent=self.root)

    def refresh_all(self, force_snmp: bool = True) -> None:
        if self._closing:
            return
        if self._refresh_pending:
            self._queued_refresh = self._queued_refresh or force_snmp
            self.status_var.set("Текущая проверка завершается — повтор добавлен в очередь")
            return

        names = list(self.cards)
        if not names:
            self.status_var.set("Добавьте хотя бы один принтер в настройках")
            return

        self._render_revision += 1
        revision = self._render_revision
        self._active_refresh_revision = revision
        self._refresh_pending = set(names)
        self._reachability_pending = set(names)
        self._snmp_pending.clear()
        self._refresh_total = len(names)
        self._refresh_completed = 0
        self._reachability_completed = 0
        self._snmp_completed = 0
        self._snmp_requested = bool(force_snmp and self.snmp_config.enabled)
        self._refresh_started = time.monotonic()
        self.refresh_button.state(["disabled"])
        work_units = len(names) * (2 if self._snmp_requested else 1)
        self.progress.configure(maximum=work_units, value=0)
        self.status_var.set(f"Проверка доступности: 0 из {len(names)}")

        for name in names:
            self.cards[name].set_loading(True)
            future = self.executor.submit(
                self.monitor.build_reachability_status,
                name,
                self._snmp_requested,
            )
            future.add_done_callback(
                lambda done, n=name, rev=revision: self._queue_monitor_result(
                    done,
                    "reachability",
                    n,
                    rev,
                )
            )

    def _queue_monitor_result(
        self,
        future: Future,
        stage: str,
        printer_name: str,
        revision: int,
    ) -> None:
        try:
            self._events.put((stage, revision, printer_name, future.result(), None))
        except Exception as exc:
            self._events.put((stage, revision, printer_name, None, exc))

    def _poll_worker_events(self) -> None:
        if self._closing:
            return
        processed = 0
        while processed < 200:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            kind = event[0]
            if kind == "reachability":
                _, revision, name, status, error = event
                self._handle_reachability_result(revision, name, status, error)
            elif kind == "snmp":
                _, revision, name, status, error = event
                self._handle_snmp_result(revision, name, status, error)
            elif kind == "server_sync":
                _, names, error = event
                self._handle_server_sync_result(names, error)
        self._poll_job = self.root.after(50, self._poll_worker_events)

    def _handle_reachability_result(
        self,
        revision: int,
        printer_name: str,
        status: PrinterStatus | None,
        error: Exception | None,
    ) -> None:
        if revision != self._active_refresh_revision or printer_name not in self._reachability_pending:
            return
        self._reachability_pending.discard(printer_name)
        self._reachability_completed += 1
        if error:
            logging.error(
                "Reachability task crashed for %s: %s",
                printer_name,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            card = self.cards.get(printer_name)
            if card:
                card.detail_label.configure(text="Ошибка фоновой проверки", fg=PALETTE["critical"])
            self._refresh_pending.discard(printer_name)
            if self._snmp_requested:
                self._snmp_completed += 1
        elif status is not None:
            self._apply_status(printer_name, status, revision)

            if self._snmp_requested and status.snmp_pending:
                self._snmp_pending.add(printer_name)
                future = self.snmp_executor.submit(self.monitor.enrich_status_with_snmp, status)
                future.add_done_callback(
                    lambda done, n=printer_name, rev=revision: self._queue_monitor_result(
                        done,
                        "snmp",
                        n,
                        rev,
                    )
                )
            else:
                self._refresh_pending.discard(printer_name)
                if self._snmp_requested:
                    self._snmp_completed += 1

        self._refresh_completed = self._reachability_completed + self._snmp_completed
        self._update_refresh_progress()
        if not self._refresh_pending:
            self._finish_refresh()

    def _handle_snmp_result(
        self,
        revision: int,
        printer_name: str,
        status: PrinterStatus | None,
        error: Exception | None,
    ) -> None:
        if revision != self._active_refresh_revision or printer_name not in self._snmp_pending:
            return
        self._snmp_pending.discard(printer_name)
        self._refresh_pending.discard(printer_name)
        self._snmp_completed += 1
        self._refresh_completed = self._reachability_completed + self._snmp_completed

        if error:
            logging.error(
                "SNMP task crashed for %s: %s",
                printer_name,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            current = self._status_by_printer.get(printer_name)
            if current is not None:
                self._apply_status(
                    printer_name,
                    replace(
                        current,
                        snmp_pending=False,
                        summary_text="SNMP: ошибка опроса",
                        diagnostic=str(error),
                    ),
                    revision,
                )
        elif status is not None:
            self._apply_status(printer_name, status, revision)

        self._update_refresh_progress()
        if not self._refresh_pending:
            self._finish_refresh()

    def _update_refresh_progress(self) -> None:
        self.progress.configure(value=self._refresh_completed)
        if self._reachability_pending:
            if self._snmp_requested:
                self.status_var.set(
                    f"Доступность: {self._reachability_completed} из {self._refresh_total}"
                    f" · SNMP: {self._snmp_completed} из {self._refresh_total}"
                )
            else:
                self.status_var.set(
                    f"Проверка доступности: {self._reachability_completed} из {self._refresh_total}"
                )
        elif self._snmp_pending:
            self.status_var.set(
                f"Доступность показана · SNMP: {self._snmp_completed} из {self._refresh_total}"
            )

    def _apply_status(self, printer_name: str, status: PrinterStatus, revision: int) -> None:
        if revision != self._render_revision:
            return
        card = self.cards.get(printer_name)
        if not card:
            return
        self._status_by_printer[printer_name] = status
        card.set_status(status)
        self._update_summary()

    def _finish_refresh(self) -> None:
        elapsed = time.monotonic() - self._refresh_started
        self.refresh_button.state(["!disabled"])
        if self._snmp_requested:
            self.status_var.set(f"Доступность и SNMP обновлены за {elapsed:.1f} с")
        else:
            self.status_var.set(f"Доступность обновлена за {elapsed:.1f} с")
        self._update_summary()
        self._reflow_cards()
        if self._queued_refresh:
            self._queued_refresh = False
            self.root.after(100, lambda: self.refresh_all(True))

    def _update_summary(self) -> None:
        statuses = list(self._status_by_printer.values())
        attention = sum(item.severity in {CardSeverity.WARNING, CardSeverity.CRITICAL} for item in statuses)
        offline = sum(not item.reachable for item in statuses)
        online = sum(item.reachable and item.severity not in {CardSeverity.WARNING, CardSeverity.CRITICAL} for item in statuses)
        self.stat_vars["total"].set(str(len(self.printers)))
        self.stat_vars["online"].set(str(online))
        self.stat_vars["attention"].set(str(attention))
        self.stat_vars["offline"].set(str(offline))

    def _schedule_snmp_refresh(self) -> None:
        if self._snmp_refresh_job is not None:
            self.root.after_cancel(self._snmp_refresh_job)
            self._snmp_refresh_job = None
        if self.snmp_config.refresh_interval <= 0 or self._closing:
            return
        self._snmp_refresh_job = self.root.after(
            int(self.snmp_config.refresh_interval * 1000),
            self._run_scheduled_refresh,
        )

    def _run_scheduled_refresh(self) -> None:
        self._snmp_refresh_job = None
        self.refresh_all(force_snmp=True)
        self._schedule_snmp_refresh()

    def _schedule_server_sync(self, initial: bool = False) -> None:
        if not self.cfg.print_server or self.cfg.sync_interval <= 0 or self._closing:
            return
        if self._server_sync_job:
            self.root.after_cancel(self._server_sync_job)
        delay_ms = 1500 if initial else int(self.cfg.sync_interval * 1000)
        self._server_sync_job = self.root.after(delay_ms, self._sync_printers_from_server_async)

    def _sync_printers_from_server_async(self) -> None:
        self._server_sync_job = None
        if self._sync_in_progress or self._closing:
            self._schedule_server_sync()
            return
        self._sync_in_progress = True

        def worker() -> None:
            try:
                names = fetch_printers_from_server(self.cfg.print_server or "")
                self._events.put(("server_sync", names, None))
            except Exception as exc:
                self._events.put(("server_sync", [], exc))

        self.executor.submit(worker)

    def _handle_server_sync_result(self, names: list[str], error: Exception | None) -> None:
        self._sync_in_progress = False
        if error:
            if isinstance(error, subprocess.CalledProcessError):
                details = (error.stderr or "").strip() or str(error)
            else:
                details = str(error)
            logging.error("Print server sync failed for %s: %s", self.cfg.print_server, details)
        elif names:
            self._apply_server_printers(names)
        self._schedule_server_sync()

    def _apply_server_printers(self, names: list[str]) -> None:
        sorted_names = self._sort_printers(names)
        if sorted_names == self.printers:
            return
        self.printers = sorted_names
        self._render_printer_cards()
        self.refresh_all(force_snmp=True)
        self.status_var.set("Список принтеров синхронизирован с сервером")
        try:
            write_printers_file(self.printers_path, self.printers)
        except Exception as exc:
            logging.error("Не удалось сохранить список принтеров после синхронизации: %s", exc)

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Настройки Print Monitor")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("620x560")
        dialog.minsize(560, 500)
        dialog.configure(bg=PALETTE["page"])

        printers = self._sort_printers(self.printers)
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill="both", expand=True, padx=16, pady=(16, 10))
        printers_tab = ttk.Frame(notebook, padding=14)
        snmp_tab = ttk.Frame(notebook, padding=14)
        notebook.add(printers_tab, text="Принтеры")
        notebook.add(snmp_tab, text="Мониторинг SNMP")

        printers_tab.columnconfigure(0, weight=1)
        printers_tab.rowconfigure(1, weight=1)
        ttk.Label(printers_tab, text="По одному имени на строку. Дубликаты удаляются автоматически.").grid(row=0, column=0, sticky="w", pady=(0, 8))
        list_frame = ttk.Frame(printers_tab)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, activestyle="none", borderwidth=0, highlightthickness=1, highlightbackground=PALETTE["line"])
        scrollbar.configure(command=listbox.yview)
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        def update_listbox() -> None:
            listbox.delete(0, tk.END)
            for item in printers:
                listbox.insert(tk.END, item)

        def add_printer() -> None:
            raw = simpledialog.askstring("Добавить принтеры", "Введите имена через запятую, пробел или с новой строки:", parent=dialog)
            if raw is None:
                return
            names = [item.strip() for item in re.split(r"[,\s;]+", raw) if item.strip()]
            printers[:] = self._sort_printers([*printers, *names])
            update_listbox()

        def edit_printer() -> None:
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("Выбор", "Выберите принтер для редактирования.", parent=dialog)
                return
            current = printers[selection[0]]
            value = simpledialog.askstring("Редактировать", "Новое имя принтера:", initialvalue=current, parent=dialog)
            if value and value.strip():
                printers[selection[0]] = value.strip()
                printers[:] = self._sort_printers(printers)
                update_listbox()

        def remove_printer() -> None:
            for index in reversed(listbox.curselection()):
                printers.pop(index)
            update_listbox()

        actions = ttk.Frame(printers_tab)
        actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="Добавить", command=add_printer).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Изменить", command=edit_printer).pack(side="left", padx=6)
        ttk.Button(actions, text="Удалить", command=remove_printer).pack(side="left", padx=6)

        snmp_enabled = tk.BooleanVar(value=self.snmp_config.enabled)
        values = {
            "community": tk.StringVar(value=self.snmp_config.community),
            "port": tk.StringVar(value=str(self.snmp_config.port)),
            "timeout": tk.StringVar(value=str(self.snmp_config.timeout)),
            "retries": tk.StringVar(value=str(self.snmp_config.retries)),
            "interval": tk.StringVar(value=str(self.snmp_config.refresh_interval)),
            "warning": tk.StringVar(value=str(self.snmp_config.warning_threshold)),
            "critical": tk.StringVar(value=str(self.snmp_config.critical_threshold)),
        }
        snmp_tab.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            snmp_tab,
            text="Уточнять состояние и уровни расходников по SNMP",
            variable=snmp_enabled,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(
            snmp_tab,
            text="Если опция выключена, карточки показывают только доступность веб-интерфейса.",
            foreground=PALETTE["muted"],
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        fields = (
            ("Community", "community"),
            ("Порт", "port"),
            ("Таймаут, сек.", "timeout"),
            ("Повторы", "retries"),
            ("Интервал обновления, сек.", "interval"),
            ("Предупреждение, %", "warning"),
            ("Критический уровень, %", "critical"),
        )
        for row, (title, key) in enumerate(fields, start=2):
            ttk.Label(snmp_tab, text=title).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=6)
            ttk.Entry(snmp_tab, textvariable=values[key]).grid(row=row, column=1, sticky="ew", pady=6)

        def save_and_close() -> None:
            try:
                new_config = _validated_snmp_config(
                    enabled=snmp_enabled.get(),
                    community=values["community"].get(),
                    port=values["port"].get(),
                    timeout=values["timeout"].get(),
                    retries=values["retries"].get(),
                    refresh_interval=values["interval"].get(),
                    warning_threshold=values["warning"].get(),
                    critical_threshold=values["critical"].get(),
                )
                cleaned = self._sort_printers(printers)
                write_printers_file(self.printers_path, cleaned)
                self.snmp_config = new_config
                self.monitor = PrinterMonitor(timeout=self.cfg.timeout, snmp_config=self.snmp_config)
                self._save_snmp_settings()
            except Exception as exc:
                logging.error("Ошибка сохранения настроек: %s", exc)
                messagebox.showerror("Проверьте настройки", str(exc), parent=dialog)
                return

            self.printers = cleaned
            self._render_printer_cards()
            self._schedule_snmp_refresh()
            self.refresh_all(force_snmp=True)
            dialog.destroy()

        controls = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        controls.pack(fill="x")
        ttk.Button(controls, text="Сохранить", style="Accent.TButton", command=save_and_close).pack(side="right")
        ttk.Button(controls, text="Отмена", command=dialog.destroy).pack(side="right", padx=(0, 8))
        update_listbox()
        dialog.wait_window()

    def _on_close(self) -> None:
        self._closing = True
        self._save_window_position()
        for job in (self._poll_job, self._snmp_refresh_job, self._server_sync_job, self._reflow_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.snmp_executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def _save_window_position(self) -> None:
        try:
            self.window_pos_path.write_text(self.root.geometry(), encoding="utf-8")
        except Exception as exc:
            logging.error("Ошибка при сохранении позиции окна: %s", exc)

    def _load_window_position(self) -> None:
        try:
            if self.window_pos_path.is_file():
                geometry = self.window_pos_path.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"\d+x\d+(?:[+-]\d+){2}", geometry):
                    self.root.geometry(geometry)
        except Exception as exc:
            logging.error("Ошибка при загрузке позиции окна: %s", exc)

    def run(self) -> None:
        self.root.mainloop()
