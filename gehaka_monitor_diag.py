import os
import json
import socket
import subprocess
import threading
import textwrap
import time
from datetime import datetime
from tkinter import ttk
import tkinter as tk
import serial
import serial.tools.list_ports

DEFAULT_BAUD_RATE = 115200
DEFAULT_PRINTER_MODE = "Compartilhada (TXT/SMB)"
DEFAULT_SHARED_PRINTER_PATH = r"\\192.1.2.43\MP4200"
RECONNECT_INTERVAL_SECONDS = 5.0
TICKET_WIDTH_80MM = 42
TICKET_SEPARATOR = "=" * 38
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILENAME = "config.json"
GENERATED_TICKET_FILENAME = "TicketGerado.txt"
GENERATED_TICKET_PREVIEW_FILENAME = "TicketGerado_preview.txt"

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

class GehakaMonitorBridgeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GEHAKA G2000")
        self.root.geometry("900x640")
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
        self.capture_file = None
        self.capture_filename = None
        self.maintenance_window = None
        self.reconnect_job = None
        self.current_port_name = None
        self.last_connected_port = None
        self.maintenance_log_lines = []
        self.config_path = os.path.join(PROJECT_ROOT, CONFIG_FILENAME)
        self.config_com_port = ""
        self.config_baud_rate = DEFAULT_BAUD_RATE
        self.printer_mode_var = tk.StringVar(value=DEFAULT_PRINTER_MODE)
        self.printer_name_var = tk.StringVar(value=DEFAULT_SHARED_PRINTER_PATH)
        self.printer_ip_var = tk.StringVar(value="")
        self.printer_port_var = tk.StringVar(value="")

        self.load_runtime_config()

        self.build_ui()
        self.load_local_printers()
        self.root.bind("<Control-Shift-F12>", self.toggle_maintenance_window)
        self.root.after(500, self.auto_connect)

    def build_ui(self):
        self.root.config(bg="#f4f4f4")

        title = tk.Label(
            self.root,
            text="GEHAKA G2000",
            font=("Arial", 18, "bold"),
            bg="#f4f4f4",
        )
        title.pack(pady=(14, 8))

        status_frame = tk.LabelFrame(self.root, text="Status", bg="#f4f4f4", padx=12, pady=10)
        status_frame.pack(fill="x", padx=16, pady=(0, 10))

        tk.Label(status_frame, text="Status G2000", font=("Arial", 11, "bold"), bg="#f4f4f4").grid(row=0, column=0, padx=(0, 10), pady=6, sticky="w")
        self.g2000_status_var = tk.StringVar(value="� Não encontrado")
        self.g2000_status_label = tk.Label(status_frame, textvariable=self.g2000_status_var, font=("Arial", 11), bg="#f4f4f4", fg="#7a7a7a")
        self.g2000_status_label.grid(row=0, column=1, padx=(0, 25), pady=6, sticky="w")

        tk.Label(status_frame, text="Status Impressora", font=("Arial", 11, "bold"), bg="#f4f4f4").grid(row=0, column=2, padx=(0, 10), pady=6, sticky="w")
        self.printer_status_var = tk.StringVar(value="🔴 Não disponível")
        self.printer_status_label = tk.Label(status_frame, textvariable=self.printer_status_var, font=("Arial", 11), bg="#f4f4f4", fg="#7a7a7a")
        self.printer_status_label.grid(row=0, column=3, padx=(0, 25), pady=6, sticky="w")

        tk.Label(status_frame, text="Última impressão", font=("Arial", 11, "bold"), bg="#f4f4f4").grid(row=1, column=0, padx=(0, 10), pady=6, sticky="w")
        self.last_print_var = tk.StringVar(value="Nenhum")
        tk.Label(status_frame, textvariable=self.last_print_var, font=("Arial", 11), bg="#f4f4f4").grid(row=1, column=1, padx=(0, 25), pady=6, sticky="w")

        tk.Label(status_frame, text="Último erro", font=("Arial", 11, "bold"), bg="#f4f4f4").grid(row=1, column=2, padx=(0, 10), pady=6, sticky="w")
        self.last_error_var = tk.StringVar(value="Nenhum")
        tk.Label(status_frame, textvariable=self.last_error_var, font=("Arial", 11), bg="#f4f4f4").grid(row=1, column=3, padx=(0, 25), pady=6, sticky="w")

        measurement_frame = tk.LabelFrame(self.root, text="Última medição", bg="#f4f4f4", padx=12, pady=10)
        measurement_frame.pack(fill="both", padx=16, pady=(0, 10))
        self.measurement_text = tk.Text(measurement_frame, height=12, width=90, wrap="word")
        self.measurement_text.pack(fill="both", expand=True)
        self.measurement_text.insert(tk.END, "Aguardando medição...\n")
        self.measurement_text.configure(state="disabled")

        counter_frame = tk.LabelFrame(self.root, text="Contadores", bg="#f4f4f4", padx=12, pady=10)
        counter_frame.pack(fill="x", padx=16, pady=(0, 10))

        tk.Label(counter_frame, text="Medições recebidas", font=("Arial", 11, "bold"), bg="#f4f4f4").grid(row=0, column=0, padx=(0, 20), pady=6, sticky="w")
        self.measurements_count_var = tk.StringVar(value="0")
        tk.Label(counter_frame, textvariable=self.measurements_count_var, font=("Arial", 11), bg="#f4f4f4").grid(row=0, column=1, padx=(0, 30), pady=6, sticky="w")

        tk.Label(counter_frame, text="Tickets impressos", font=("Arial", 11, "bold"), bg="#f4f4f4").grid(row=0, column=2, padx=(0, 20), pady=6, sticky="w")
        self.tickets_count_var = tk.StringVar(value="0")
        tk.Label(counter_frame, textvariable=self.tickets_count_var, font=("Arial", 11), bg="#f4f4f4").grid(row=0, column=3, padx=(0, 30), pady=6, sticky="w")

        tk.Label(counter_frame, text="Erros de impressão", font=("Arial", 11, "bold"), bg="#f4f4f4").grid(row=0, column=4, padx=(0, 20), pady=6, sticky="w")
        self.errors_count_var = tk.StringVar(value="0")
        tk.Label(counter_frame, textvariable=self.errors_count_var, font=("Arial", 11), bg="#f4f4f4").grid(row=0, column=5, pady=6, sticky="w")

        self.status_label = tk.StringVar(value="Aguardando nova medição...")
        tk.Label(self.root, textvariable=self.status_label, bg="#f4f4f4", font=("Arial", 10, "bold")).pack(fill="x", padx=16, pady=(0, 10))
        self.set_g2000_status_color("waiting")
        self.set_printer_status_color("waiting")

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

    def printer_mode_to_config(self):
        if self.printer_mode_var.get() == DEFAULT_PRINTER_MODE:
            return "shared"
        return "tcp"

    def config_mode_to_ui(self, mode):
        if (mode or "").strip().lower() == "tcp":
            return "Rede (TCP/IP)"
        return DEFAULT_PRINTER_MODE

    def load_runtime_config(self):
        default_config = {
            "com_port": "",
            "baudrate": DEFAULT_BAUD_RATE,
            "printer_mode": "shared",
            "printer_path": DEFAULT_SHARED_PRINTER_PATH,
            "printer_ip": "",
            "printer_port": "",
        }

        loaded_config = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as config_file:
                    loaded_config = json.load(config_file)
            except Exception as exc:
                self.maintenance_log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] Falha ao ler config.json: {exc}")

        runtime_config = {**default_config, **loaded_config}

        self.config_com_port = str(runtime_config.get("com_port", "") or "").strip()

        try:
            self.config_baud_rate = int(runtime_config.get("baudrate", DEFAULT_BAUD_RATE))
        except (TypeError, ValueError):
            self.config_baud_rate = DEFAULT_BAUD_RATE

        self.printer_mode_var.set(self.config_mode_to_ui(runtime_config.get("printer_mode")))

        # backward compatibility: accept legacy key "printer_name"
        printer_path = runtime_config.get("printer_path") or runtime_config.get("printer_name") or DEFAULT_SHARED_PRINTER_PATH
        self.printer_name_var.set(str(printer_path).strip())
        self.printer_ip_var.set(str(runtime_config.get("printer_ip", "") or "").strip())
        self.printer_port_var.set(str(runtime_config.get("printer_port", "") or "").strip())

    def save_runtime_config(self):
        config_data = {
            "com_port": self.config_com_port,
            "baudrate": self.config_baud_rate,
            "printer_mode": self.printer_mode_to_config(),
            "printer_path": self.printer_name_var.get().strip(),
            "printer_ip": self.printer_ip_var.get().strip(),
            "printer_port": self.printer_port_var.get().strip(),
        }

        with open(self.config_path, "w", encoding="utf-8") as config_file:
            json.dump(config_data, config_file, ensure_ascii=False, indent=2)

    def load_local_printers(self):
        if not self.printer_name_var.get().strip():
            self.printer_name_var.set(DEFAULT_SHARED_PRINTER_PATH)
        self.append_maintenance_log(f"Destino de impressão compartilhado: {self.printer_name_var.get()}")

    def toggle_maintenance_window(self, event=None):
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
        self.maintenance_window.geometry("760x560")
        self.maintenance_window.resizable(False, False)
        self.maintenance_window.withdraw()

        container = tk.Frame(self.maintenance_window, padx=14, pady=12, bg="#ffffff")
        container.pack(fill="both", expand=True)

        tk.Label(container, text="Porta COM", font=("Arial", 10, "bold")).grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_port_var = tk.StringVar(value=self.config_com_port)
        self.maintenance_port_entry = tk.Entry(container, textvariable=self.maintenance_port_var, width=20)
        self.maintenance_port_entry.grid(row=0, column=1, padx=(0, 16), pady=4, sticky="w")

        tk.Label(container, text="Velocidade", font=("Arial", 10, "bold")).grid(row=1, column=0, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_baud_var = tk.StringVar(value=str(self.config_baud_rate or DEFAULT_BAUD_RATE))
        self.maintenance_baud_entry = tk.Entry(container, textvariable=self.maintenance_baud_var, width=20)
        self.maintenance_baud_entry.grid(row=1, column=1, padx=(0, 16), pady=4, sticky="w")

        tk.Label(container, text="Compartilhamento", font=("Arial", 10, "bold")).grid(row=2, column=0, padx=(0, 8), pady=4, sticky="w")
        self.maintenance_printer_entry = tk.Entry(container, textvariable=self.printer_name_var, width=36)
        self.maintenance_printer_entry.grid(row=2, column=1, padx=(0, 16), pady=4, sticky="w")

        self.apply_maintenance_btn = tk.Button(container, text="Aplicar ajustes", command=self.apply_maintenance_settings)
        self.apply_maintenance_btn.grid(row=3, column=0, columnspan=2, pady=(10, 10), sticky="w")

        self.maintenance_test_btn = tk.Button(container, text="Testar impressora", command=self.test_printer)
        self.maintenance_test_btn.grid(row=4, column=0, columnspan=2, pady=(0, 10), sticky="w")

        tk.Label(container, text="Log de comunicação", font=("Arial", 10, "bold")).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 4))
        self.maintenance_log = tk.Text(container, height=16, width=92, wrap="word")
        self.maintenance_log.grid(row=6, column=0, columnspan=2, sticky="nsew")

        self.refresh_maintenance_window()
        self.maintenance_window.protocol("WM_DELETE_WINDOW", self.maintenance_window.withdraw)
        self.maintenance_window.deiconify()

    def refresh_maintenance_window(self):
        if self.maintenance_window is None or not self.maintenance_window.winfo_exists():
            return

        self.maintenance_port_var.set(self.config_com_port)
        self.maintenance_baud_var.set(str(self.config_baud_rate or DEFAULT_BAUD_RATE))
        self.maintenance_log.configure(state="normal")
        self.maintenance_log.delete("1.0", tk.END)
        for line in self.maintenance_log_lines:
            self.maintenance_log.insert(tk.END, f"{line}\n")
        self.maintenance_log.configure(state="disabled")

    def apply_maintenance_settings(self):
        previous_port = self.config_com_port
        previous_baud = self.config_baud_rate

        port_override = self.maintenance_port_var.get().strip()
        baud_override = self.maintenance_baud_var.get().strip()
        printer_override = self.printer_name_var.get().strip()

        self.config_com_port = port_override

        try:
            self.config_baud_rate = int(baud_override)
        except ValueError:
            self.config_baud_rate = DEFAULT_BAUD_RATE

        if not printer_override:
            self.printer_name_var.set(DEFAULT_SHARED_PRINTER_PATH)
            printer_override = DEFAULT_SHARED_PRINTER_PATH

        printer_display = printer_override or "nenhum compartilhamento configurado"
        self.append_maintenance_log(
            f"Ajustes aplicados: porta={self.config_com_port or '-'}, "
            f"velocidade={self.config_baud_rate}, destino={printer_display}"
        )

        try:
            self.save_runtime_config()
            self.append_maintenance_log("config.json atualizado com sucesso.")
        except Exception as exc:
            self.last_error_var.set(str(exc))
            self.append_maintenance_log(f"Falha ao salvar config.json: {exc}")
            return

        self.status_label.set("Configurações aplicadas. Reiniciando conexão...")
        if self.config_com_port != previous_port or self.config_baud_rate != previous_baud:
            self.append_maintenance_log("Parâmetros de comunicação alterados. Reiniciando conexão com o G2000...")
        else:
            self.append_maintenance_log("Configuração salva. Reiniciando conexão para aplicar estado atual...")

        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
        self.schedule_reconnect()

    def append_maintenance_log(self, message):
        self.maintenance_log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        if len(self.maintenance_log_lines) > 120:
            self.maintenance_log_lines = self.maintenance_log_lines[-120:]
        self.refresh_maintenance_window()

    def auto_connect(self):
        self.reconnect_job = None

        if self.serial_port and self.serial_port.is_open:
            return

        port = self.config_com_port
        if not port:
            self.g2000_status_var.set("� Configurar COM")
            self.set_g2000_status_color("error")
            self.last_error_var.set("Porta COM não configurada em config.json")
            self.status_label.set("Configure a porta COM na janela de manutenção.")
            self.append_maintenance_log("Porta COM não configurada em config.json. Aguardando ajustes...")
            self.schedule_reconnect()
            return

        self.current_port_name = port
        self.append_maintenance_log(f"Tentando conectar na porta fixa: {port}")
        try:
            self.serial_port = serial.Serial(
                port=port,
                baudrate=self.config_baud_rate or DEFAULT_BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
                xonxoff=False,
                rtscts=False,
            )
        except Exception as exc:
            self.g2000_status_var.set("� Falha na conexão")
            self.set_g2000_status_color("error")
            self.last_error_var.set(f"{port}: {exc}")
            self.status_label.set(f"Tentando reconectar em {port}...")
            self.append_maintenance_log(f"Falha ao conectar em {port}: {exc}")
            self.schedule_reconnect()
            return

        self.stop_thread = False
        self.packet_buffer.clear()
        self.total_bytes = 0
        self.last_data_time = time.monotonic()
        self.last_connected_port = port

        self.g2000_status_var.set("🟢 Conectado")
        self.set_g2000_status_color("connected")
        self.last_error_var.set("Nenhum")
        self.status_label.set("Aguardando nova medição...")
        self.append_maintenance_log(f"Conectado em {port} com {self.config_baud_rate or DEFAULT_BAUD_RATE} bps")
        self.root.iconify()
        printer_port_text = self.printer_port_var.get().strip()
        try:
            printer_port = int(printer_port_text) if printer_port_text else 0
        except ValueError:
            printer_port = 0
        self.open_capture_file(port, self.config_baud_rate or DEFAULT_BAUD_RATE, self.printer_ip_var.get().strip(), printer_port, self.printer_mode_var.get())
        self.check_printer_status()
        self.read_thread = threading.Thread(target=self.read_loop, daemon=True)
        self.read_thread.start()

    def schedule_reconnect(self):
        if self.reconnect_job is not None:
            return

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
            if not printer_name:
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                self.last_error_var.set("Compartilhamento da impressora não configurado")
                return

            if not self.is_shared_printer_path(printer_name):
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                self.last_error_var.set(r"Use o formato \\servidor\impressora")
                return

            if self.check_shared_printer_available(printer_name):
                self.printer_status_var.set("🟢 Conectada")
                self.set_printer_status_color("connected")
                self.last_error_var.set("Nenhum")
                return

            self.printer_status_var.set("🔴 Offline")
            self.set_printer_status_color("error")
            self.last_error_var.set("Compartilhamento da impressora indisponível")
            return

        printer_ip = printer_ip or self.printer_ip_var.get().strip()
        printer_port_text = self.printer_port_var.get().strip()
        if printer_port is None:
            if not printer_port_text:
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                self.last_error_var.set("Porta TCP da impressora não configurada")
                return
            try:
                printer_port = int(printer_port_text)
            except ValueError:
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                self.last_error_var.set("Porta TCP da impressora inválida")
                return
        try:
            with socket.create_connection((printer_ip, printer_port), timeout=2):
                self.printer_status_var.set("🟢 Conectada")
                self.set_printer_status_color("connected")
                self.last_error_var.set("Nenhum")
        except Exception as exc:
            self.printer_status_var.set("🔴 Offline")
            self.set_printer_status_color("error")
            self.last_error_var.set(str(exc))

    def test_printer(self):
        printer_name = self.printer_name_var.get().strip()
        if self.printer_mode_var.get() == DEFAULT_PRINTER_MODE and not printer_name:
            self.last_error_var.set("Compartilhamento da impressora não configurado")
            return

        printer_port_text = self.printer_port_var.get().strip()
        if self.printer_mode_var.get() != DEFAULT_PRINTER_MODE:
            if not self.printer_ip_var.get().strip() or not printer_port_text:
                self.last_error_var.set("Impressora TCP/IP não configurada")
                return
            try:
                printer_port = int(printer_port_text)
            except ValueError:
                self.last_error_var.set("Porta TCP da impressora inválida")
                return
        else:
            printer_port = 0

        test_payload = (
            "**********************\n"
            "TESTE\n"
            "Impressora OK\n"
            f"{datetime.now().strftime('%H:%M:%S')}\n"
            "**********************\n"
        )

        success = self.send_to_printer(
            test_payload,
            self.printer_mode_var.get(),
            self.printer_ip_var.get().strip(),
            printer_port,
            printer_name=printer_name,
        )
        if success:
            self.last_print_var.set(f"{datetime.now().strftime('%H:%M:%S')} OK")
            self.last_error_var.set("Nenhum")
        else:
            self.last_print_var.set("Erro")

    def disconnect(self):
        self.stop_thread = True
        if self.reconnect_job is not None:
            self.root.after_cancel(self.reconnect_job)
            self.reconnect_job = None
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
        self.serial_port = None
        self.g2000_status_var.set("� Desconectado")
        self.status_label.set("Aguardando dispositivo G2000...")
        if self.capture_file:
            self.capture_file.close()
            self.capture_file = None

    def open_capture_file(self, port, baud_rate, printer_ip, printer_port, printer_mode):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.capture_filename = os.path.join(os.getcwd(), f"captura_{timestamp}.log")
        self.capture_file = open(self.capture_filename, "w", encoding="utf-8")
        self.capture_file.write("GEHAKA G2000 - IMPRESSÃO DE TICKETS\n")
        self.capture_file.write(f"Porta: {port}\n")
        self.capture_file.write(f"Velocidade: {baud_rate}\n")
        self.capture_file.write(f"Modo impressora: {printer_mode}\n")
        if printer_mode == "Rede (TCP/IP)":
            self.capture_file.write(f"Impressora: {printer_ip}:{printer_port}\n")
        else:
            self.capture_file.write(f"Compartilhamento: {self.printer_name_var.get().strip()}\n")
        self.capture_file.write("\n")
        self.capture_file.flush()

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
            if self.packet_buffer and (now - self.last_data_time) > 0.35:
                self.finalize_packet()

            self.packet_buffer.extend(data)
            self.last_data_time = now
            self.total_bytes += len(data)

            if data == b"\x03":
                self.finalize_packet()

        self.root.after(0, self.handle_disconnection)

    def handle_disconnection(self, reason=None):
        if reason:
            self.last_error_var.set(reason)
        self.disconnect()
        self.g2000_status_var.set("� Reconectando...")
        self.status_label.set("Aguardando retorno do dispositivo...")
        self.schedule_reconnect()

    def finalize_packet(self):
        if not self.packet_buffer:
            return

        raw_packet = bytes(self.packet_buffer)
        raw_text = raw_packet.decode("latin1", errors="replace")
        cleaned_text = self.clean_packet_text(raw_text)
        parsed_fields = self.parse_measurement_fields(cleaned_text)
        measurement_data = self.build_measurement_data(parsed_fields)
        ticket_text = self.format_ticket_text(measurement_data)

        self.measurements_received += 1
        self.measurements_count_var.set(str(self.measurements_received))
        self.status_label.set("Nova medição recebida.")

        self.root.after(0, self.set_measurement_text, ticket_text)
        self.root.after(0, self.print_measurement, ticket_text)

        if self.capture_file:
            self.capture_file.write(f"\n--- Medição {self.measurements_received} ---\n")
            self.capture_file.write("[RAW]\n")
            self.capture_file.write(cleaned_text)
            self.capture_file.write("\n\n[FORMATADO]\n")
            self.capture_file.write(ticket_text)
            self.capture_file.write("\n")
            self.capture_file.flush()

        self.packet_buffer.clear()

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
            "Gaasa e Alimentos Ltda",
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
            lines.extend(["", line_sep])

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

    def build_ticket_file_bytes(self, text):
        corta_papel = bytes([29, 86, 66, 0])

        payload = bytearray()
        lines = text.splitlines()

        for line in lines:
            encoded_line = line.encode("latin1", errors="replace")
            payload.extend(encoded_line)
            payload.extend(b"\r\n")

        payload.extend(b"\r\n\r\n\r\n")
        payload.extend(corta_papel)
        return bytes(payload)

    def get_generated_ticket_paths(self):
        return (
            os.path.join(PROJECT_ROOT, GENERATED_TICKET_FILENAME),
            os.path.join(PROJECT_ROOT, GENERATED_TICKET_PREVIEW_FILENAME),
        )

    def save_generated_ticket_files(self, text):
        printer_file_path, preview_file_path = self.get_generated_ticket_paths()
        normalized_text = (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        payload_bytes = self.build_ticket_file_bytes(normalized_text)

        with open(printer_file_path, "wb") as ticket_file:
            ticket_file.write(payload_bytes)

        with open(preview_file_path, "w", encoding="latin1", newline="\r\n") as preview_file:
            preview_file.write(normalized_text)
            preview_file.write("\r\n")

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

    def send_text_file_to_shared_printer(self, text, printer_path):
        ticket_file = None
        try:
            ticket_file = self.write_ticket_text_file(text)
            command_args = ["cmd.exe", "/c", "copy", "/b", "/y", ticket_file, printer_path]
            command_preview = " ".join(f'"{arg}"' if " " in arg else arg for arg in command_args)
            self.append_maintenance_log(f"Executando comando: {command_preview}")
            completed = subprocess.run(
                command_args,
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0:
                self.append_maintenance_log(f"Arquivo de ticket enviado para {printer_path}: {ticket_file}")
                return True

            detail = completed.stderr.strip() or completed.stdout.strip() or "copy /b retornou erro"
            self.last_error_var.set(detail)
            self.append_maintenance_log(f"Falha no copy /b para {printer_path}: {detail}")
            return False
        except Exception as exc:
            self.last_error_var.set(str(exc))
            self.append_maintenance_log(f"Falha ao gerar/enviar ticket TXT para {printer_path}: {exc}")
            return False

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
            if not printer_name:
                self.printer_status_var.set("🔴 Offline")
                self.set_printer_status_color("error")
                self.last_error_var.set("Compartilhamento da impressora não configurado")
                return False

            if self.send_text_file_to_shared_printer(payload, printer_name):
                self.printer_status_var.set("🟢 Online")
                self.set_printer_status_color("connected")
                return True

            self.printer_status_var.set("🔴 Offline")
            self.set_printer_status_color("error")
            if not self.last_error_var.get() or self.last_error_var.get() == "Nenhum":
                self.last_error_var.set("Falha ao enviar ticket TXT para o compartilhamento da impressora")
            return False

        try:
            with socket.create_connection((printer_ip, printer_port), timeout=3) as sock:
                sock.sendall(payload_bytes)
                self.printer_status_var.set("🟢 Online")
                self.set_printer_status_color("connected")
                return True
        except Exception as exc:
            self.printer_status_var.set("🔴 Offline")
            self.set_printer_status_color("error")
            self.last_error_var.set(str(exc))
            return False

    def set_g2000_status(self, value):
        self.g2000_status_var.set(value)

    def set_printer_status(self, value):
        self.printer_status_var.set(value)


def main():
    root = tk.Tk()
    app = GehakaMonitorBridgeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()