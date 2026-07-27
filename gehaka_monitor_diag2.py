import importlib
import json
import os
import socket
import subprocess
import tempfile
import threading
import textwrap
import time
import ctypes
import sys
from datetime import datetime
from tkinter import ttk
import tkinter as tk
from tkinter import messagebox
import serial
import serial.tools.list_ports

try:
    import pystray
except Exception:
    pystray = None

try:
    from PIL import Image, ImageDraw
except Exception:
    Image = None
    ImageDraw = None

DEFAULT_BAUD_RATE = 115200
DEFAULT_PRINTER_MODE = "Compartilhada (TXT/SMB)"
DEFAULT_SHARED_PRINTER_PATH = r"\\192.1.2.43\MP4200"
RECONNECT_INTERVAL_SECONDS = 5.0
DEFAULT_SERIAL_READ_TIMEOUT_SECONDS = 0.5
DEFAULT_PACKET_GAP_TIMEOUT_SECONDS = 1.5
TICKET_WIDTH_80MM = 42
TICKET_SEPARATOR = "=" * 38


def get_runtime_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resolve_resource_path(filename):
    search_paths = [get_runtime_base_dir()]
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        search_paths.append(bundle_dir)

    for base_path in search_paths:
        candidate = os.path.join(base_path, filename)
        if os.path.exists(candidate):
            return candidate

    return os.path.join(get_runtime_base_dir(), filename)


PROJECT_ROOT = get_runtime_base_dir()
CONFIG_FILENAME = "config.json"
GENERATED_TICKET_FILENAME = "TicketGerado.txt"
GENERATED_TICKET_PREVIEW_FILENAME = "TicketGerado_preview.txt"
TICKET_STORAGE_DIR = os.path.join(PROJECT_ROOT, "tickets")
MAX_STORED_TICKETS = 5
LOG_STORAGE_DIR = os.path.join(PROJECT_ROOT, "logs")
MAX_RUNTIME_LOG_FILES = 10
MAIN_BG_COLOR = "#eef3f7"
PANEL_BG_COLOR = "#ffffff"
TITLE_COLOR = "#1f3b57"
APP_USER_MODEL_ID = "Gehaka.G2000.Monitor"
STARTUP_WINDOW_MODE = "hidden"

MEASUREMENT_FIELD_NAMES = (
    "Número da Amostra",
    "Umidade (%)",
    "Peso (g)",
    "Densidade",
    "Temperatura da Amostra (°C)",
    "Temperatura do Instrumento (°C)",
    "Capacitância",
    "Nome do Grão",
    "Número da Curva",
    "Validade da Curva",
    "Modelo do Instrumento",
    "Versão do Firmware",
    "Versão do Hardware",
    "Número de Série",
    "Hora",
    "Data",
    "Hash",
    "Assinatura",
)

win32print = None
try:
    win32print = importlib.import_module("win32print")
except Exception:
    win32print = None


def set_windows_app_user_model_id():
    if os.name != "nt":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

class GehakaMonitorBridgeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GEHAKA G2000")
        self.root.geometry("820x560")
        self.root.resizable(False, False)

        self.serial_port = None
        self.read_thread = None
        self.stop_thread = False
        self.total_bytes = 0
        self.measurements_received = 0
        self.tickets_printed = 0
        self.print_errors = 0
        self.packet_buffer = bytearray()
        self.last_data_time = time.monotonic()
        self.runtime_log_filename = None
        self.icon_image = None
        self.tray_icon = None
        self.tray_enabled = False
        self.is_app_exiting = False
        self.maintenance_window = None
        self.reconnect_job = None
        self.current_port_name = None
        self.maintenance_log_lines = []
        self.ticket_rotation_index = 0
        self.last_ticket_text = ""
        self.available_printers = []
        self.printer_lookup = {}
        self.default_windows_printer = ""
        self.config_path = os.path.join(PROJECT_ROOT, CONFIG_FILENAME)
        self.config_data = self.load_config()
        self.initial_setup_pending = False
        self.manual_port_override = (self.config_data.get("com_port") or self.config_data.get("manual_port_override") or "").strip()
        self.manual_baud_rate = self.parse_int_value(self.config_data.get("baudrate", self.config_data.get("manual_baud_rate")), DEFAULT_BAUD_RATE)
        self.serial_read_timeout_seconds = self.parse_float_value(
            self.config_data.get("serial_read_timeout_seconds", self.config_data.get("read_timeout_seconds")),
            DEFAULT_SERIAL_READ_TIMEOUT_SECONDS,
        )
        self.packet_gap_timeout_seconds = self.parse_float_value(
            self.config_data.get("packet_gap_timeout_seconds", self.config_data.get("packet_timeout_seconds")),
            DEFAULT_PACKET_GAP_TIMEOUT_SECONDS,
        )
        self.printer_mode_var = tk.StringVar(value=self.map_config_printer_mode_to_ui(self.config_data.get("printer_mode")))
        initial_printer_value = (
            self.config_data.get("printer_path")
            or self.config_data.get("preferred_printer")
            or self.config_data.get("last_printer_path")
            or DEFAULT_SHARED_PRINTER_PATH
        )
        self.printer_name_var = tk.StringVar(value=initial_printer_value)
        self.printer_ip_var = tk.StringVar(value=(self.config_data.get("printer_ip") or "").strip())
        self.printer_port_var = tk.StringVar(value=str(self.config_data.get("printer_port") or "").strip())

        self.ensure_ticket_storage_dir()
        self.initialize_ticket_rotation_index()
        self.start_runtime_log()
        self.apply_window_icon()
        self.root.report_callback_exception = self.handle_tk_exception
        threading.excepthook = self.handle_thread_exception

        self.build_ui()
        self.init_system_tray()
        self.load_local_printers()
        self.root.bind("<Control-Shift-F12>", self.toggle_maintenance_window)
        self.root.bind("<Control-Shift-M>", self.toggle_main_window_visibility)
        self.root.after(250, self.check_printer_status)
        self.root.after(500, self.auto_connect)
        self.root.after(200, self.start_in_background)
        self.root.after(900, self.trigger_initial_setup_if_needed)

    def trigger_initial_setup_if_needed(self):
        self.initial_setup_pending = self.is_initial_setup_needed()
        if not self.initial_setup_pending:
            return

        self.toggle_maintenance_window()
        messagebox.showinfo(
            "Configuração inicial",
            "Primeira configuração necessária:\n"
            "1) Escolha a Porta COM\n"
            "2) Configure a impressora\n"
            "3) Clique em Aplicar ajustes e teste a impressão",
            parent=self.maintenance_window,
        )
        self.append_maintenance_log("Assistente inicial: configuração obrigatória exibida para o técnico.")

    def is_initial_setup_needed(self):
        com_port = (self.manual_port_override or "").strip()
        printer_target = (self.printer_name_var.get() or "").strip()
        return not com_port or not printer_target

    def set_guided_error(self, public_message, technical_detail=None):
        self.last_error_var.set(public_message)
        if technical_detail:
            self.append_maintenance_log(f"Detalhe técnico: {technical_detail}")

    def friendly_error_message(self, context, raw_error):
        error_text = (raw_error or "").lower()
        if context == "serial_connect":
            if "access is denied" in error_text or "perm" in error_text:
                return "Porta COM ocupada por outro programa. Feche outros aplicativos seriais e tente novamente."
            if "could not open port" in error_text or "file not found" in error_text:
                return "Porta COM não disponível. Verifique cabo/USB e confirme a porta na manutenção."
            return "Falha ao conectar no G2000. Verifique cabo USB e configuração da porta COM."

        if context == "printer_tcp":
            if "timed out" in error_text:
                return "Impressora TCP sem resposta. Verifique IP/porta e rede local."
            if "refused" in error_text:
                return "Conexão TCP recusada. Confira se a impressora está ligada e a porta está correta."
            return "Falha na impressão TCP. Verifique IP, porta e conectividade de rede."

        if context == "printer_share":
            if "access is denied" in error_text:
                return "Sem permissão para a impressora compartilhada. Verifique usuário e permissões no compartilhamento."
            if "network path was not found" in error_text or "path not found" in error_text:
                return "Compartilhamento não encontrado. Verifique nome do servidor e da impressora."
            return "Falha ao enviar para impressora compartilhada. Verifique compartilhamento e rede."

        return "Ocorreu uma falha. Consulte o log da manutenção para detalhes técnicos."

    def open_folder_in_explorer(self, folder_path, label):
        try:
            os.makedirs(folder_path, exist_ok=True)
            if os.name == "nt":
                os.startfile(folder_path)
            else:
                subprocess.Popen(["xdg-open", folder_path])
            self.append_maintenance_log(f"Pasta de {label} aberta: {folder_path}")
        except Exception as exc:
            self.set_guided_error(f"Não foi possível abrir a pasta de {label}.", str(exc))

    def start_in_background(self):
        if STARTUP_WINDOW_MODE == "hidden":
            self.hide_main_window()
            self.append_maintenance_log("Aplicação iniciada oculta em segundo plano.")
            return

        self.root.iconify()
        self.append_maintenance_log("Aplicação iniciada minimizada em segundo plano.")

    def hide_main_window(self):
        self.root.withdraw()

    def show_main_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def toggle_main_window_visibility(self, event=None):
        current_state = str(self.root.state())
        if current_state in ("iconic", "withdrawn"):
            self.show_main_window()
            self.append_maintenance_log("Janela principal restaurada (Ctrl+Shift+M).")
            return

        self.hide_main_window()
        self.append_maintenance_log("Janela principal ocultada para bandeja (Ctrl+Shift+M).")

    def init_system_tray(self):
        if os.name != "nt":
            self.write_runtime_log("System Tray habilitado apenas no Windows.")
            return

        if pystray is None or Image is None:
            self.write_runtime_log("pystray/Pillow indisponíveis. System Tray desabilitado.")
            return

        tray_image = self.load_tray_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem("Abrir painel", self.tray_show_main),
            pystray.MenuItem("Ocultar painel", self.tray_hide_main),
            pystray.MenuItem("Manutenção", self.tray_open_maintenance),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", self.tray_exit_app),
        )

        self.tray_icon = pystray.Icon("gehaka_g2000", tray_image, "GEHAKA G2000", menu)
        self.tray_icon.run_detached()
        self.tray_enabled = True
        self.append_maintenance_log("System Tray iniciado com sucesso.")

    def load_tray_icon_image(self):
        icon_path = resolve_resource_path("Icon.png")
        if Image is not None and os.path.exists(icon_path):
            try:
                return Image.open(icon_path)
            except Exception as exc:
                self.write_runtime_log(f"Falha ao carregar Icon.png para tray: {exc}")

        # Fallback simples caso o ícone não possa ser carregado.
        fallback = Image.new("RGBA", (64, 64), "#1f3b57") if Image is not None else None
        if fallback is not None and ImageDraw is not None:
            draw = ImageDraw.Draw(fallback)
            draw.rectangle((8, 8, 56, 56), outline="#ffffff", width=3)
            draw.text((22, 18), "G", fill="#ffffff")
        return fallback

    def tray_show_main(self, icon, item):
        self.root.after(0, self.show_main_window)
        self.root.after(0, lambda: self.append_maintenance_log("Janela principal restaurada via System Tray."))

    def tray_hide_main(self, icon, item):
        self.root.after(0, self.hide_main_window)
        self.root.after(0, lambda: self.append_maintenance_log("Janela principal ocultada via System Tray."))

    def tray_open_maintenance(self, icon, item):
        self.root.after(0, self.toggle_maintenance_window)
        self.root.after(0, self.show_main_window)
        self.root.after(0, lambda: self.append_maintenance_log("Tela de manutenção aberta via System Tray."))

    def tray_exit_app(self, icon, item):
        self.root.after(0, self.shutdown_application)

    def on_main_window_close(self):
        if self.tray_enabled and not self.is_app_exiting:
            self.hide_main_window()
            self.append_maintenance_log("Janela fechada para bandeja. Use o ícone do System Tray para reabrir.")
            return
        self.shutdown_application()

    def shutdown_application(self):
        if self.is_app_exiting:
            return
        self.is_app_exiting = True
        self.append_maintenance_log("Encerrando aplicação...")

        self.disconnect(update_ui=False)

        if self.read_thread and self.read_thread.is_alive() and threading.current_thread() is not self.read_thread:
            try:
                # Wait briefly for the reader loop to observe stop flag and exit cleanly.
                self.read_thread.join(timeout=1.5)
            except Exception:
                pass
        self.read_thread = None

        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

        self.root.destroy()

    def ensure_ticket_storage_dir(self):
        os.makedirs(TICKET_STORAGE_DIR, exist_ok=True)

    def initialize_ticket_rotation_index(self):
        latest_slot = 0
        latest_mtime = 0.0
        for slot in range(1, MAX_STORED_TICKETS + 1):
            path = os.path.join(TICKET_STORAGE_DIR, f"TicketGerado_{slot:02d}.txt")
            if not os.path.exists(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_slot = slot

        self.ticket_rotation_index = latest_slot

    def start_runtime_log(self):
        os.makedirs(LOG_STORAGE_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.runtime_log_filename = os.path.join(LOG_STORAGE_DIR, f"execucao_{timestamp}.log")
        self.write_runtime_log("Inicialização do monitor GEHAKA G2000")
        self.write_runtime_log(f"Pasta de logs: {LOG_STORAGE_DIR}")
        self.write_runtime_log(f"Pasta de tickets: {TICKET_STORAGE_DIR}")
        removed_count = self.cleanup_runtime_logs()
        if removed_count:
            self.write_runtime_log(f"Retenção de logs aplicada: {removed_count} arquivo(s) antigo(s) removido(s)")

    def cleanup_runtime_logs(self):
        try:
            all_logs = []
            for name in os.listdir(LOG_STORAGE_DIR):
                if not (name.startswith("execucao_") and name.endswith(".log")):
                    continue
                file_path = os.path.join(LOG_STORAGE_DIR, name)
                try:
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    continue
                all_logs.append((mtime, file_path))

            all_logs.sort(reverse=True)
            removed = 0
            for _, old_path in all_logs[MAX_RUNTIME_LOG_FILES:]:
                try:
                    os.remove(old_path)
                    removed += 1
                except OSError:
                    continue

            return removed
        except Exception:
            return 0

    def apply_window_icon(self):
        icon_path = resolve_resource_path("Icon.png")
        if not os.path.exists(icon_path):
            self.write_runtime_log("Icon.png não encontrado; mantendo ícone padrão.")
            return

        try:
            self.icon_image = tk.PhotoImage(file=icon_path)
            self.root.iconphoto(True, self.icon_image)
            try:
                self.root.wm_iconphoto(True, self.icon_image)
            except Exception:
                pass
            self.write_runtime_log(f"Ícone da aplicação carregado: {icon_path}")
        except Exception as exc:
            self.write_runtime_log(f"Falha ao carregar Icon.png: {exc}")

    def write_runtime_log(self, message):
        if not self.runtime_log_filename:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.runtime_log_filename, "a", encoding="utf-8") as log_file:
                log_file.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass

    def handle_thread_exception(self, args):
        exc_name = type(args.exc_value).__name__ if args.exc_value else "Erro"
        self.write_runtime_log(f"Exceção em thread ({args.thread.name}): {exc_name}: {args.exc_value}")

    def handle_tk_exception(self, exc, value, tb):
        self.write_runtime_log(f"Exceção na UI: {exc.__name__}: {value}")

    def parse_int_value(self, value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def parse_float_value(self, value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def map_config_printer_mode_to_ui(self, mode_value):
        return "Rede (TCP/IP)" if str(mode_value).strip().lower() == "tcp" else DEFAULT_PRINTER_MODE

    def map_ui_printer_mode_to_config(self, mode_value):
        return "tcp" if mode_value == "Rede (TCP/IP)" else "shared"

    def parse_printer_port_value(self):
        return self.parse_int_value(self.printer_port_var.get().strip(), 0)

    def infer_printer_name_from_target(self, target_value):
        normalized_target = (target_value or "").strip()
        if not normalized_target:
            return ""

        resolved_target = self.resolve_printer_target(normalized_target).lower()
        normalized_raw = normalized_target.lower()

        for entry in self.available_printers:
            printer_name = (entry.get("name") or "").strip()
            if not printer_name:
                continue

            unc_path = (entry.get("unc_path") or "").strip().lower()
            auto_path = self.build_shared_path_for_printer(entry).lower()
            if normalized_raw == printer_name.lower():
                return printer_name
            if resolved_target and (resolved_target == unc_path or resolved_target == auto_path):
                return printer_name

        return ""

    def get_current_printer_name(self):
        if hasattr(self, "maintenance_printer_var"):
            selected_name = (self.maintenance_printer_var.get() or "").strip()
            if selected_name:
                return selected_name

        inferred = self.infer_printer_name_from_target(self.printer_name_var.get().strip())
        if inferred:
            return inferred

        return (self.default_windows_printer or "").strip()

    def send_windows_test_page(self, printer_name):
        safe_name = (printer_name or "").strip()
        if not safe_name:
            return False, "Nenhuma impressora selecionada para teste do Windows"

        try:
            completed = subprocess.run(
                ["rundll32.exe", "printui.dll,PrintUIEntry", "/k", "/n", safe_name],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return False, str(exc)

        if completed.returncode == 0:
            return True, ""

        details = completed.stderr.strip() or completed.stdout.strip() or "Falha ao enviar página de teste do Windows"
        return False, details

    def load_config(self):
        if not os.path.exists(self.config_path):
            return {}

        try:
            with open(self.config_path, "r", encoding="utf-8") as config_file:
                loaded = json.load(config_file)
                return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def save_config(self):
        computer_name = os.environ.get("COMPUTERNAME", "").strip()
        computer_ip = self.get_local_ip_address()
        printer_name = self.get_current_printer_name()
        printer_path = self.printer_name_var.get().strip()

        config_payload = {
            "com_port": self.manual_port_override,
            "baudrate": self.manual_baud_rate,
            "printer_mode": self.map_ui_printer_mode_to_config(self.printer_mode_var.get()),
            "printer_name": printer_name,
            "printer_path": printer_path,
            "computer_name": computer_name,
            "computer_ip": computer_ip,
            "printer_ip": self.printer_ip_var.get().strip(),
            "printer_port": self.printer_port_var.get().strip(),
            "serial_read_timeout_seconds": self.serial_read_timeout_seconds,
            "packet_gap_timeout_seconds": self.packet_gap_timeout_seconds,
        }
        try:
            with open(self.config_path, "w", encoding="utf-8") as config_file:
                json.dump(config_payload, config_file, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.append_maintenance_log(f"Falha ao salvar configuração: {exc}")

    def build_ui(self):
        self.root.config(bg=MAIN_BG_COLOR)
        self.root.protocol("WM_DELETE_WINDOW", self.on_main_window_close)

        title = tk.Label(
            self.root,
            text="GEHAKA G2000",
            font=("Segoe UI", 17, "bold"),
            bg=MAIN_BG_COLOR,
            fg=TITLE_COLOR,
        )
        title.pack(pady=(10, 6))

        status_frame = tk.LabelFrame(self.root, text="Status", bg=PANEL_BG_COLOR, padx=10, pady=8)
        status_frame.pack(fill="x", padx=12, pady=(0, 8))

        tk.Label(status_frame, text="Status G2000", font=("Segoe UI", 10, "bold"), bg=PANEL_BG_COLOR).grid(row=0, column=0, padx=(0, 10), pady=4, sticky="w")
        self.g2000_status_var = tk.StringVar(value="� Não encontrado")
        self.g2000_status_label = tk.Label(status_frame, textvariable=self.g2000_status_var, font=("Segoe UI", 10), bg=PANEL_BG_COLOR, fg="#7a7a7a")
        self.g2000_status_label.grid(row=0, column=1, padx=(0, 20), pady=4, sticky="w")

        tk.Label(status_frame, text="Status Impressora", font=("Segoe UI", 10, "bold"), bg=PANEL_BG_COLOR).grid(row=0, column=2, padx=(0, 10), pady=4, sticky="w")
        self.printer_status_var = tk.StringVar(value="🔴 Não disponível")
        self.printer_status_label = tk.Label(status_frame, textvariable=self.printer_status_var, font=("Segoe UI", 10), bg=PANEL_BG_COLOR, fg="#7a7a7a")
        self.printer_status_label.grid(row=0, column=3, padx=(0, 20), pady=4, sticky="w")

        tk.Label(status_frame, text="Última impressão", font=("Segoe UI", 10, "bold"), bg=PANEL_BG_COLOR).grid(row=1, column=0, padx=(0, 10), pady=4, sticky="w")
        self.last_print_var = tk.StringVar(value="Nenhum")
        tk.Label(status_frame, textvariable=self.last_print_var, font=("Segoe UI", 10), bg=PANEL_BG_COLOR).grid(row=1, column=1, padx=(0, 20), pady=4, sticky="w")

        tk.Label(status_frame, text="Último erro", font=("Segoe UI", 10, "bold"), bg=PANEL_BG_COLOR).grid(row=1, column=2, padx=(0, 10), pady=4, sticky="w")
        self.last_error_var = tk.StringVar(value="Nenhum")
        tk.Label(status_frame, textvariable=self.last_error_var, font=("Segoe UI", 10), bg=PANEL_BG_COLOR).grid(row=1, column=3, padx=(0, 20), pady=4, sticky="w")

        measurement_frame = tk.LabelFrame(self.root, text="Última medição", bg=PANEL_BG_COLOR, padx=10, pady=8)
        measurement_frame.pack(fill="both", padx=12, pady=(0, 8))
        self.measurement_text = tk.Text(measurement_frame, height=10, width=78, wrap="word", font=("Consolas", 10), relief="flat", bg="#fbfdff")
        self.measurement_text.pack(fill="both", expand=True)
        self.measurement_text.insert(tk.END, "Aguardando medição...\n")
        self.measurement_text.configure(state="disabled")

        actions_frame = tk.Frame(self.root, bg=MAIN_BG_COLOR)
        actions_frame.pack(fill="x", padx=12, pady=(0, 8))
        self.reprint_button = tk.Button(
            actions_frame,
            text="Reimprimir última medição",
            font=("Segoe UI", 10, "bold"),
            bg="#2d6ea5",
            fg="#ffffff",
            activebackground="#23557f",
            activeforeground="#ffffff",
            relief="flat",
            padx=10,
            pady=5,
            command=self.reprint_last_measurement,
        )
        self.reprint_button.pack(side="right")

        counter_frame = tk.LabelFrame(self.root, text="Contadores", bg=PANEL_BG_COLOR, padx=10, pady=8)
        counter_frame.pack(fill="x", padx=12, pady=(0, 8))

        tk.Label(counter_frame, text="Medições recebidas", font=("Segoe UI", 10, "bold"), bg=PANEL_BG_COLOR).grid(row=0, column=0, padx=(0, 20), pady=4, sticky="w")
        self.measurements_count_var = tk.StringVar(value="0")
        tk.Label(counter_frame, textvariable=self.measurements_count_var, font=("Segoe UI", 10), bg=PANEL_BG_COLOR).grid(row=0, column=1, padx=(0, 28), pady=4, sticky="w")

        tk.Label(counter_frame, text="Tickets impressos", font=("Segoe UI", 10, "bold"), bg=PANEL_BG_COLOR).grid(row=0, column=2, padx=(0, 20), pady=4, sticky="w")
        self.tickets_count_var = tk.StringVar(value="0")
        tk.Label(counter_frame, textvariable=self.tickets_count_var, font=("Segoe UI", 10), bg=PANEL_BG_COLOR).grid(row=0, column=3, padx=(0, 28), pady=4, sticky="w")

        tk.Label(counter_frame, text="Erros de impressão", font=("Segoe UI", 10, "bold"), bg=PANEL_BG_COLOR).grid(row=0, column=4, padx=(0, 20), pady=4, sticky="w")
        self.errors_count_var = tk.StringVar(value="0")
        tk.Label(counter_frame, textvariable=self.errors_count_var, font=("Segoe UI", 10), bg=PANEL_BG_COLOR).grid(row=0, column=5, pady=4, sticky="w")

        self.status_label = tk.StringVar(value="Aguardando nova medição...")
        tk.Label(self.root, textvariable=self.status_label, bg=MAIN_BG_COLOR, font=("Segoe UI", 10, "bold"), fg="#2c3f52").pack(fill="x", padx=12, pady=(0, 8))
        self.set_g2000_status_color("waiting")
        self.set_printer_status_color("waiting")

    def reprint_last_measurement(self):
        if not self.last_ticket_text.strip():
            self.last_error_var.set("Sem medição disponível para reimpressão")
            self.status_label.set("Nenhuma medição disponível para reimprimir.")
            self.append_maintenance_log("Reimpressão solicitada sem medição disponível.")
            return

        self.status_label.set("Reimprimindo última medição...")
        self.append_maintenance_log("Reimpressão manual da última medição solicitada.")
        self.print_measurement(self.last_ticket_text)

    def set_status_color(self, label, state):
        if state == "connected":
            color = "#0f9d58"
        elif state == "error":
            color = "#d93025"
        else:
            color = "#7a7a7a"

        label.configure(fg=color)

    def set_g2000_status_color(self, state):
        self.set_status_color(self.g2000_status_label, state)

    def set_printer_status_color(self, state):
        self.set_status_color(self.printer_status_label, state)

    def load_ports(self):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        return ports

    def get_port_inventory(self):
        inventory = []
        for port in serial.tools.list_ports.comports():
            description = (port.description or "").strip()
            hwid = (port.hwid or "").strip()
            display = f"{port.device} | {description} | {hwid}"
            inventory.append({"device": port.device, "display": display})
        return inventory

    def get_local_ip_address(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "Indisponível"

    def extract_port_device(self, selected_value):
        raw = (selected_value or "").strip()
        if not raw:
            return ""
        return raw.split(" | ", 1)[0].strip()

    def build_printer_combo_values(self):
        values = []
        for entry in self.available_printers:
            printer_name = (entry.get("name") or "").strip()
            if printer_name:
                values.append(printer_name)
        # Keep insertion order while removing duplicates.
        return list(dict.fromkeys(values))

    def extract_printer_target(self, selected_value):
        raw = (selected_value or "").strip()
        if not raw:
            return ""
        if " | " in raw:
            return raw.split(" | ", 1)[1].strip()
        return raw

    def find_printer_entry_by_name(self, printer_name):
        target_name = (printer_name or "").strip().lower()
        if not target_name:
            return None
        for entry in self.available_printers:
            if (entry.get("name") or "").strip().lower() == target_name:
                return entry
        return None

    def build_shared_path_for_printer(self, entry):
        if not entry:
            return ""

        share_name = (entry.get("share_name") or "").strip()
        unc_path = (entry.get("unc_path") or "").strip()
        server_name = (entry.get("server_name") or "").strip().lstrip("\\")
        local_name = os.environ.get("COMPUTERNAME", "").strip().lower()

        if share_name:
            if not server_name or server_name.lower() == local_name:
                host_name = self.get_local_ip_address()
            else:
                host_name = server_name
            return f"\\\\{host_name}\\{share_name}"

        if unc_path.startswith("\\\\"):
            return unc_path

        return ""

    def on_maintenance_printer_selected(self, event=None):
        selected_name = (self.maintenance_printer_var.get() or "").strip()
        entry = self.find_printer_entry_by_name(selected_name)
        auto_path = self.build_shared_path_for_printer(entry)

        if auto_path:
            self.maintenance_printer_path_var.set(auto_path)
            self.maintenance_printer_notice_var.set("Compartilhamento detectado automaticamente.")
            self.append_maintenance_log(f"Impressora selecionada: {selected_name} -> {auto_path}")
            return

        self.maintenance_printer_notice_var.set("A impressora não está compartilhada. Informe o caminho manualmente.")
        self.last_error_var.set("Impressora sem compartilhamento detectado no Windows")
        self.append_maintenance_log(f"Impressora selecionada sem compartilhamento: {selected_name}")
        messagebox.showwarning(
            "Impressora sem compartilhamento",
            "Esta impressora não possui compartilhamento ativo no Windows.\nInforme o caminho manualmente no campo Caminho.",
            parent=self.maintenance_window,
        )

    def refresh_maintenance_lists(self):
        self.load_local_printers()
        self.append_maintenance_log("Listas de portas COM e impressoras atualizadas.")
        self.refresh_maintenance_window()

    def load_local_printers(self):
        printer_entries = self.get_windows_printer_inventory()
        self.available_printers = printer_entries
        self.printer_lookup = {}
        self.default_windows_printer = ""

        for entry in printer_entries:
            for key in (entry["name"], entry["share_name"], entry["unc_path"]):
                normalized_key = (key or "").strip().lower()
                if normalized_key:
                    self.printer_lookup[normalized_key] = entry
            if entry.get("is_default"):
                self.default_windows_printer = entry["name"]

        configured_target = self.printer_name_var.get().strip()
        if not configured_target:
            if self.default_windows_printer:
                self.printer_name_var.set(self.default_windows_printer)
            elif printer_entries:
                self.printer_name_var.set(printer_entries[0]["unc_path"] or printer_entries[0]["name"])
            else:
                self.printer_name_var.set(DEFAULT_SHARED_PRINTER_PATH)

        printer_labels = ", ".join(entry["name"] for entry in printer_entries[:5])
        if printer_labels:
            self.append_maintenance_log(f"Impressoras detectadas: {printer_labels}")
        if self.default_windows_printer:
            self.append_maintenance_log(f"Impressora padrão do Windows: {self.default_windows_printer}")
        self.append_maintenance_log(f"Destino de impressão configurado: {self.printer_name_var.get()}")
        self.save_config()

    def get_windows_printer_inventory(self):
        entries = self.get_windows_printer_inventory_via_win32print()
        if entries:
            return entries
        return self.get_windows_printer_inventory_via_powershell()

    def get_windows_printer_inventory_via_win32print(self):
        if win32print is None:
            return []

        entries = []
        default_printer_name = ""
        try:
            default_printer_name = (win32print.GetDefaultPrinter() or "").strip()
        except Exception:
            default_printer_name = ""

        try:
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            raw_printers = win32print.EnumPrinters(flags, None, 2)
        except Exception:
            return []

        local_host = os.environ.get("COMPUTERNAME", "localhost")
        for printer in raw_printers:
            if not isinstance(printer, tuple) or len(printer) < 4:
                continue

            server_name = (printer[0] or "").strip() if len(printer) > 0 else ""
            printer_name = (printer[1] or "").strip() if len(printer) > 1 else ""
            share_name = (printer[2] or "").strip() if len(printer) > 2 else ""
            attributes = printer[13] if len(printer) > 13 and isinstance(printer[13], int) else 0
            if not printer_name:
                continue

            if server_name.startswith("\\"):
                server_name = server_name.lstrip("\\")

            is_shared = bool(share_name)
            if not share_name and attributes and getattr(win32print, "PRINTER_ATTRIBUTE_SHARED", 0) and (attributes & win32print.PRINTER_ATTRIBUTE_SHARED):
                share_name = printer_name
                is_shared = True

            unc_path = ""
            if share_name:
                unc_host = server_name or local_host
                unc_path = f"\\\\{unc_host}\\{share_name}"
            elif printer_name.startswith("\\\\"):
                unc_path = printer_name

            entries.append(
                {
                    "name": printer_name,
                    "share_name": share_name,
                    "unc_path": unc_path,
                    "server_name": server_name,
                    "is_default": printer_name.lower() == default_printer_name.lower(),
                    "is_shared": is_shared,
                }
            )

        return entries

    def get_windows_printer_inventory_via_powershell(self):
        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Printer | Select-Object Name,ShareName,SystemName,Default,Shared | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return []

        if completed.returncode != 0 or not completed.stdout.strip():
            return []

        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return []

        items = parsed if isinstance(parsed, list) else [parsed]
        local_host = os.environ.get("COMPUTERNAME", "localhost")
        entries = []
        for item in items:
            if not isinstance(item, dict):
                continue

            printer_name = (item.get("Name") or "").strip()
            share_name = (item.get("ShareName") or "").strip()
            system_name = (item.get("SystemName") or "").strip().lstrip("\\")
            if not printer_name:
                continue

            unc_path = ""
            if share_name:
                unc_path = f"\\\\{system_name or local_host}\\{share_name}"
            elif printer_name.startswith("\\\\"):
                unc_path = printer_name

            entries.append(
                {
                    "name": printer_name,
                    "share_name": share_name,
                    "unc_path": unc_path,
                    "server_name": system_name,
                    "is_default": bool(item.get("Default")),
                    "is_shared": bool(item.get("Shared")) or bool(share_name),
                }
            )

        return entries

    def resolve_printer_target(self, printer_value):
        normalized = (printer_value or "").strip()
        if not normalized and self.default_windows_printer:
            normalized = self.default_windows_printer
        if not normalized:
            return ""
        if self.is_shared_printer_path(normalized):
            return normalized

        entry = self.printer_lookup.get(normalized.lower())
        if entry:
            if entry.get("unc_path"):
                return entry["unc_path"]
            if entry.get("share_name"):
                host_name = entry.get("server_name") or os.environ.get("COMPUTERNAME", "localhost")
                return f"\\\\{host_name}\\{entry['share_name']}"

        return normalized

    def toggle_maintenance_window(self, event=None):
        self.load_local_printers()
        if self.maintenance_window is None:
            self.build_maintenance_window()
        elif self.maintenance_window.winfo_exists():
            self.maintenance_window.deiconify()
            self.maintenance_window.lift()
        else:
            self.build_maintenance_window()

    def build_maintenance_window(self):
        self.maintenance_window = tk.Toplevel(self.root)
        self.maintenance_window.title("Manutenção Técnica - GEHAKA G2000")
        self.maintenance_window.geometry("760x620")
        self.maintenance_window.resizable(False, False)
        self.maintenance_window.withdraw()
        self.maintenance_window.config(bg=MAIN_BG_COLOR)
        if self.icon_image is not None:
            try:
                self.maintenance_window.iconphoto(True, self.icon_image)
            except Exception:
                pass

        container = tk.Frame(self.maintenance_window, padx=12, pady=10, bg=MAIN_BG_COLOR)
        container.pack(fill="both", expand=True)

        tk.Label(container, text="Computador", font=("Segoe UI", 10, "bold"), bg=MAIN_BG_COLOR, fg=TITLE_COLOR).grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_computer_name_var = tk.StringVar(value=os.environ.get("COMPUTERNAME", "Indisponível"))
        self.maintenance_computer_name_entry = tk.Entry(container, textvariable=self.maintenance_computer_name_var, width=28, state="readonly", font=("Segoe UI", 9))
        self.maintenance_computer_name_entry.grid(row=0, column=1, padx=(0, 16), pady=4, sticky="w")

        tk.Label(container, text="IP Local", font=("Segoe UI", 10, "bold"), bg=MAIN_BG_COLOR, fg=TITLE_COLOR).grid(row=0, column=2, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_ip_var = tk.StringVar(value=self.get_local_ip_address())
        self.maintenance_ip_entry = tk.Entry(container, textvariable=self.maintenance_ip_var, width=18, state="readonly", font=("Segoe UI", 9))
        self.maintenance_ip_entry.grid(row=0, column=3, padx=(0, 8), pady=4, sticky="w")

        tk.Label(container, text="Porta COM", font=("Segoe UI", 10, "bold"), bg=MAIN_BG_COLOR, fg=TITLE_COLOR).grid(row=1, column=0, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_port_var = tk.StringVar(value=self.manual_port_override or self.current_port_name or "")
        self.maintenance_port_combo = ttk.Combobox(container, textvariable=self.maintenance_port_var, state="readonly", width=44)
        self.maintenance_port_combo.grid(row=1, column=1, columnspan=3, padx=(0, 16), pady=4, sticky="w")

        tk.Label(container, text="Velocidade", font=("Segoe UI", 10, "bold"), bg=MAIN_BG_COLOR, fg=TITLE_COLOR).grid(row=2, column=0, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_baud_var = tk.StringVar(value=str(self.manual_baud_rate or DEFAULT_BAUD_RATE))
        self.maintenance_baud_entry = tk.Entry(container, textvariable=self.maintenance_baud_var, width=20, font=("Segoe UI", 9))
        self.maintenance_baud_entry.grid(row=2, column=1, padx=(0, 16), pady=4, sticky="w")

        tk.Label(container, text="Impressora", font=("Segoe UI", 10, "bold"), bg=MAIN_BG_COLOR, fg=TITLE_COLOR).grid(row=3, column=0, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_printer_var = tk.StringVar(value="")
        self.maintenance_printer_combo = ttk.Combobox(container, textvariable=self.maintenance_printer_var, state="readonly", width=44)
        self.maintenance_printer_combo.grid(row=3, column=1, columnspan=3, padx=(0, 16), pady=4, sticky="w")
        self.maintenance_printer_combo.bind("<<ComboboxSelected>>", self.on_maintenance_printer_selected)

        tk.Label(container, text="Caminho", font=("Segoe UI", 10, "bold"), bg=MAIN_BG_COLOR, fg=TITLE_COLOR).grid(row=4, column=0, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_printer_path_var = tk.StringVar(value=self.printer_name_var.get().strip())
        self.maintenance_printer_path_entry = tk.Entry(container, textvariable=self.maintenance_printer_path_var, width=46, font=("Segoe UI", 9))
        self.maintenance_printer_path_entry.grid(row=4, column=1, columnspan=3, padx=(0, 16), pady=4, sticky="w")

        self.maintenance_printer_notice_var = tk.StringVar(value="")
        tk.Label(container, textvariable=self.maintenance_printer_notice_var, font=("Segoe UI", 9), fg="#66788a", bg=MAIN_BG_COLOR).grid(row=5, column=0, columnspan=4, sticky="w", pady=(0, 6))

        self.maintenance_refresh_lists_btn = tk.Button(container, text="Atualizar listas", command=self.refresh_maintenance_lists, font=("Segoe UI", 9, "bold"), bg="#dce9f4", relief="flat")
        self.maintenance_refresh_lists_btn.grid(row=6, column=0, columnspan=2, pady=(2, 8), sticky="w")

        self.apply_maintenance_btn = tk.Button(container, text="Aplicar ajustes", command=self.apply_maintenance_settings, font=("Segoe UI", 9, "bold"), bg="#2d6ea5", fg="#ffffff", activebackground="#23557f", activeforeground="#ffffff", relief="flat")
        self.apply_maintenance_btn.grid(row=6, column=2, pady=(2, 8), sticky="w")

        self.maintenance_test_btn = tk.Button(container, text="Testar impressora", command=self.test_printer, font=("Segoe UI", 9, "bold"), bg="#dce9f4", relief="flat")
        self.maintenance_test_btn.grid(row=6, column=3, pady=(2, 8), sticky="w")

        self.maintenance_open_logs_btn = tk.Button(
            container,
            text="Abrir pasta de logs",
            command=lambda: self.open_folder_in_explorer(LOG_STORAGE_DIR, "logs"),
            font=("Segoe UI", 9, "bold"),
            bg="#dce9f4",
            relief="flat",
        )
        self.maintenance_open_logs_btn.grid(row=7, column=0, columnspan=2, pady=(0, 8), sticky="w")

        self.maintenance_open_tickets_btn = tk.Button(
            container,
            text="Abrir pasta de tickets",
            command=lambda: self.open_folder_in_explorer(TICKET_STORAGE_DIR, "tickets"),
            font=("Segoe UI", 9, "bold"),
            bg="#dce9f4",
            relief="flat",
        )
        self.maintenance_open_tickets_btn.grid(row=7, column=2, columnspan=2, pady=(0, 8), sticky="w")

        tk.Label(container, text="Log de comunicação", font=("Segoe UI", 10, "bold"), bg=MAIN_BG_COLOR, fg=TITLE_COLOR).grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 4))
        self.maintenance_log = tk.Text(container, height=16, width=92, wrap="word", font=("Consolas", 9), relief="flat", bg="#fbfdff")
        self.maintenance_log.grid(row=9, column=0, columnspan=4, sticky="nsew")

        self.refresh_maintenance_window()
        self.maintenance_window.protocol("WM_DELETE_WINDOW", self.maintenance_window.withdraw)
        self.maintenance_window.deiconify()

    def refresh_maintenance_window(self):
        if self.maintenance_window is None or not self.maintenance_window.winfo_exists():
            return

        self.maintenance_computer_name_var.set(os.environ.get("COMPUTERNAME", "Indisponível"))
        self.maintenance_ip_var.set(self.get_local_ip_address())

        port_inventory = self.get_port_inventory()
        port_values = [item["display"] for item in port_inventory]
        self.maintenance_port_combo["values"] = port_values

        # Preserve unsaved COM selection in the maintenance combo while logs keep refreshing.
        selected_port_display_draft = (self.maintenance_port_var.get() or "").strip() if hasattr(self, "maintenance_port_var") else ""
        selected_port_draft = self.extract_port_device(selected_port_display_draft)
        selected_port = selected_port_draft or self.manual_port_override or self.current_port_name or ""
        selected_port_display = ""
        for item in port_inventory:
            if item["device"] == selected_port:
                selected_port_display = item["display"]
                break
        if not selected_port_display and selected_port:
            selected_port_display = selected_port

        if selected_port_display and selected_port_display not in port_values:
            port_values.append(selected_port_display)
            self.maintenance_port_combo["values"] = port_values

        self.maintenance_port_var.set(selected_port_display)

        printer_values = self.build_printer_combo_values()
        configured_printer_target = self.printer_name_var.get().strip()
        resolved_target = self.resolve_printer_target(configured_printer_target)
        selected_printer_display = ""
        for entry in self.available_printers:
            candidate_path = self.build_shared_path_for_printer(entry)
            if resolved_target and candidate_path.lower() == resolved_target.lower():
                selected_printer_display = (entry.get("name") or "").strip()
                break
            unc_path = (entry.get("unc_path") or "").strip()
            if resolved_target and unc_path and unc_path.lower() == resolved_target.lower():
                selected_printer_display = (entry.get("name") or "").strip()
                break

        if not selected_printer_display:
            selected_printer_display = (self.maintenance_printer_var.get() or "").strip()

        if selected_printer_display and selected_printer_display not in printer_values:
            printer_values.append(selected_printer_display)
        self.maintenance_printer_combo["values"] = printer_values
        self.maintenance_printer_var.set(selected_printer_display)
        self.maintenance_printer_path_var.set(configured_printer_target)

        selected_entry = self.find_printer_entry_by_name(selected_printer_display)
        if selected_entry and self.build_shared_path_for_printer(selected_entry):
            self.maintenance_printer_notice_var.set("Compartilhamento detectado automaticamente.")
        else:
            self.maintenance_printer_notice_var.set("Selecione uma impressora para gerar o caminho automaticamente.")

        self.maintenance_baud_var.set(str(self.manual_baud_rate or DEFAULT_BAUD_RATE))
        self.maintenance_log.configure(state="normal")
        self.maintenance_log.delete("1.0", tk.END)
        for line in self.maintenance_log_lines:
            self.maintenance_log.insert(tk.END, f"{line}\n")
        self.maintenance_log.configure(state="disabled")

    def apply_maintenance_settings(self):
        port_override = self.extract_port_device(self.maintenance_port_var.get().strip())
        baud_override = self.maintenance_baud_var.get().strip()
        printer_override = self.maintenance_printer_path_var.get().strip()

        self.manual_port_override = port_override
        self.printer_name_var.set(printer_override)

        try:
            self.manual_baud_rate = int(baud_override)
        except ValueError:
            self.manual_baud_rate = DEFAULT_BAUD_RATE

        printer_display = printer_override or "nenhum compartilhamento configurado"
        self.append_maintenance_log(
            f"Ajustes aplicados: porta={self.manual_port_override or 'não definida'}, "
            f"velocidade={self.manual_baud_rate}, destino={printer_display}"
        )
        self.save_config()
        self.check_printer_status()
        self.status_label.set("Configurações aplicadas. Reiniciando conexão...")
        self.append_maintenance_log("Configuração salva. Reiniciando conexão fixa do G2000...")
        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
        self.schedule_reconnect()

    def append_maintenance_log(self, message):
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.maintenance_log_lines.append(f"[{timestamp}] {message}")
        self.write_runtime_log(message)
        if len(self.maintenance_log_lines) > 120:
            self.maintenance_log_lines = self.maintenance_log_lines[-120:]
        self.refresh_maintenance_window()

    def auto_connect(self):
        if self.is_app_exiting:
            return

        self.reconnect_job = None

        if self.serial_port and self.serial_port.is_open:
            return

        target_port = (self.manual_port_override or "").strip()
        if not target_port:
            self.g2000_status_var.set("� Não encontrado")
            self.set_g2000_status_color("error")
            self.last_error_var.set("Porta COM não configurada no config.json")
            self.status_label.set("Configure a porta COM na manutenção (Ctrl+Shift+F12).")
            self.append_maintenance_log("Porta COM não configurada; aguardando configuração para reconectar.")
            self.schedule_reconnect()
            return

        self.current_port_name = target_port
        self.append_maintenance_log(f"Tentando conectar na porta fixa {target_port}...")
        try:
            self.serial_port = serial.Serial(
                port=target_port,
                baudrate=self.manual_baud_rate or DEFAULT_BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.serial_read_timeout_seconds,
                xonxoff=False,
                rtscts=False,
            )
        except Exception as exc:
            self.g2000_status_var.set("� Falha na conexão")
            self.set_g2000_status_color("error")
            guided = self.friendly_error_message("serial_connect", str(exc))
            self.set_guided_error(guided, str(exc))
            self.status_label.set("Tentando reconectar automaticamente...")
            self.append_maintenance_log(f"Falha ao abrir {target_port}: {exc}")
            self.schedule_reconnect()
            return

        self.stop_thread = False
        self.packet_buffer.clear()
        self.total_bytes = 0
        self.last_data_time = time.monotonic()
        self.save_config()

        self.g2000_status_var.set("🟢 Conectado")
        self.set_g2000_status_color("connected")
        self.last_error_var.set("Nenhum")
        self.status_label.set("Aguardando nova medição...")
        self.append_maintenance_log(f"Conectando em {target_port} com {self.manual_baud_rate or DEFAULT_BAUD_RATE} bps")
        self.root.iconify()
        self.check_printer_status()
        self.read_thread = threading.Thread(target=self.read_loop, daemon=True)
        self.read_thread.start()

    def schedule_reconnect(self):
        if self.is_app_exiting:
            return

        if self.reconnect_job is not None:
            return

        self.append_maintenance_log(f"Agendada nova tentativa de conexão em {int(RECONNECT_INTERVAL_SECONDS)}s")
        self.reconnect_job = self.root.after(int(RECONNECT_INTERVAL_SECONDS * 1000), self.auto_connect)

    def toggle_connection(self):
        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
            return

        self.auto_connect()

    def check_printer_status(self, printer_mode=None, printer_ip=None, printer_port=None):
        printer_mode = printer_mode or self.printer_mode_var.get()
        printer_name = self.printer_name_var.get().strip()

        if printer_mode == DEFAULT_PRINTER_MODE:
            resolved_target = self.resolve_printer_target(printer_name)
            if not resolved_target:
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                self.last_error_var.set("Compartilhamento da impressora não configurado")
                return

            if not self.is_shared_printer_path(resolved_target):
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                if printer_name and printer_name.lower() in self.printer_lookup:
                    self.last_error_var.set("Impressora encontrada, mas sem compartilhamento SMB ativo no Windows")
                else:
                    self.last_error_var.set(r"Use o formato \\servidor\impressora ou o nome de uma impressora detectada")
                return

            if self.check_shared_printer_available(resolved_target):
                self.printer_status_var.set("🟢 Conectada")
                self.set_printer_status_color("connected")
                self.last_error_var.set("Nenhum")
                self.save_config()
                return

            self.printer_status_var.set("🔴 Offline")
            self.set_printer_status_color("error")
            self.last_error_var.set("Compartilhamento da impressora indisponível")
            return

        printer_ip = printer_ip or self.printer_ip_var.get().strip()
        printer_port = printer_port or self.parse_printer_port_value()
        try:
            with socket.create_connection((printer_ip, printer_port), timeout=2):
                self.printer_status_var.set("🟢 Conectada")
                self.set_printer_status_color("connected")
                self.last_error_var.set("Nenhum")
        except Exception as exc:
            self.printer_status_var.set("🔴 Offline")
            self.set_printer_status_color("error")
            self.set_guided_error(self.friendly_error_message("printer_tcp", str(exc)), str(exc))

    def test_printer(self):
        printer_name = self.printer_name_var.get().strip()
        if not printer_name:
            self.last_error_var.set("Compartilhamento da impressora não configurado")
            return

        success = self.send_temp_config_test_to_shared_printer(printer_name)
        if success:
            self.last_print_var.set(f"{datetime.now().strftime('%H:%M:%S')} OK (teste temporário)")
            self.last_error_var.set("Nenhum")
            self.status_label.set("Teste temporário enviado, impresso e removido.")
            self.append_maintenance_log("Teste temporário da impressora concluído.")
        else:
            self.last_print_var.set("Erro")
            self.append_maintenance_log(f"Falha no teste temporário: {self.last_error_var.get()}")

    def disconnect(self, update_ui=True):
        self.stop_thread = True
        if self.reconnect_job is not None:
            try:
                self.root.after_cancel(self.reconnect_job)
            except Exception:
                pass
            self.reconnect_job = None
        if self.serial_port and self.serial_port.is_open:
            try:
                self.serial_port.reset_input_buffer()
            except Exception:
                pass
            try:
                self.serial_port.close()
            except Exception:
                pass
        self.serial_port = None
        if update_ui:
            self.g2000_status_var.set("� Desconectado")
            self.status_label.set("Aguardando dispositivo G2000...")
        self.current_port_name = None
        self.append_maintenance_log("Conexão serial encerrada e porta COM liberada.")

    def read_loop(self):
        while not self.stop_thread:
            if not self.serial_port or not self.serial_port.is_open:
                break

            try:
                data = self.serial_port.read(1)
            except serial.SerialException as exc:
                self.root.after(0, self.handle_disconnection, f"USB desconectado: {exc}")
                break

            if not data:
                time.sleep(0.05)
                continue

            now = time.monotonic()
            if self.packet_buffer and (now - self.last_data_time) > self.packet_gap_timeout_seconds:
                self.dispatch_packet("timeout")

            self.packet_buffer.extend(data)
            self.last_data_time = now
            self.total_bytes += len(data)

            if self.packet_buffer.endswith(b"\r\n"):
                self.dispatch_packet("CRLF")
                continue

            if data == b"\x03":
                self.dispatch_packet("ETX")

        if not self.is_app_exiting:
            self.root.after(0, self.handle_disconnection)

    def dispatch_packet(self, trigger):
        if not self.packet_buffer:
            return

        raw_packet = bytes(self.packet_buffer)
        self.packet_buffer.clear()
        self.root.after(0, self.finalize_packet, raw_packet, trigger)

    def handle_disconnection(self, reason=None):
        if self.is_app_exiting:
            return

        if reason:
            self.last_error_var.set(reason)
        self.disconnect()
        self.g2000_status_var.set("� Reconectando...")
        self.status_label.set("Aguardando retorno do dispositivo...")
        self.schedule_reconnect()

    def finalize_packet(self, raw_packet, trigger="timeout"):
        if not raw_packet:
            return

        packet_size = len(raw_packet)
        self.write_runtime_log(f"Pacote recebido: {packet_size} bytes | trigger={trigger}")

        raw_text = raw_packet.decode("latin1", errors="replace")
        cleaned_text = self.clean_packet_text(raw_text)
        parsed_fields = self.parse_measurement_fields(cleaned_text)
        measurement_data = self.build_measurement_data(parsed_fields)
        ticket_text = self.format_ticket_text(measurement_data)
        self.last_ticket_text = ticket_text

        self.measurements_received += 1
        self.measurements_count_var.set(str(self.measurements_received))
        self.status_label.set("Nova medição recebida.")
        self.append_maintenance_log(f"Medição recebida #{self.measurements_received} (trigger={trigger})")

        self.root.after(0, self.set_measurement_text, ticket_text)
        self.root.after(0, self.print_measurement, ticket_text)

    def clean_packet_text(self, text):
        cleaned = text.replace("\x02", "").replace("\x03", "")
        cleaned = cleaned.strip("\r\n")
        return cleaned

    def parse_measurement_fields(self, text):
        values = [part.strip() for part in text.split(";")]
        while values and values[-1] == "":
            values.pop()

        parsed = []
        for index, value in enumerate(values):
            if index < len(MEASUREMENT_FIELD_NAMES):
                field_name = MEASUREMENT_FIELD_NAMES[index]
            else:
                field_name = f"Campo extra {index + 1}"
            parsed.append((field_name, value))

        return parsed

    def build_measurement_data(self, parsed_fields):
        data = {name: "" for name in MEASUREMENT_FIELD_NAMES}
        for field_name, value in parsed_fields:
            data[field_name] = value
        return data

    def format_decimal(self, value, decimals):
        raw = (value or "").strip()
        if not raw:
            return "-"
        normalized = raw.replace(",", ".")
        try:
            number = float(normalized)
        except ValueError:
            return raw
        return f"{number:.{decimals}f}".replace(".", ",")

    def wrap_ticket_value(self, value, width=40):
        cleaned = (value or "").strip()
        if not cleaned:
            return ["-"]
        return textwrap.wrap(cleaned, width=width, break_long_words=True, break_on_hyphens=False)

    def format_ticket_text(self, data):
        line_sep = TICKET_SEPARATOR

        model = data.get("Modelo do Instrumento", "").strip() or "G2000"
        firmware = data.get("Versão do Firmware", "").strip() or "-"
        hardware = data.get("Versão do Hardware", "").strip() or "-"
        serial_number = data.get("Número de Série", "").strip() or "-"
        hora = data.get("Hora", "").strip() or "-"
        data_medicao = data.get("Data", "").strip() or "-"
        produto = data.get("Nome do Grão", "").strip() or "-"
        curva = data.get("Número da Curva", "").strip() or "-"
        validade_curva = data.get("Validade da Curva", "").strip() or "-"
        amostra = data.get("Número da Amostra", "").strip() or "-"

        temp_amostra = self.format_decimal(data.get("Temperatura da Amostra (°C)", ""), 1)
        temp_instrumento = self.format_decimal(data.get("Temperatura do Instrumento (°C)", ""), 1)
        peso_amostra = self.format_decimal(data.get("Peso (g)", ""), 1)
        umidade = self.format_decimal(data.get("Umidade (%)", ""), 2)
        assinatura = data.get("Assinatura", "").strip()

        lines = [
            " Gaasa e Alimentos Ltda",
            f" GEHAKA Medidor de Umidade {model}",
            line_sep,
            f"Versao de Firmware      {firmware}",
            f"Versao de Hardware      {hardware}",
            f"Numero de Serie         {serial_number}",
            f"Hora {hora}     Data {data_medicao}",
            line_sep,
            f"Produto = {produto}",
            f"Versao Equacao ..= {curva}",
            f"Data Val. Curva .= {validade_curva}",
            f"Amostra Numero ..= {amostra}",
            f"Temp.Amostra ....= {temp_amostra:>6} C",
            f"Temp.Instru. ....= {temp_instrumento:>6} C",
            f"Peso Amostra ....= {peso_amostra:>6} g",
            "",
            f"Umidade .........= {umidade:>6} %",
            line_sep,
        ]

        if assinatura:
            lines.extend(["", "", "", "", "Assinatura", ""])
            lines.extend(self.wrap_signature_lines(assinatura, width=len(line_sep)))
            lines.extend([line_sep])

        return "\n".join(lines)

    def wrap_signature_lines(self, value, width):
        normalized = (value or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for raw_line in normalized.split("\n"):
            clean_line = raw_line.strip()
            if not clean_line:
                continue
            lines.extend(self.wrap_ticket_value(clean_line, width=width))
        return lines

    def build_ticket_file_bytes(self, text, include_cut=True):
        corta_papel = bytes([29, 86, 66, 0])

        payload = bytearray()
        lines = text.splitlines()

        for line in lines:
            encoded_line = line.encode("latin1", errors="replace")
            payload.extend(encoded_line)
            payload.extend(b"\r\n")

        payload.extend(b"\r\n\r\n\r\n")
        if include_cut:
            payload.extend(corta_papel)
        return bytes(payload)

    def get_generated_ticket_paths(self):
        self.ensure_ticket_storage_dir()
        self.ticket_rotation_index = (self.ticket_rotation_index % MAX_STORED_TICKETS) + 1
        slot = f"{self.ticket_rotation_index:02d}"
        return (
            os.path.join(TICKET_STORAGE_DIR, f"TicketGerado_{slot}.txt"),
            os.path.join(TICKET_STORAGE_DIR, f"TicketGerado_preview_{slot}.txt"),
        )

    def save_generated_ticket_files(self, text):
        printer_file_path, preview_file_path = self.get_generated_ticket_paths()
        normalized_text = (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

        payload_bytes = self.build_ticket_file_bytes(normalized_text, include_cut=True)

        with open(printer_file_path, "wb") as ticket_file:
            ticket_file.write(payload_bytes)

        with open(preview_file_path, "w", encoding="latin1", newline="\r\n") as preview_file:
            preview_file.write(normalized_text)
            preview_file.write("\r\n")

        self.append_maintenance_log(f"Ticket salvo em rotação: {printer_file_path}")
        self.append_maintenance_log(f"Prévia do ticket salva em rotação: {preview_file_path}")

        return printer_file_path, preview_file_path

    def is_shared_printer_path(self, printer_path):
        normalized = (printer_path or "").strip()
        if not normalized.startswith("\\\\"):
            return False
        parts = [part for part in normalized[2:].split("\\") if part]
        return len(parts) >= 2

    def check_shared_printer_available(self, printer_path):
        if not self.is_shared_printer_path(printer_path):
            return False

        normalized = printer_path.strip()
        host_name = normalized[2:].split("\\", 1)[0]
        share_name = normalized.rsplit("\\", 1)[-1].lower()

        try:
            completed = subprocess.run(
                ["cmd.exe", "/c", "net", "view", f"\\\\{host_name}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self.append_maintenance_log(f"Falha ao consultar compartilhamentos em {host_name}: {exc}")
            return False

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "host não respondeu"
            self.append_maintenance_log(f"Compartilhamento indisponível em {host_name}: {detail}")
            return False

        return share_name in completed.stdout.lower()

    def write_ticket_text_file(self, text):
        printer_file_path, preview_file_path = self.save_generated_ticket_files(text)
        self.append_maintenance_log(f"Ticket gravado em {printer_file_path}")
        self.append_maintenance_log(f"Prévia do ticket gravada em {preview_file_path}")
        return printer_file_path

    def normalize_shared_printer_path(self, printer_path):
        raw = (printer_path or "")
        # Remove hidden/control chars and normalize quotation variants that break cmd parsing.
        raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u00a0", " ")
        raw = "".join(ch for ch in raw if ch >= " " or ch == "\t")
        raw = raw.strip().strip('"').replace("/", "\\")
        if not raw:
            return ""

        if raw.startswith("\\"):
            tail = raw.lstrip("\\")
            parts = [part for part in tail.split("\\") if part]
            if len(parts) >= 2:
                return "\\\\" + "\\".join(parts)
        return raw

    def copy_file_to_shared_printer(self, source_file, printer_path):
        safe_source = (source_file or "").strip()
        safe_printer = self.normalize_shared_printer_path(printer_path)

        if not safe_source or not os.path.exists(safe_source):
            self.set_guided_error("Arquivo de ticket não encontrado para impressão.")
            return False

        if not self.is_shared_printer_path(safe_printer):
            self.set_guided_error(r"Caminho da impressora inválido. Use o formato \\servidor\impressora.")
            return False

        file_size = os.path.getsize(safe_source)
        if file_size <= 0:
            self.set_guided_error("Ticket vazio. Gere uma medição antes de imprimir.")
            return False

        copy_command = f'copy /b "{safe_source}" "{safe_printer}"'
        self.append_maintenance_log(f"Comando de envio: {copy_command}")

        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", copy_command],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if completed.returncode == 0:
            output = completed.stdout.strip()
            if output:
                self.append_maintenance_log(f"Retorno copy /b: {output}")
            self.append_maintenance_log(f"Envio OK ({file_size} bytes): {safe_source} -> {safe_printer}")
            return True

        stdout_text = completed.stdout.strip()
        stderr_text = completed.stderr.strip()
        detail = stderr_text or stdout_text or "copy /b retornou erro"
        self.append_maintenance_log(f"Falha no copy /b (code={completed.returncode}): {detail}")
        self.set_guided_error(self.friendly_error_message("printer_share", detail), detail)
        if stdout_text:
            self.append_maintenance_log(f"copy stdout: {stdout_text}")
        if stderr_text:
            self.append_maintenance_log(f"copy stderr: {stderr_text}")

        self.append_maintenance_log("Aplicando fallback: escrita binária direta no compartilhamento UNC")
        try:
            with open(safe_source, "rb") as source_handle:
                payload = source_handle.read()

            if not payload:
                self.set_guided_error("Ticket vazio. Gere uma medição antes de imprimir.")
                self.append_maintenance_log("Fallback cancelado: arquivo de origem vazio")
                return False

            with open(safe_printer, "wb") as printer_handle:
                printer_handle.write(payload)

            self.append_maintenance_log(f"Envio OK no fallback ({len(payload)} bytes): {safe_source} -> {safe_printer}")
            return True
        except Exception as exc:
            self.set_guided_error(self.friendly_error_message("printer_share", str(exc)), str(exc))
            self.append_maintenance_log(f"Falha no fallback (escrita UNC direta): {exc}")
        return False

    def send_text_file_to_shared_printer(self, text, printer_path):
        ticket_file = None
        try:
            ticket_file = self.write_ticket_text_file(text)
            if self.copy_file_to_shared_printer(ticket_file, printer_path):
                self.append_maintenance_log(f"Arquivo de ticket enviado para {printer_path}: {ticket_file}")
                return True
            return False
        except Exception as exc:
            self.set_guided_error("Falha ao preparar ticket para impressão.", str(exc))
            self.append_maintenance_log(f"Falha ao gerar/enviar ticket TXT para {printer_path}: {exc}")
            return False

    def send_temp_config_test_to_shared_printer(self, printer_target):
        resolved_target = self.resolve_printer_target(printer_target)
        if not self.is_shared_printer_path(resolved_target):
            self.last_error_var.set("Caminho da impressora inválido para teste")
            return False

        computer_name = os.environ.get("COMPUTERNAME", "Indisponível")
        computer_ip = self.get_local_ip_address()
        printer_name = self.get_current_printer_name() or "Indisponível"
        printer_path = self.printer_name_var.get().strip() or "Indisponível"

        content = (
            "GEHAKA G2000 - TESTE DE IMPRESSORA\n"
            "======================================\n"
            f"Data/Hora: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
            f"Computador: {computer_name}\n"
            f"IP: {computer_ip}\n"
            f"Impressora: {printer_name}\n"
            f"Caminho: {printer_path}\n"
            "======================================\n"
        )

        temp_file_path = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="latin1", newline="\r\n", suffix="_teste_impressora.txt") as temp_file:
                temp_file.write(content)
                temp_file.write("\r\n")
                temp_file_path = temp_file.name

            if self.copy_file_to_shared_printer(temp_file_path, resolved_target):
                self.append_maintenance_log(f"Arquivo temporário de teste enviado para {resolved_target}: {temp_file_path}")
                return True
            return False
        except Exception as exc:
            self.last_error_var.set(str(exc))
            self.append_maintenance_log(f"Falha no teste temporário da impressora: {exc}")
            return False
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    self.append_maintenance_log(f"Arquivo temporário removido: {temp_file_path}")
                except OSError as exc:
                    self.append_maintenance_log(f"Não foi possível remover arquivo temporário {temp_file_path}: {exc}")

    def set_measurement_text(self, text):
        self.measurement_text.configure(state="normal")
        self.measurement_text.delete("1.0", tk.END)
        self.measurement_text.insert(tk.END, text)
        self.measurement_text.configure(state="disabled")

    def print_measurement(self, text):
        printer_mode = self.printer_mode_var.get()
        printer_ip = self.printer_ip_var.get().strip()
        printer_port_text = self.printer_port_var.get().strip()
        printer_name = self.printer_name_var.get().strip()
        self.append_maintenance_log(f"Iniciando envio de ticket. Modo={printer_mode}")

        if printer_mode == "Rede (TCP/IP)":
            if not printer_ip or not printer_port_text:
                self.last_error_var.set("Impressora não configurada")
                self.print_errors += 1
                self.errors_count_var.set(str(self.print_errors))
                return
            try:
                printer_port = int(printer_port_text)
            except ValueError:
                self.last_error_var.set("Porta da impressora inválida")
                self.print_errors += 1
                self.errors_count_var.set(str(self.print_errors))
                return
        else:
            if not printer_name:
                self.last_error_var.set("Compartilhamento da impressora não configurado")
                self.print_errors += 1
                self.errors_count_var.set(str(self.print_errors))
                return
            printer_port = 0

        if self.send_to_printer(text, printer_mode, printer_ip, printer_port, printer_name=printer_name if printer_mode == DEFAULT_PRINTER_MODE else None):
            self.tickets_printed += 1
            self.tickets_count_var.set(str(self.tickets_printed))
            self.last_print_var.set(f"{datetime.now().strftime('%H:%M:%S')} OK")
            self.last_error_var.set("Nenhum")
            self.status_label.set("Impressão enviada com sucesso.")
            self.printer_status_var.set("🟢 Online")
        else:
            self.print_errors += 1
            self.errors_count_var.set(str(self.print_errors))
            self.last_print_var.set(f"{datetime.now().strftime('%H:%M:%S')} ERRO")
            self.status_label.set("Erro ao enviar para a impressora.")
            self.append_maintenance_log("Falha ao enviar texto para a impressora.")

    def send_to_printer(self, payload, printer_mode, printer_ip, printer_port, printer_name=None):
        payload_bytes = self.build_ticket_file_bytes(payload)

        if printer_mode == DEFAULT_PRINTER_MODE:
            resolved_target = self.resolve_printer_target(printer_name)
            self.append_maintenance_log(f"Destino compartilhado resolvido: {resolved_target or 'não resolvido'}")
            if not resolved_target:
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                self.last_error_var.set("Compartilhamento da impressora não configurado")
                return False

            if not self.is_shared_printer_path(resolved_target):
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                self.last_error_var.set(r"Impressora sem compartilhamento SMB. Use o nome de uma impressora compartilhada ou um caminho \\servidor\impressora")
                return False

            if self.send_text_file_to_shared_printer(payload, resolved_target):
                self.printer_status_var.set("🟢 Online")
                self.set_printer_status_color("connected")
                self.save_config()
                return True

            self.printer_status_var.set("🔴 Offline")
            self.set_printer_status_color("error")
            if not self.last_error_var.get() or self.last_error_var.get() == "Nenhum":
                self.last_error_var.set("Falha ao enviar ticket TXT para o compartilhamento da impressora")
            return False

        try:
            self.append_maintenance_log(f"Enviando ticket via TCP para {printer_ip}:{printer_port}")
            with socket.create_connection((printer_ip, printer_port), timeout=3) as sock:
                sock.sendall(payload_bytes)
                self.printer_status_var.set("🟢 Online")
                self.set_printer_status_color("connected")
                return True
        except Exception as exc:
            self.printer_status_var.set("🔴 Offline")
            self.set_printer_status_color("error")
            self.set_guided_error(self.friendly_error_message("printer_tcp", str(exc)), str(exc))
            return False

    def set_g2000_status(self, value):
        self.g2000_status_var.set(value)

    def set_printer_status(self, value):
        self.printer_status_var.set(value)


def main():
    set_windows_app_user_model_id()
    root = tk.Tk()
    app = GehakaMonitorBridgeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
