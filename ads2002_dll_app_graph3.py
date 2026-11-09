"""
ADS2002-IP-B-3 / AC2122 — interface Python reconstruída para WAC2122A.DLL.
Com suporte a transmissão via Porta Série (COM) em formato CSV, detecção avançada (com0com),
canais a transmitir organizados abaixo e intervalo de envio em milissegundos (ms).

v6 — Integração com o Dynomax Launcher
  * Emite eventos em stdout, uma linha por evento:  @@ADS@@ {"event": "connected", ...}
    (ready, connected, serial_open, acq_started, error, closed).
  * --stdin-control : encerra de forma limpa quando recebe "quit" no stdin (ou EOF).
  * --no-auto       : ignora as ações "Ao abrir" (modo edição).

v5 — Configuração em JSON
  * Botões "Salvar config..." / "Carregar config..." (JSON completo: IP, DLL, módulo,
    frequência, canais, porta COM, opções de início automático).
  * Linha de comando para lançadores externos:
        python ads2002_dll_app_graph5.py --config maquina1.json
        python ads2002_dll_app_graph5.py --config maquina1.json --auto-connect --auto-start
    Com --config, a gravação automática de ads2002_config.json é desativada, para que
    várias instâncias abertas ao mesmo tempo não sobrescrevam a configuração umas das outras.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import c_byte, c_short, c_int, c_float, POINTER
import csv
import json
import platform
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from datetime import datetime

# Importação para suporte a porta COM
import serial
import serial.tools.list_ports

try:
    import winreg  # Específico para Windows (leitura de portas do registro)
except ImportError:
    winreg = None

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "ads2002_config.json"
BUF_SIZE = 16384
SCHEMA = "ads2000-config"
CFG_VERSION = 5
DEFAULT_RANGES = [-10.0, -5.0, -2.0, -1.0]
BASE_TITLE = "ADS2002-IP-B-3 / AC2122 — Aquisição & Serial"


class TpUserBuf(ctypes.Structure):
    _fields_ = [("Buffer", ctypes.c_int16 * BUF_SIZE)]


class LynxDLL:
    """Camada de acesso à DLL da Lynx; não contém código da interface."""

    def __init__(self, dll_path: str):
        if struct.calcsize("P") * 8 != 32:
            raise OSError(
                "A WAC2122A.DLL é x86/PE32. Execute com Python de 32 bits (x86)."
            )
        if platform.system() != "Windows":
            raise OSError("A WAC2122A.DLL só pode ser carregada no Windows.")
        self.dll_path = str(Path(dll_path).resolve())
        self.dll = ctypes.WinDLL(self.dll_path)
        self._bind()

    def _bind(self):
        d = self.dll
        d.OpenDriver.argtypes = []
        d.OpenDriver.restype = c_byte
        d.CloseDriver.argtypes = []
        d.CloseDriver.restype = None
        d.GetVersion.argtypes = [POINTER(c_byte), POINTER(c_byte)]
        d.GetVersion.restype = None
        d.GetAiCaps.argtypes = [POINTER(c_short), POINTER(c_short), POINTER(c_float)]
        d.GetAiCaps.restype = None
        d.ConnectAC2122.argtypes = [ctypes.c_char_p]
        d.ConnectAC2122.restype = c_byte
        d.DisconnectAC2122.argtypes = []
        d.DisconnectAC2122.restype = None
        d.Clear_CM.argtypes = []
        d.Clear_CM.restype = None
        d.Insert_AI_CM.argtypes = [c_byte, c_byte, c_byte]
        d.Insert_AI_CM.restype = c_byte
        d.AcquisitionSetup.argtypes = [c_byte, c_float, POINTER(c_float), POINTER(c_int)]
        d.AcquisitionSetup.restype = c_byte
        d.StartAcquisition.argtypes = []
        d.StartAcquisition.restype = c_byte
        d.StopAcquisition.argtypes = []
        d.StopAcquisition.restype = None
        d.GetSamples.argtypes = [
            POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(TpUserBuf)
        ]
        d.GetSamples.restype = c_byte

    def open_driver(self):
        return bool(self.dll.OpenDriver())

    def close_driver(self):
        self.dll.CloseDriver()

    def get_version(self):
        hi, lo = c_byte(), c_byte()
        self.dll.GetVersion(ctypes.byref(hi), ctypes.byref(lo))
        return hi.value, lo.value

    def connect(self, ip: str):
        return bool(self.dll.ConnectAC2122(ip.encode("ascii") + b"\0"))

    def disconnect(self):
        self.dll.DisconnectAC2122()

    def get_ai_caps(self):
        nch, nr = c_short(), c_short()
        ranges = (c_float * 16)()
        self.dll.GetAiCaps(ctypes.byref(nch), ctypes.byref(nr), ranges)
        return nch.value, nr.value, list(ranges)

    def clear_channels(self):
        self.dll.Clear_CM()

    def insert_channel(self, module_addr: int, channel: int, range_index: int):
        return bool(self.dll.Insert_AI_CM(module_addr, channel, range_index))

    def setup_acquisition(self, external_clock: bool, requested_hz: float):
        actual_hz, error_code = c_float(), c_int()
        ok = bool(self.dll.AcquisitionSetup(
            c_byte(1 if external_clock else 0), c_float(requested_hz),
            ctypes.byref(actual_hz), ctypes.byref(error_code)
        ))
        return ok, actual_hz.value, error_code.value

    def start(self):
        return bool(self.dll.StartAcquisition())

    def stop(self):
        self.dll.StopAcquisition()

    def get_samples(self, n_channels: int):
        error_code, last_sample, samples_read = c_int(), c_int(), c_int()
        user_buf = TpUserBuf()
        ok = bool(self.dll.GetSamples(
            ctypes.byref(error_code), ctypes.byref(last_sample),
            ctypes.byref(samples_read), ctypes.byref(user_buf)
        ))
        n_rows = max(0, min(samples_read.value, BUF_SIZE // max(1, n_channels)))
        n_values = n_rows * n_channels
        values = list(user_buf.Buffer[:n_values])
        return ok, error_code.value, last_sample.value, n_rows, values


class App:
    RANGE_LABELS = {0: "-10V a 10V", 1: "-5V a 5V", 2: "-2V a 2V", 3: "-1V a 1V"}

    def __init__(self, root: tk.Tk, config_path: Path | None = None, cli_flags: dict | None = None):
        self.root = root
        self.root.title(BASE_TITLE)
        self.root.geometry("1250x960")
        self.root.minsize(950, 780)

        # Com --config: não grava o arquivo padrão (evita conflito entre instâncias)
        self.config_path = config_path
        self.config_file = None if config_path else CONFIG_FILE
        self._quit_requested = False

        self.api = None
        self.driver_open = False
        self.connected = False
        self.acq_started = False
        self.csv_handle = None
        self.csv_writer = None

        # Variáveis para a Porta Série
        self.serial_conn = None
        self.serial_connected = False
        self.serial_thread = None
        self.serial_stop_event = threading.Event()
        self.latest_sample_data = {}
        # Cópias simples dos parâmetros série, lidas pela thread (evita usar variáveis Tk fora da thread principal)
        self._serial_period_s = 0.02
        self._serial_channels = []

        self.poll_after_id = None
        self.plot_times = []
        self.plot_values = []
        self.sample_index = 0

        self.ai_ranges = list(DEFAULT_RANGES)
        self.active = []

        default_dll = APP_DIR / "WAC2122A.DLL"
        self.machine_name = tk.StringVar(value="")
        self.dll_path = tk.StringVar(value=str(default_dll))
        self.ip = tk.StringVar(value="192.168.0.10")
        self.sample_rate = tk.StringVar(value="1000")
        self.external_clock = tk.BooleanVar(value=False)
        self.fixed_axis = tk.BooleanVar(value=True)
        self.module_addr = tk.StringVar(value="16")
        self.csv_path = tk.StringVar(value=str(APP_DIR / "ads2002_samples.csv"))

        # Início automático (útil quando a interface é aberta por um lançador)
        self.auto_connect = tk.BooleanVar(value=False)
        self.auto_open_serial = tk.BooleanVar(value=False)
        self.auto_start = tk.BooleanVar(value=False)

        # Variáveis da Porta Série avançadas (Período em ms)
        self.com_port = tk.StringVar(value="COM1")
        self.com_baudrate = tk.StringVar(value="115200")
        self.serial_period_ms = tk.StringVar(value="20")  # Padrão: 20 ms (equivalente a 50 Hz)
        self.serial_ch_vars = [tk.BooleanVar(value=(1 <= i <= 4)) for i in range(16)]

        self.plot_channel = tk.StringVar(value="Canal 1")
        self.status = tk.StringVar(value="Desconectado")
        self.sample_status = tk.StringVar(value="Nenhuma amostra recebida")

        # Canal 1 por padrão (a tabela só mostra os canais 1 a 8)
        self.channel_vars = [tk.BooleanVar(value=(i == 1)) for i in range(16)]
        self.sensor_vars = [tk.StringVar(value="None") for i in range(16)]
        self.unit_vars = [tk.StringVar(value="-") for i in range(16)]
        self.coef_vars = [tk.StringVar(value="1.0") for i in range(16)]
        self.offset_vars = [tk.StringVar(value="0.0") for i in range(16)]
        self.range_vars = [tk.StringVar(value="0 — -10V a 10V") for i in range(16)]

        self.serial_period_ms.trace_add("write", lambda *_: self._sync_serial_params())
        for v in self.serial_ch_vars:
            v.trace_add("write", lambda *_: self._sync_serial_params())
        self.machine_name.trace_add("write", lambda *_: self._update_title())

        self._build_ui()
        self._load_config()
        # Opções passadas na linha de comando têm prioridade sobre o JSON
        for key, var in (("auto_connect", self.auto_connect),
                         ("auto_open_serial", self.auto_open_serial),
                         ("auto_start", self.auto_start)):
            if cli_flags and cli_flags.get(key):
                var.set(True)
        if cli_flags and cli_flags.get("no_auto"):
            self.auto_connect.set(False)
            self.auto_open_serial.set(False)
            self.auto_start.set(False)
        self._sync_serial_params()
        self._update_title()
        self.root.protocol("WM_DELETE_WINDOW", self.close_all)
        self.draw_plot()
        if cli_flags and cli_flags.get("stdin_control"):
            self._start_stdin_control()
        self._emit("ready", machine=self.machine_name.get().strip(),
                   config=str(config_path) if config_path else None)
        if self.auto_connect.get() or self.auto_open_serial.get() or self.auto_start.get():
            self.root.after(400, self._run_startup)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # Frame de Configuração (JSON)
        cfgf = ttk.LabelFrame(outer, text="Configuração (JSON)", padding=6)
        cfgf.pack(fill="x", pady=(0, 8))
        ttk.Label(cfgf, text="Máquina:").pack(side="left")
        ttk.Entry(cfgf, textvariable=self.machine_name, width=22).pack(side="left", padx=4)
        ttk.Button(cfgf, text="Salvar config...", command=self.export_config).pack(side="left", padx=4)
        ttk.Button(cfgf, text="Carregar config...", command=self.import_config).pack(side="left", padx=4)
        ttk.Label(cfgf, text="Ao abrir:").pack(side="left", padx=(16, 4))
        ttk.Checkbutton(cfgf, text="conectar", variable=self.auto_connect).pack(side="left", padx=3)
        ttk.Checkbutton(cfgf, text="abrir COM", variable=self.auto_open_serial).pack(side="left", padx=3)
        ttk.Checkbutton(cfgf, text="iniciar aquisição", variable=self.auto_start).pack(side="left", padx=3)

        # Frame de Conexão LAN/DLL
        conn = ttk.LabelFrame(outer, text="Conexão e Driver (AC2122)", padding=8)
        conn.pack(fill="x")
        ttk.Label(conn, text="DLL:").grid(row=0, column=0, sticky="w")
        ttk.Entry(conn, textvariable=self.dll_path, width=65).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(conn, text="Procurar...", command=self.choose_dll).grid(row=0, column=2, padx=3)
        ttk.Label(conn, text="IP do AC2122:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(conn, textvariable=self.ip, width=15).grid(row=1, column=1, sticky="w", padx=3)
        self.connect_btn = ttk.Button(conn, text="Conectar", command=self.connect)
        self.connect_btn.grid(row=1, column=2, padx=3)
        self.disconnect_btn = ttk.Button(conn, text="Desconectar", command=self.disconnect, state="disabled")
        self.disconnect_btn.grid(row=1, column=3, padx=3)
        self.status_label = ttk.Label(conn, textvariable=self.status)
        self.status_label.grid(row=1, column=4, columnspan=4, sticky="w", pady=(5, 0))
        conn.columnconfigure(1, weight=1)

        # Frame de Transmissão por Porta Série (COM)
        serial_frame = ttk.LabelFrame(outer, text="Transmissão Porta Série", padding=8)
        serial_frame.pack(fill="x", pady=8)

        s_row0 = ttk.Frame(serial_frame)
        s_row0.pack(fill="x", pady=2)

        ttk.Label(s_row0, text="Porta COM:").pack(side="left")
        self.com_combo = ttk.Combobox(s_row0, textvariable=self.com_port, state="normal", width=10)
        self.com_combo.pack(side="left", padx=4)

        ttk.Button(s_row0, text="Atualizar", command=self.refresh_ports).pack(side="left", padx=2)

        ttk.Label(s_row0, text="Baud:").pack(side="left", padx=(10, 2))
        ttk.Combobox(s_row0, textvariable=self.com_baudrate,
                     values=["9600", "19200", "38400", "57600", "115200", "230400", "921600"],
                     width=8, state="readonly").pack(side="left", padx=4)

        ttk.Label(s_row0, text="Período (ms):").pack(side="left", padx=(10, 2))
        ttk.Entry(s_row0, textvariable=self.serial_period_ms, width=6).pack(side="left", padx=4)

        self.serial_connect_btn = ttk.Button(s_row0, text="Abrir Porta COM", command=self.toggle_serial)
        self.serial_connect_btn.pack(side="left", padx=8)

        self.serial_status_label = ttk.Label(s_row0, text="COM: Fechada", foreground="gray")
        self.serial_status_label.pack(side="left", padx=4)

        ttk.Label(s_row0, text="Canais Tx:").pack(side="left", padx=(0, 6))
        for i in range(1, 9):
            ttk.Checkbutton(s_row0, text=f"C{i}", variable=self.serial_ch_vars[i]).pack(side="left", padx=4)

        # Frame de Configuração de Aquisição
        setup = ttk.LabelFrame(outer, text="Configuração de Aquisição", padding=8)
        setup.pack(fill="x", pady=4)
        ttk.Label(setup, text="Endereço do Módulo (0x10 = 16):").grid(row=0, column=0, sticky="w")
        ttk.Entry(setup, textvariable=self.module_addr, width=8).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(setup, text="Frequência Solicitada (Hz):").grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Entry(setup, textvariable=self.sample_rate, width=12).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Checkbutton(setup, text="Clock Externo", variable=self.external_clock).grid(row=0, column=4, padx=10)
        self.configure_btn = ttk.Button(setup, text="Aplicar Configuração", command=self.configure, state="disabled")
        self.configure_btn.grid(row=0, column=5, padx=4)

        # Tabela de Canais
        chframe = ttk.LabelFrame(
            outer, text="Tabela de Configuração (valor = Coeficiente × Volts + Offset)", padding=8)
        chframe.pack(fill="x", pady=4)

        container_canvas = tk.Canvas(chframe, height=100, highlightthickness=0)
        scrollbar = ttk.Scrollbar(chframe, orient="vertical", command=container_canvas.yview)
        scrollable_interior = ttk.Frame(container_canvas)
        scrollable_interior.bind(
            "<Configure>",
            lambda e: container_canvas.configure(scrollregion=container_canvas.bbox("all"))
        )
        container_canvas.create_window((0, 0), window=scrollable_interior, anchor="nw")
        container_canvas.configure(yscrollcommand=scrollbar.set)
        container_canvas.pack(side="left", fill="x", expand=True)
        scrollbar.pack(side="right", fill="y")

        headers = ["Ativar", "Canal", "Sensor", "Unidade", "Coeficiente", "Offset", "Faixa"]
        for col_idx, text in enumerate(headers):
            lbl = ttk.Label(scrollable_interior, text=text, font=("Segoe UI", 9, "bold"))
            lbl.grid(row=0, column=col_idx, padx=6, pady=(2, 6), sticky="w" if col_idx in (2, 6) else "ew")

        for ch in range(1, 9):
            r = ch + 1
            ttk.Checkbutton(scrollable_interior, variable=self.channel_vars[ch]).grid(
                row=r, column=0, padx=12, pady=2, sticky="w")
            ttk.Label(scrollable_interior, text=f"Canal {ch}", anchor="center").grid(
                row=r, column=1, padx=6, pady=2, sticky="ew")
            ttk.Entry(scrollable_interior, textvariable=self.sensor_vars[ch], width=16).grid(
                row=r, column=2, padx=6, pady=2, sticky="ew")
            ttk.Entry(scrollable_interior, textvariable=self.unit_vars[ch], width=8).grid(
                row=r, column=3, padx=6, pady=2, sticky="ew")
            ttk.Entry(scrollable_interior, textvariable=self.coef_vars[ch], width=12).grid(
                row=r, column=4, padx=6, pady=2, sticky="ew")
            ttk.Entry(scrollable_interior, textvariable=self.offset_vars[ch], width=12).grid(
                row=r, column=5, padx=6, pady=2, sticky="ew")
            ttk.Combobox(scrollable_interior, textvariable=self.range_vars[ch], width=16, state="readonly",
                         values=[f"{i} — {self.RANGE_LABELS[i]}" for i in self.RANGE_LABELS]).grid(
                row=r, column=6, padx=6, pady=2, sticky="ew")

        # Botões de Ação de Aquisição
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=8)
        self.start_btn = ttk.Button(actions, text="Iniciar Aquisição", command=self.start, state="disabled")
        self.start_btn.pack(side="left", padx=(0, 5))
        self.stop_btn = ttk.Button(actions, text="Parar", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        ttk.Button(actions, text="Limpar Gráfico", command=self.clear_plot).pack(side="left", padx=5)
        ttk.Checkbutton(actions, text="Eixo Y fixo (faixa)", variable=self.fixed_axis,
                        command=self.draw_plot).pack(side="left", padx=5)
        ttk.Label(actions, text="Canal no Gráfico:").pack(side="left", padx=(15, 3))
        self.plot_combo = ttk.Combobox(actions, textvariable=self.plot_channel, width=12, state="readonly",
                                       values=[f"Canal {i}" for i in range(1, 9)])
        self.plot_combo.pack(side="left")
        self.plot_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_plot_channel_change())

        csvframe = ttk.Frame(outer)
        csvframe.pack(fill="x", pady=(0, 8))
        ttk.Label(csvframe, text="Arquivo CSV:").pack(side="left")
        ttk.Entry(csvframe, textvariable=self.csv_path).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(csvframe, text="Salvar Como...", command=self.choose_csv).pack(side="left")

        self.sample_label = ttk.Label(outer, textvariable=self.sample_status)
        self.sample_label.pack(anchor="w")
        self.plot_canvas = tk.Canvas(outer, background="white", height=200, highlightthickness=1,
                                     highlightbackground="#bdbdbd")
        self.plot_canvas.pack(fill="both", expand=True, pady=6)

        logframe = ttk.LabelFrame(outer, text="Registro (Log)", padding=5)
        logframe.pack(fill="both", expand=False)
        self.log_text = tk.Text(logframe, height=5, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

        self.refresh_ports()

    def log(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _update_title(self):
        name = self.machine_name.get().strip()
        self.root.title(f"{name} — {BASE_TITLE}" if name else BASE_TITLE)

    # ------------------------------------------- comunicação com o Launcher
    @staticmethod
    def _emit(event, **data):
        """Uma linha em stdout por evento; lida pelo Dynomax Launcher."""
        try:
            print("@@ADS@@ " + json.dumps({"event": event, **data}), flush=True)
        except Exception:
            pass

    def _start_stdin_control(self):
        def worker():
            try:
                for line in sys.stdin:
                    if line.strip().lower() == "quit":
                        break
            except Exception:
                pass
            self._quit_requested = True      # "quit" ou EOF (Launcher fechou)
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(200, self._check_quit)

    def _check_quit(self):
        if self._quit_requested:
            self.close_all()
            return
        self.root.after(200, self._check_quit)

    # ------------------------------------------------------------- série
    def refresh_ports(self):
        ports = []
        try:
            for p in serial.tools.list_ports.comports():
                if p.device not in ports:
                    ports.append(p.device)
        except Exception:
            pass

        if winreg is not None:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
                for i in range(winreg.QueryInfoKey(key)[1]):
                    try:
                        _name, val, _ = winreg.EnumValue(key, i)
                        if val not in ports:
                            ports.append(val)
                    except OSError:
                        break
                winreg.CloseKey(key)
            except OSError:
                pass

        if not ports:
            ports = ["COM1", "COM2", "CNCA0", "CNCB0"]

        self.com_combo['values'] = ports
        if not self.com_port.get() or self.com_port.get() not in ports:
            self.com_port.set(ports[0])
        self.log(f"Portas COM atualizadas: {ports}")

    def toggle_serial(self):
        if self.serial_connected:
            try:
                self._stop_serial_thread()
                if self.serial_conn and self.serial_conn.is_open:
                    self.serial_conn.close()
                self.serial_connected = False
                self.serial_conn = None
                self.serial_connect_btn.configure(text="Abrir Porta COM")
                self.serial_status_label.configure(text="COM: Fechada", foreground="gray")
                self.log("Porta série fechada.")
            except Exception as e:
                messagebox.showerror("Erro", f"Erro ao fechar porta série: {e}")
        else:
            try:
                port = self.com_port.get()
                baud = int(self.com_baudrate.get())
                self._sync_serial_params()
                self.serial_conn = serial.Serial(port=port, baudrate=baud, timeout=0.1)
                self.serial_connected = True
                self._start_serial_thread()
                self.serial_connect_btn.configure(text="Fechar Porta COM")
                self.serial_status_label.configure(text="COM: Aberta", foreground="green")
                self.log(f"Porta série {port} aberta com sucesso a {baud} bauds.")
                self._emit("serial_open", port=port, baud=baud)
            except Exception as e:
                self.serial_connected = False
                self.serial_conn = None
                self._emit("error", stage="serial", message=str(e))
                messagebox.showerror("Erro de Porta Série", f"Não foi possível abrir a porta {self.com_port.get()}:\n{e}")

    def _sync_serial_params(self):
        """Atualiza (na thread principal) as cópias simples usadas pela thread de envio."""
        try:
            p = float(str(self.serial_period_ms.get()).replace(",", "."))
        except (ValueError, tk.TclError):
            p = 20.0
        self._serial_period_s = (p / 1000.0) if p > 0 else 0.02
        try:
            self._serial_channels = [i for i in range(16) if self.serial_ch_vars[i].get()]
        except tk.TclError:
            pass

    def _start_serial_thread(self):
        self.serial_stop_event.clear()
        self.serial_thread = threading.Thread(target=self._serial_tx_worker, daemon=True)
        self.serial_thread.start()

    def _stop_serial_thread(self):
        self.serial_stop_event.set()
        if self.serial_thread and self.serial_thread.is_alive():
            self.serial_thread.join(timeout=0.5)

    def _serial_tx_worker(self):
        """Thread separada que envia periodicamente os dados com base no período em milissegundos (ms)."""
        while not self.serial_stop_event.is_set():
            if self.serial_connected and self.serial_conn and self.serial_conn.is_open:
                channels = list(self._serial_channels)
                if channels and self.latest_sample_data:
                    row_elements = [f"{self.latest_sample_data.get(ch, 0.0):.6f}" for ch in channels]
                    line = ",".join(row_elements) + "\r\n"
                    try:
                        self.serial_conn.write(line.encode("ascii"))
                    except Exception:
                        pass
            time.sleep(self._serial_period_s)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _parse_int(text):
        t = str(text).strip()
        try:
            return int(t, 0)
        except ValueError:
            return int(t)

    @staticmethod
    def _float_field(var, label, ch):
        try:
            return float(str(var.get()).replace(",", "."))
        except ValueError:
            raise ValueError(f"Canal {ch}: {label} inválido ('{var.get()}').")

    @staticmethod
    def _num_or_text(text, float_only=False):
        """Converte para número quando possível (JSON mais limpo); senão mantém o texto."""
        t = str(text).strip().replace(",", ".")
        try:
            f = float(t)
        except ValueError:
            return str(text)
        if float_only:
            return f
        return int(f) if f.is_integer() else f

    def _range_index(self, ch):
        return int(self.range_vars[ch].get().split("—")[0].strip())

    def raw_to_volts(self, raw, range_idx):
        r = self.ai_ranges[range_idx] if 0 <= range_idx < len(self.ai_ranges) else DEFAULT_RANGES[0]
        if r < 0:
            return raw * (-r) / 32768.0
        return raw * r / 65536.0 + r / 2.0

    def _plot_ch(self):
        try:
            return int(self.plot_channel.get().replace("Canal ", ""))
        except ValueError:
            return 0

    def _cal(self):
        return {ch: (ri, co, of) for ch, ri, co, of in self.active}

    def _plot_limits(self):
        ridx, coef, off = self._cal().get(self._plot_ch(), (0, 1.0, 0.0))
        r = self.ai_ranges[ridx] if 0 <= ridx < len(self.ai_ranges) else DEFAULT_RANGES[0]
        lo, hi = (r, -r) if r < 0 else (0.0, r)
        a, b = coef * lo + off, coef * hi + off
        return min(a, b), max(a, b)

    def _plot_unit(self):
        u = self.unit_vars[self._plot_ch()].get().strip()
        return "V" if u in ("", "-") else u

    def _on_plot_channel_change(self):
        self.clear_plot()

    # -------------------------------------------------------------- ações
    def choose_dll(self):
        path = filedialog.askopenfilename(title="Selecionar WAC2122A.DLL",
                                          filetypes=[("DLL", "*.dll"), ("Todos os arquivos", "*.*")])
        if path:
            self.dll_path.set(path)

    def connect(self):
        if self.connected:
            return
        try:
            dll_path = Path(self.dll_path.get()).expanduser()
            if not dll_path.is_file():
                raise FileNotFoundError(f"DLL não encontrada: {dll_path}")
            self.api = LynxDLL(str(dll_path))
            if not self.api.open_driver():
                self.api = None
                raise RuntimeError("OpenDriver retornou falso.")
            self.driver_open = True
            version = self.api.get_version()
            self.log(f"Driver aberto. Versão informada pela DLL: {version[0]}.{version[1]}")
            if not self.api.connect(self.ip.get().strip()):
                self.api.close_driver()
                self.driver_open = False
                self.api = None
                raise RuntimeError("ConnectAC2122 retornou falso. Verifique IP, rede e configuração.")
            self.connected = True
            nch, nr, ranges = self.api.get_ai_caps()
            self.log(f"Conectado ao AC2122. Capacidades: {nch} canais; {nr} faixas; vetor={ranges[:max(0, nr)]}")
            if 1 <= nr <= 16 and all(r != 0 for r in ranges[:nr]):
                self.ai_ranges = [float(r) for r in ranges[:nr]]
            self.status.set(f"Conectado ao dispositivo {self.ip.get().strip()}")
            self.connect_btn.configure(state="disabled")
            self.disconnect_btn.configure(state="normal")
            self.configure_btn.configure(state="normal")
            self.start_btn.configure(state="normal")
            self._save_config()
            self._emit("connected", ip=self.ip.get().strip(), channels_caps=nch)
        except Exception as exc:
            self.log(f"Falha de conexão: {exc}")
            self._emit("error", stage="connect", message=str(exc))
            messagebox.showerror("Falha de Conexão", str(exc))

    def disconnect(self):
        if self.acq_started:
            self.stop()
        if self.api and self.connected:
            try:
                self.api.disconnect()
                self.log("DisconnectAC2122 executado.")
            except Exception as exc:
                self.log(f"Erro em DisconnectAC2122: {exc}")
        if self.api and self.driver_open:
            try:
                self.api.close_driver()
            except Exception as exc:
                self.log(f"Erro em CloseDriver: {exc}")
        self.api = None
        self.connected = False
        self.driver_open = False
        self.active = []
        self.status.set("Desconectado")
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.configure_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self._save_config()

    def configure(self):
        if not self.connected or not self.api:
            return
        self.active = []
        try:
            module_addr = self._parse_int(self.module_addr.get())
            if not 0 <= module_addr <= 127:
                raise ValueError("O endereço do módulo deve estar entre 0 e 127 (a DLL rejeita >= 0x80).")
            selected = [ch for ch in range(16) if self.channel_vars[ch].get()]
            if not selected:
                raise ValueError("Selecione pelo menos um canal.")
            requested = float(self.sample_rate.get().replace(",", "."))
            if requested <= 0:
                raise ValueError("A frequência deve ser positiva.")

            plan = []
            for ch in selected:
                range_index = self._range_index(ch)
                coef = self._float_field(self.coef_vars[ch], "coeficiente", ch)
                off = self._float_field(self.offset_vars[ch], "offset", ch)
                plan.append((ch, range_index, coef, off))

            self.api.clear_channels()
            for ch, range_index, _c, _o in plan:
                if not self.api.insert_channel(module_addr, ch, range_index):
                    raise RuntimeError(
                        f"Insert_AI_CM falhou (módulo {module_addr}, canal {ch}, faixa {range_index}).")
            ok, actual, error_code = self.api.setup_acquisition(self.external_clock.get(), requested)
            if not ok:
                raise RuntimeError(f"AcquisitionSetup falhou. ErrorCode={error_code}")

            self.active = plan
            self.clear_plot()
            self.log(f"Módulo 0x{module_addr:02X} ({module_addr}); frequência solicitada={requested:g} Hz; "
                     f"informada={actual:g} Hz.")
            for ch, ri, co, of in plan:
                self.log(f"  Canal {ch}: faixa {ri} ({self.RANGE_LABELS[ri]}); "
                         f"valor = {co:g} × V + {of:g} [{self.unit_vars[ch].get()}]")
            self.status.set(f"Configuração aplicada — {actual:g} Hz")
            self._save_config()
        except Exception as exc:
            self.log(f"Erro de configuração: {exc}")
            self._emit("error", stage="configure", message=str(exc))
            messagebox.showerror("Erro de Configuração", str(exc))

    def start(self, confirm=True):
        if not self.connected or not self.api:
            return
        if not any(v.get() for v in self.channel_vars):
            self._emit("error", stage="start", message="nenhum canal selecionado")
            messagebox.showwarning("Aquisição", "Selecione pelo menos um canal.")
            return
        self.configure()
        if not self.active:
            return
        if confirm and not messagebox.askyesno(
                "Iniciar Aquisição",
                "Iniciar aquisição contínua e gravar amostras em CSV?\n"
                "(Também transmitirá via porta COM de forma periódica)"):
            return
        try:
            path = Path(self.csv_path.get()).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.csv_handle = path.open("w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_handle)
            self.csv_writer.writerow(["sample_index", "channel_position", "channel_number",
                                      "raw_adc", "volts", "valor_escalado", "unidade"])
            self.csv_handle.flush()
            if not self.api.start():
                raise RuntimeError("StartAcquisition retornou falso.")
            self.acq_started = True
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.status.set("Aquisição ativa")
            self.log(f"Aquisição iniciada. CSV: {path}")
            self._emit("acq_started", csv=str(path))
            self._poll_samples()
        except Exception as exc:
            self.log(f"Falha ao iniciar: {exc}")
            self._emit("error", stage="start", message=str(exc))
            self._close_csv()
            messagebox.showerror("Falha ao Iniciar Aquisição", str(exc))

    def _run_startup(self):
        """Executa as ações marcadas em 'Ao abrir' (JSON ou linha de comando)."""
        try:
            if self.auto_connect.get() and not self.connected:
                self.connect()
            if self.auto_open_serial.get() and not self.serial_connected:
                self.toggle_serial()
            if self.auto_start.get() and self.connected and not self.acq_started:
                self.start(confirm=False)
        except Exception as exc:
            self.log(f"Erro no início automático: {exc}")

    def _poll_samples(self):
        if not self.acq_started or not self.api:
            return
        try:
            selected = [ch for ch, _r, _c, _o in self.active]
            cal = self._cal()
            ok, error_code, last_sample, nrows, values = self.api.get_samples(len(selected))
            if not ok:
                if error_code == 111:
                    raise RuntimeError("Buffer overrun (ErrorCode 111). Reduza a frequência/canais ou aumente a taxa de leitura.")
                if error_code not in (0, 102, 103):
                    self.log(f"GetSamples retornou erro {error_code}.")
            elif nrows > 0:
                nchan = len(selected)
                if nchan == 0:
                    raise RuntimeError("Nenhum canal selecionado durante a aquisição.")
                plot_ch = self._plot_ch()
                last = None
                for row in range(nrows):
                    sample_idx = last_sample - nrows + 1 + row
                    for col, ch in enumerate(selected):
                        raw = values[row * nchan + col]
                        ridx, coef, off = cal[ch]
                        volts = self.raw_to_volts(raw, ridx)
                        value = coef * volts + off
                        unit = self.unit_vars[ch].get()

                        self.latest_sample_data[ch] = value

                        row_data = [sample_idx, col, ch, raw, f"{volts:.6f}", f"{value:.6f}", unit]

                        if self.csv_writer:
                            self.csv_writer.writerow(row_data)

                        if ch == plot_ch:
                            self.plot_times.append(sample_idx)
                            self.plot_values.append(value)
                            last = (raw, volts, value)

                self.plot_times = self.plot_times[-2000:]
                self.plot_values = self.plot_values[-2000:]
                msg = f"Último índice: {last_sample} | Instantes lidos: {nrows}"
                if last:
                    msg += (f" | Canal {plot_ch}: bruto={last[0]}  {last[1]:.4f} V  "
                            f"→ {last[2]:.4f} {self._plot_unit()}")
                elif plot_ch not in cal:
                    msg += f" | Canal {plot_ch} do gráfico não está ativo"
                self.sample_status.set(msg)
                self.draw_plot()
                if self.csv_handle:
                    self.csv_handle.flush()
            self.poll_after_id = self.root.after(20, self._poll_samples)
        except Exception as exc:
            self.log(f"Erro durante a leitura: {exc}")
            self._emit("error", stage="acquisition", message=str(exc))
            self.stop()
            messagebox.showerror("Erro de Aquisição", str(exc))

    def _close_csv(self):
        if self.csv_handle:
            try:
                self.csv_handle.flush()
                self.csv_handle.close()
            except Exception:
                pass
        self.csv_handle = None
        self.csv_writer = None

    def choose_csv(self):
        path = filedialog.asksaveasfilename(title="Salvar Amostras",
                                            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile="ads2002_samples.csv")
        if path:
            self.csv_path.set(path)

    def stop(self):
        if self.poll_after_id is not None:
            try:
                self.root.after_cancel(self.poll_after_id)
            except Exception:
                pass
            self.poll_after_id = None
        if self.acq_started and self.api:
            try:
                self.api.stop()
                self.log("StopAcquisition executado.")
            except Exception as exc:
                self.log(f"Erro em StopAcquisition: {exc}")
        self.acq_started = False
        self._close_csv()
        self.start_btn.configure(state="normal" if self.connected else "disabled")
        self.stop_btn.configure(state="disabled")
        if self.connected:
            self.status.set("Conectado; aquisição parada")

    def clear_plot(self):
        self.plot_times.clear()
        self.plot_values.clear()
        self.draw_plot()

    def draw_plot(self):
        c = getattr(self, "plot_canvas", None)
        if c is None:
            return
        c.delete("all")
        width, height = max(c.winfo_width(), 300), max(c.winfo_height(), 180)
        left, right, top, bottom = 60, width - 18, 22, height - 38
        if right <= left or bottom <= top:
            return
        sensor = self.sensor_vars[self._plot_ch()].get().strip()
        sensor = "" if sensor in ("", "None") else f" — {sensor}"
        c.create_text(width / 2, 8,
                      text=f"ADS2002 — {self.plot_channel.get()}{sensor} ({self._plot_unit()})",
                      anchor="n", fill="#222")
        if len(self.plot_values) >= 2:
            xs, ys = self.plot_times, self.plot_values
            xmin, xmax = min(xs), max(xs)
            if self.fixed_axis.get():
                ymin, ymax = self._plot_limits()
            else:
                ymin, ymax = min(ys), max(ys)
            if ymin == ymax:
                ymin -= 1e-3
                ymax += 1e-3
            if not self.fixed_axis.get():
                pad = (ymax - ymin) * 0.1
                ymin -= pad
                ymax += pad
            for i in range(5):
                y = top + (bottom - top) * i / 4
                val = ymax - (ymax - ymin) * i / 4
                c.create_line(left, y, right, y, fill="#e5e5e5")
                c.create_text(left - 8, y, text=f"{val:.3f}", anchor="e", fill="#555",
                              font=("Segoe UI", 8))
            span_x = (xmax - xmin) or 1
            coords = []
            for x, y in zip(xs, ys):
                yy = min(max(y, ymin), ymax)
                coords.extend([left + (x - xmin) / span_x * (right - left),
                               bottom - (yy - ymin) / (ymax - ymin) * (bottom - top)])
            c.create_line(*coords, fill="#1769aa", width=2)
        else:
            c.create_text((left + right) / 2, (top + bottom) / 2,
                          text="Aguardando amostras do dispositivo", fill="#777")
        c.create_line(left, top, left, bottom, fill="#333")
        c.create_line(left, bottom, right, bottom, fill="#333")
        c.create_text((left + right) / 2, height - 8, text="Índice da Amostra", anchor="s", fill="#333")

    # ------------------------------------------------------ configuração JSON
    def _collect_config(self) -> dict:
        """Estado atual da interface em um dicionário pronto para JSON."""
        channels = {}
        for ch in range(16):
            channels[str(ch)] = {
                "enabled": bool(self.channel_vars[ch].get()),
                "sensor": self.sensor_vars[ch].get(),
                "unit": self.unit_vars[ch].get(),
                "coefficient": self._num_or_text(self.coef_vars[ch].get(), float_only=True),
                "offset": self._num_or_text(self.offset_vars[ch].get(), float_only=True),
                "range_index": self._range_index(ch),
            }
        return {
            "schema": SCHEMA,
            "version": CFG_VERSION,
            "machine_name": self.machine_name.get().strip(),
            "acquisition": {
                "dll_path": self.dll_path.get(),
                "ip": self.ip.get().strip(),
                "module_addr": self._num_or_text(self.module_addr.get()),
                "sample_rate_hz": self._num_or_text(self.sample_rate.get()),
                "external_clock": bool(self.external_clock.get()),
            },
            "serial": {
                "com_port": self.com_port.get(),
                "baudrate": self._num_or_text(self.com_baudrate.get()),
                "period_ms": self._num_or_text(self.serial_period_ms.get()),
                "tx_channels": [i for i in range(16) if self.serial_ch_vars[i].get()],
            },
            "output": {"csv_path": self.csv_path.get()},
            "ui": {"plot_channel": self._plot_ch()},
            "startup": {
                "auto_connect": bool(self.auto_connect.get()),
                "auto_open_serial": bool(self.auto_open_serial.get()),
                "auto_start": bool(self.auto_start.get()),
            },
            "channels": channels,
        }

    def _apply_config(self, cfg: dict):
        """Aplica um JSON à interface. Aceita o formato novo (v5) e o antigo (plano)."""
        if not isinstance(cfg, dict):
            raise ValueError("JSON inválido: um objeto era esperado na raiz.")
        schema = cfg.get("schema")
        if schema not in (None, SCHEMA):
            raise ValueError(f"Esquema desconhecido: {schema!r} (esperado {SCHEMA!r}).")

        new_fmt = "acquisition" in cfg
        version = cfg.get("version", cfg.get("cfg_version", 1))
        acq = cfg.get("acquisition", cfg)
        ser = cfg.get("serial", cfg)

        if "machine_name" in cfg:
            self.machine_name.set(str(cfg["machine_name"]))
        self.dll_path.set(str(acq.get("dll_path", self.dll_path.get())))
        self.ip.set(str(acq.get("ip", self.ip.get())))
        self.sample_rate.set(str(acq.get("sample_rate_hz", self.sample_rate.get())))
        self.external_clock.set(bool(acq.get("external_clock", False)))
        if new_fmt or version >= 2:        # o "0" gravado pelas versões antigas é ignorado
            self.module_addr.set(str(acq.get("module_addr", self.module_addr.get())))

        self.com_port.set(str(ser.get("com_port", self.com_port.get())))
        self.com_baudrate.set(str(ser.get("baudrate", self.com_baudrate.get())))
        self.serial_period_ms.set(str(ser.get("period_ms", ser.get("serial_period_ms", self.serial_period_ms.get()))))
        if "tx_channels" in ser:
            wanted = {int(c) for c in ser["tx_channels"]}
            for i in range(16):
                self.serial_ch_vars[i].set(i in wanted)
        elif "serial_channels" in ser:      # formato antigo: lista de 16 booleanos
            for i, val in enumerate(ser["serial_channels"][:16]):
                self.serial_ch_vars[i].set(bool(val))

        out = cfg.get("output", {})
        if "csv_path" in out:
            self.csv_path.set(str(out["csv_path"]))

        plot_ch = cfg.get("ui", {}).get("plot_channel")
        if isinstance(plot_ch, int) and 1 <= plot_ch <= 8:
            self.plot_channel.set(f"Canal {plot_ch}")

        st = cfg.get("startup", {})
        self.auto_connect.set(bool(st.get("auto_connect", False)))
        self.auto_open_serial.set(bool(st.get("auto_open_serial", False)))
        self.auto_start.set(bool(st.get("auto_start", False)))

        channels = cfg.get("channels")
        if isinstance(channels, dict):
            for ch in range(16):               # canais ausentes do JSON ficam desativados
                self.channel_vars[ch].set(False)
            for key, value in channels.items():
                ch = int(key)
                if not 0 <= ch < 16:
                    continue
                self.channel_vars[ch].set(bool(value.get("enabled", False)))
                if "sensor" in value:
                    self.sensor_vars[ch].set(str(value["sensor"]))
                if "unit" in value:
                    self.unit_vars[ch].set(str(value["unit"]))
                if "coefficient" in value:
                    self.coef_vars[ch].set(str(value["coefficient"]))
                if "offset" in value:
                    self.offset_vars[ch].set(str(value["offset"]))
                idx = min(max(int(value.get("range_index", 0)), 0), 3)
                self.range_vars[ch].set(f"{idx} — {self.RANGE_LABELS[idx]}")

        self._sync_serial_params()
        self._update_title()

    def _read_config_file(self, path: Path):
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        self._apply_config(cfg)

    def export_config(self):
        name = self.machine_name.get().strip() or "ads2000_config"
        initial = "".join(c if c.isalnum() or c in "-_" else "_" for c in name) + ".json"
        dialog_kw = {}
        if self.config_path:
            dialog_kw = {"initialdir": str(self.config_path.parent), "initialfile": self.config_path.name}
        else:
            dialog_kw = {"initialfile": initial}
        path = filedialog.asksaveasfilename(title="Salvar configuração (JSON)",
                                            defaultextension=".json", filetypes=[("JSON", "*.json")],
                                            **dialog_kw)
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._collect_config(), indent=2, ensure_ascii=False),
                                  encoding="utf-8")
            self.log(f"Configuração salva em {path}")
        except Exception as exc:
            self.log(f"Erro ao salvar configuração: {exc}")
            messagebox.showerror("Salvar configuração", str(exc))

    def import_config(self):
        if self.acq_started:
            messagebox.showwarning("Carregar configuração", "Pare a aquisição antes de carregar uma configuração.")
            return
        path = filedialog.askopenfilename(title="Carregar configuração (JSON)",
                                          filetypes=[("JSON", "*.json"), ("Todos os arquivos", "*.*")])
        if not path:
            return
        try:
            self._read_config_file(Path(path))
            self.active = []
            self.log(f"Configuração carregada de {path}")
            if self.connected:
                self.log("Atenção: IP e DLL só têm efeito após desconectar e conectar novamente; "
                         "canais e frequência são reenviados ao iniciar a aquisição.")
        except Exception as exc:
            self.log(f"Erro ao carregar configuração: {exc}")
            messagebox.showerror("Carregar configuração", str(exc))

    def _save_config(self):
        """Gravação automática do arquivo padrão (desativada quando se usa --config)."""
        if self.config_file is None:
            return
        try:
            self.config_file.write_text(json.dumps(self._collect_config(), indent=2, ensure_ascii=False),
                                        encoding="utf-8")
        except Exception as exc:
            self.log(f"Não foi possível salvar configuração: {exc}")

    def _load_config(self):
        try:
            if self.config_path:
                self._read_config_file(self.config_path)
                self.log(f"Configuração carregada de {self.config_path}")
            elif CONFIG_FILE.exists():
                self._read_config_file(CONFIG_FILE)
        except Exception as exc:
            self.log(f"Não foi possível carregar configuração: {exc}")
            if self.config_path:
                messagebox.showerror("Configuração", f"Falha ao ler {self.config_path}:\n{exc}")

    def close_all(self):
        try:
            self._stop_serial_thread()
            if self.serial_connected and self.serial_conn and self.serial_conn.is_open:
                self.serial_conn.close()
            if self.acq_started:
                self.stop()
            if self.connected:
                self.disconnect()
            elif self.driver_open and self.api:
                self.api.close_driver()
        except Exception as exc:
            self.log(f"Erro ao encerrar: {exc}")
        finally:
            self._emit("closed")
            self.root.destroy()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="ADS2002-IP-B-3 / AC2122 — aquisição")
    p.add_argument("--config", "-c", metavar="ARQUIVO.json",
                   help="carrega esta configuração (e desativa a gravação automática do arquivo padrão)")
    p.add_argument("--auto-connect", action="store_true", help="conecta ao AC2122 ao abrir")
    p.add_argument("--auto-open-serial", action="store_true", help="abre a porta COM ao abrir")
    p.add_argument("--auto-start", action="store_true", help="inicia a aquisição ao abrir (sem confirmação)")
    p.add_argument("--stdin-control", action="store_true",
                   help='encerra de forma limpa ao receber "quit" no stdin (usado pelo Dynomax Launcher)')
    p.add_argument("--no-auto", action="store_true",
                   help="ignora as ações 'Ao abrir' do JSON (modo edição)")
    p.add_argument("--headless", action="store_true", 
                   help="executa em segundo plano ocultando a interface gráfica")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config_path = Path(args.config).expanduser().resolve() if args.config else None
    
    root = tk.Tk()
    
    # --- MASQUAGE TOTAL EN MODE HEADLESS ---
    if getattr(args, "headless", False):
        root.withdraw() # Cache la fenêtre principale immédiatement
    # --------------------------------------
    
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
        
    App(root, config_path=config_path, cli_flags={
        "auto_connect": args.auto_connect,
        "auto_open_serial": args.auto_open_serial,
        "auto_start": args.auto_start,
        "stdin_control": args.stdin_control,
        "no_auto": args.no_auto,
        "headless": getattr(args, "headless", False),
    })
    root.mainloop()


if __name__ == "__main__":
    main()