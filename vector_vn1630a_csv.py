import csv
import io
import json
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --- Importações opcionais (cada uma separada, para que a falta de uma
#     não desabilite as outras) ---------------------------------------------
try:
    import can
except ImportError:
    can = None

try:
    import serial
except ImportError:
    serial = None

try:
    import winreg  # Somente Windows (usado para listar portas COM)
except ImportError:
    winreg = None


# Número máximo de sinais permitidos
MAX_SIGNALS = 30

# Sinais iniciais com o canal CAN especificado (can_channel: 0 ou 1)
DEFAULT_SIGNALS = [
    dict(pid="PID001", name="RPM", can_channel=0, can_id="0x18FF50E5", extended=True, start_bit=0, length=16, endian="Intel", signed=False, factor=0.125, offset=0, unit="rpm"),
    dict(pid="PID002", name="Torque", can_channel=0, can_id="0x18FF50E5", extended=True, start_bit=16, length=16, endian="Intel", signed=True, factor=0.25, offset=-500, unit="Nm"),
    dict(pid="PID003", name="Vel_veiculo", can_channel=1, can_id="0x0CFEF100", extended=True, start_bit=0, length=16, endian="Intel", signed=False, factor=0.01, offset=0, unit="km/h"),
]


# =============================================================================
# Funções auxiliares de CAN (codificação / decodificação de sinais)
# =============================================================================
def parse_can_id(value):
    """Converte texto ('0x123', '291', ...) em inteiro."""
    return int(str(value).strip(), 0)


def _validate_signal(sig):
    """Valida os parâmetros de um sinal; levanta ValueError se inválido."""
    length = int(sig["length"])
    start = int(sig["start_bit"])
    if not 1 <= length <= 64:
        raise ValueError(f"{sig['name']}: comprimento deve estar entre 1 e 64 bits")
    if start < 0 or start > 63:
        raise ValueError(f"{sig['name']}: start bit deve estar entre 0 e 63")
    if float(sig["factor"]) == 0:
        raise ValueError(f"{sig['name']}: fator não pode ser zero")
    can_id = parse_can_id(sig["can_id"])
    if sig.get("extended", False) and not 0 <= can_id <= 0x1FFFFFFF:
        raise ValueError(f"{sig['name']}: ID estendido inválido")
    if not sig.get("extended", False) and not 0 <= can_id <= 0x7FF:
        raise ValueError(f"{sig['name']}: ID padrão deve ser até 0x7FF")


def _motorola_positions(start, length):
    """
    Lista as posições de bit (na ordem MSB -> LSB) de um sinal Motorola.
    No padrão DBC, o start bit identifica o MSB do sinal.
    """
    pos = start
    positions = []
    for _ in range(length):
        if pos < 0 or pos >= 64:
            raise ValueError("Sinal Motorola excede 8 bytes")
        positions.append(pos)
        pos = pos - 1 if pos % 8 else pos + 15
    return positions


def encode_raw(data, sig, raw):
    """Grava o valor bruto (inteiro) do sinal dentro do bytearray 'data'."""
    length, start = int(sig["length"]), int(sig["start_bit"])
    endian = sig["endian"].lower()
    raw = int(raw) & ((1 << length) - 1)  # trunca para o número de bits (trata negativos)
    if endian == "intel":
        positions = [start + i for i in range(length)]
        for i, pos in enumerate(positions):
            if pos >= 64:
                raise ValueError(f"{sig['name']}: sinal excede 8 bytes")
            mask = 1 << (pos % 8)
            if (raw >> i) & 1:
                data[pos // 8] |= mask
            else:
                data[pos // 8] &= ~mask
    elif endian == "motorola":
        for i, pos in enumerate(_motorola_positions(start, length)):
            mask = 1 << (7 - (pos % 8))
            if (raw >> (length - 1 - i)) & 1:
                data[pos // 8] |= mask
            else:
                data[pos // 8] &= ~mask
    else:
        raise ValueError("Endian deve ser Intel ou Motorola")


def decode_raw(data, sig):
    """Lê o valor bruto (inteiro) do sinal a partir dos bytes recebidos."""
    length, start = int(sig["length"]), int(sig["start_bit"])
    endian = sig["endian"].lower()
    raw = 0
    if endian == "intel":
        for i in range(length):
            pos = start + i
            if pos >= len(data) * 8:
                return None  # frame curto demais para este sinal
            raw |= ((data[pos // 8] >> (pos % 8)) & 1) << i
    elif endian == "motorola":
        for pos in _motorola_positions(start, length):
            if pos >= len(data) * 8:
                return None
            raw = (raw << 1) | ((data[pos // 8] >> (7 - pos % 8)) & 1)
    else:
        raise ValueError("Endian deve ser Intel ou Motorola")
    # Conversão para valor com sinal (complemento de dois)
    if sig.get("signed", False) and raw & (1 << (length - 1)):
        raw -= 1 << length
    return raw


def format_value(v):
    """
    Formata um valor para envio/gravação: 1500.0 -> '1500', 125.5 -> '125.5'.
    Strings (timestamp, can_id, vazio) passam sem alteração.
    """
    if isinstance(v, str):
        return v
    v = float(v)
    if v.is_integer():
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


# =============================================================================
# Aplicação principal
# =============================================================================
class VectorCsvApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Vector VN1630A — CAN ↔ CSV / COM Virtual (Multi-CAN)")
        self.configure(bg="#0b1724")

        # Filas para comunicação thread de trabalho -> interface (thread-safe)
        self.log_queue = queue.Queue()  # mensagens de texto do log
        self.ui_queue = queue.Queue()   # eventos de UI: ("msg"|"status"|"inc", ...)

        self.stop_event = threading.Event()
        self.worker = None
        self.csv_handle = None
        self.csv_writer = None
        self.rx_count = 0
        self.tx_count = 0
        self.signals = [dict(s) for s in DEFAULT_SIGNALS]

        self._style()
        self._build_ui()
        self._populate_table()
        self._fit_window()   # ajusta o tamanho da janela ao conteúdo (evita seções espremidas)
        self.after(100, self._poll_log)
        self._log("Aplicação iniciada. Configure a COM virtual e os canais CAN.")

    def _fit_window(self):
        """
        Calcula o tamanho necessário para mostrar TODAS as seções e usa como
        tamanho mínimo da janela. Funciona com qualquer escala de tela (DPI).
        """
        self.update_idletasks()
        w = max(1080, self.winfo_reqwidth())
        h = self.winfo_reqheight() + 10
        w = min(w, self.winfo_screenwidth() - 40)
        h = min(h, self.winfo_screenheight() - 80)
        self.geometry(f"{w}x{h}")
        self.minsize(min(w, 900), h)

    # -------------------------------------------------------------------------
    # Detecção do hardware Vector
    # -------------------------------------------------------------------------
    def detect_vector(self):
        if can is None:
            self._log("python-can não instalado: py -m pip install python-can")
            return
        try:
            configs = can.detect_available_configs(interfaces=["vector"])
            if configs:
                channels_found = [str(cfg.get("channel", 0)) for cfg in configs]
                channels_found = list(dict.fromkeys(channels_found))

                self.combo_channel1["values"] = channels_found
                self.combo_channel2["values"] = channels_found

                if len(channels_found) > 0:
                    self.channel1.set(channels_found[0])
                if len(channels_found) > 1:
                    self.channel2.set(channels_found[1])

                self.app_name.set(configs[0].get("app_name", "CAN2CSV"))
                self._log(f"Matériel Vector détecté. Canaux disponibles : {channels_found}")
            else:
                self._log("Nenhum hardware Vector detectado.")
        except Exception as e:
            self._log(f"Erro ao detectar Vector: {e}")

    # -------------------------------------------------------------------------
    # Janelas de monitor (CAN, COM e CSV)
    # -------------------------------------------------------------------------
    def _create_monitor_window(self, attr_window, attr_tree, title):
        win = getattr(self, attr_window, None)
        if win is not None and win.winfo_exists():
            win.lift()
            return

        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("600x400")
        win.configure(bg="#0b1724")
        setattr(self, attr_window, win)

        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)

        tree = ttk.Treeview(frame, columns=("timestamp", "source", "details"),
                            show="headings", height=15)
        tree.heading("timestamp", text="Timestamp")
        tree.heading("source", text="Origem")
        tree.heading("details", text="Mensagem / Dados")
        tree.column("timestamp", width=110, anchor="center")
        tree.column("source", width=80, anchor="center")
        tree.column("details", width=370, anchor="w")

        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        setattr(self, attr_tree, tree)

    def open_messages_CAN(self):
        self._create_monitor_window("msg_window_can", "msg_tree_can", "Mensagens CAN (Monitor)")

    def open_messages_COM(self):
        self._create_monitor_window("msg_window_com", "msg_tree_com", "Mensagens COM / CSV (Monitor)")

    def open_csv_file(self):
        csv_path = self.csv_path.get().strip()

        if not Path(csv_path).exists():
            messagebox.showwarning("Aviso", f"O arquivo CSV '{csv_path}' ainda não existe ou não foi gerado.")
            return

        csv_window = tk.Toplevel(self)
        csv_window.title(f"Visualizador CSV — {csv_path}")
        csv_window.geometry("700x450")
        csv_window.configure(bg="#0b1724")

        frame = ttk.Frame(csv_window, padding=8)
        frame.pack(fill="both", expand=True)

        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                rows = list(csv.reader(f))

            if not rows:
                ttk.Label(frame, text="O arquivo CSV está vazio.").pack(padx=10, pady=10)
                return

            headers = rows[0]
            data_rows = rows[1:]

            tree = ttk.Treeview(frame, columns=headers, show="headings", height=18)
            for h in headers:
                tree.heading(h, text=h)
                tree.column(h, width=120, anchor="center")
            for row in data_rows:
                tree.insert("", "end", values=row)

            scroll_y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scroll_y.set)
            tree.pack(side="left", fill="both", expand=True)
            scroll_y.pack(side="right", fill="y")
        except Exception as e:
            messagebox.showerror("Erro", f"Impossível ler o arquivo CSV:\n{e}")

    def _add_received_message(self, source, details):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.ui_queue.put(("msg", source, ts, details))

    # -------------------------------------------------------------------------
    # Estilo visual
    # -------------------------------------------------------------------------
    def _style(self):
        self.option_add("*Font", ("Segoe UI", 10))
        self.option_add("*Background", "#0b1724")
        self.option_add("*Foreground", "#eaf2f9")
        self.option_add("*insertBackground", "#eaf2f9")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background="#0b1724", foreground="#eaf2f9",
                        fieldbackground="#07131f", font=("Segoe UI", 10))
        style.configure("TFrame", background="#0b1724")
        style.configure("TLabelframe", background="#0d1b29",
                        bordercolor="#29465d", relief="solid")
        style.configure("TLabelframe.Label", background="#0d1b29",
                        foreground="#55baff", font=("Segoe UI", 11, "bold"))
        style.configure("TLabel", background="#0d1b29", foreground="#eaf2f9")
        style.configure("TButton", background="#1d3041", foreground="#f3f7fb",
                        padding=(10, 7), bordercolor="#3b5a72",
                        font=("Segoe UI", 10))
        style.map("TButton",
                  background=[("active", "#29465c"), ("pressed", "#142b3d"), ("disabled", "#172735")],
                  foreground=[("disabled", "#8298a9")])
        # Botão compacto (usado na tabela de sinais para economizar altura)
        style.configure("Small.TButton", padding=(8, 3), font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground="#07131f", foreground="#eaf2f9",
                        insertcolor="#eaf2f9", bordercolor="#3b5a72", padding=5)
        style.configure("TCombobox", fieldbackground="#07131f", foreground="#eaf2f9",
                        background="#1d3041", arrowcolor="#eaf2f9", padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", "#07131f")],
                  foreground=[("readonly", "#eaf2f9")])
        style.configure("TCheckbutton", background="#0d1b29", foreground="#eaf2f9")
        style.map("TCheckbutton", background=[("active", "#0d1b29")])
        style.configure("Treeview", background="#07131f", foreground="#eaf2f9",
                        fieldbackground="#07131f", rowheight=29,
                        bordercolor="#233b4d", lightcolor="#233b4d",
                        darkcolor="#233b4d", font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background="#192b3a",
                        foreground="#f0f5fa", relief="flat",
                        font=("Segoe UI", 10))
        style.map("Treeview", background=[("selected", "#14517b")],
                  foreground=[("selected", "#ffffff")])
        # Tabela compacta (linhas mais baixas) para a seção 4
        style.configure("Compact.Treeview", rowheight=22, font=("Segoe UI", 9))
        style.configure("Compact.Treeview.Heading", font=("Segoe UI", 9))
        style.configure("Vertical.TScrollbar", background="#263d50",
                        troughcolor="#07131f", arrowcolor="#dbe7f1")

    # -------------------------------------------------------------------------
    # Construção da interface
    # -------------------------------------------------------------------------
    def _build_ui(self):
        menu_opts = dict(bg="#101f2e", fg="#eaf2f9",
                         activebackground="#1b4e71", activeforeground="#ffffff")
        menubar = tk.Menu(self, relief="flat", borderwidth=0, **menu_opts)
        file_menu = tk.Menu(menubar, tearoff=0, **menu_opts)
        file_menu.add_command(label="Salvar configuração", command=self.save_config)
        file_menu.add_command(label="Carregar configuração", command=self.load_config)
        file_menu.add_separator()
        file_menu.add_command(label="Sair", command=self.destroy)
        settings_menu = tk.Menu(menubar, tearoff=0, **menu_opts)
        settings_menu.add_command(label="Configurações CAN", command=self.save_config)
        help_menu = tk.Menu(menubar, tearoff=0, **menu_opts)
        help_menu.add_command(label="Sobre", command=lambda: messagebox.showinfo(
            "Sobre", "Vector CAN → CSV Configurator\nVN1630A • Multi-CAN • CSV • COM virtual"))
        menubar.add_cascade(label="Arquivo", menu=file_menu)
        menubar.add_cascade(label="Configurações", menu=settings_menu)
        menubar.add_cascade(label="Ajuda", menu=help_menu)
        self.configure(menu=menubar)

        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill="x")

        # ----- 1. Configuração da Vector (Multi-CAN) -----
        vf = ttk.LabelFrame(top, text="1. Configuração da Vector (Multi-CAN)", padding=6)
        vf.pack(side="left", fill="both", expand=True, padx=4)

        self.app_name = tk.StringVar(value="CAN2CSV")

        self.channel1 = tk.StringVar(value="0")
        self.bitrate1 = tk.StringVar(value="500000")

        self.channel2 = tk.StringVar(value="1")
        self.bitrate2 = tk.StringVar(value="250000")

        self._field(vf, "Nome da aplicação", self.app_name, 0)

        ttk.Label(vf, text="CAN 1 Canal / Bitrate").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        f_can1 = ttk.Frame(vf)
        f_can1.grid(row=1, column=1, sticky="ew", padx=4, pady=3)
        self.combo_channel1 = ttk.Combobox(f_can1, textvariable=self.channel1, values=["0", "1", "2", "3"], width=4, state="readonly")
        self.combo_channel1.pack(side="left")
        ttk.Combobox(f_can1, textvariable=self.bitrate1, values=["125000", "250000", "500000", "1000000"], width=9, state="readonly").pack(side="left", padx=(4, 0))

        ttk.Label(vf, text="CAN 2 Canal / Bitrate").grid(row=2, column=0, sticky="w", padx=4, pady=3)
        f_can2 = ttk.Frame(vf)
        f_can2.grid(row=2, column=1, sticky="ew", padx=4, pady=3)
        self.combo_channel2 = ttk.Combobox(f_can2, textvariable=self.channel2, values=["0", "1", "2", "3"], width=4, state="readonly")
        self.combo_channel2.pack(side="left")
        ttk.Combobox(f_can2, textvariable=self.bitrate2, values=["125000", "250000", "500000", "1000000"], width=9, state="readonly").pack(side="left", padx=(4, 0))

        ttk.Button(vf, text="Detectar Hardware Vector", command=self.detect_vector, style="Small.TButton").grid(row=3, column=0, columnspan=2, pady=4)
        self.connection_label = ttk.Label(vf, text="● Desconectado", foreground="#ff6b6b")
        self.connection_label.grid(row=4, column=0, columnspan=2, padx=4, sticky="w")

        # ----- 2. Porta COM virtual / CSV -----
        sf = ttk.LabelFrame(top, text="2. Porta COM virtual / CSV", padding=6)
        sf.pack(side="left", fill="both", expand=True, padx=4)
        self.com_port = tk.StringVar(value="COM24")

        def refresh_ports():
            ports = []
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
                ports = ["Nenhum"]
            combo["values"] = ports
            if not self.com_port.get() or self.com_port.get() not in ports:
                self.com_port.set(ports[0])

        self.baud = tk.StringVar(value="115200")
        self.msg_rate = tk.StringVar(value="100")
        self.csv_path = tk.StringVar(value="dynomax.csv")

        ttk.Label(sf, text="Porta COM").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        combo = ttk.Combobox(sf, textvariable=self.com_port, state="normal", width=12)
        combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=4, pady=2)
        ttk.Button(sf, text="⟳", width=3, command=refresh_ports, style="Small.TButton").grid(row=0, column=3, padx=4)
        refresh_ports()

        # Baud e Msg rate na mesma linha
        ttk.Label(sf, text="Baud").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        baud_combo = ttk.Combobox(sf, textvariable=self.baud, values=["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"], width=10, state="readonly")
        baud_combo.grid(row=1, column=1, sticky="ew", padx=4, pady=2)

        ttk.Label(sf, text="Msg rate (ms)").grid(row=1, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(sf, textvariable=self.msg_rate, width=5).grid(row=1, column=3, sticky="w", padx=4, pady=2)

        row = ttk.Frame(sf)
        row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=2)
        ttk.Label(row, text="CSV").pack(side="left")
        ttk.Entry(row, textvariable=self.csv_path, width=20).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="...", command=self._choose_csv, style="Small.TButton").pack(side="left")

        self.write_header = tk.BooleanVar(value=True)
        self.save_csv_enabled = tk.BooleanVar(value=True)
        self.send_serial_enabled = tk.BooleanVar(value=True)
        self.send_can_enabled = tk.BooleanVar(value=True)

        ttk.Checkbutton(sf, text="Incluir cabeçalho no CSV", variable=self.write_header).grid(row=3, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(sf, text="Salvar dados no CSV", variable=self.save_csv_enabled).grid(row=4, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(sf, text="Propagar para Serial COM (CAN → COM)", variable=self.send_serial_enabled).grid(row=5, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(sf, text="Transferir COM para Vector (COM → CAN)", variable=self.send_can_enabled).grid(row=6, column=0, columnspan=4, sticky="w")

        # ----- 3. Controle da aquisição -----
        cf = ttk.LabelFrame(top, text="3. Controle da aquisição", padding=6)
        cf.pack(side="left", fill="both", expand=True, padx=4)
        ttk.Button(cf, text="▶ Iniciar ponte", command=self.start).grid(row=0, column=0, padx=3, pady=4, sticky="ew")
        ttk.Button(cf, text="■ Parar", command=self.stop).grid(row=0, column=1, padx=3, pady=4, sticky="ew")
        self.rx_label = ttk.Label(cf, text="CAN → COM/CSV: 0")
        self.rx_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=4)
        self.tx_label = ttk.Label(cf, text="COM → CAN: 0")
        self.tx_label.grid(row=2, column=0, columnspan=2, sticky="w", padx=4)
        cf.columnconfigure(0, weight=1)
        cf.columnconfigure(1, weight=1)
        ttk.Button(cf, text="Abrir log Tx", command=self.open_messages_CAN, style="Small.TButton").grid(row=3, column=0, padx=3, pady=2, sticky="ew")
        ttk.Button(cf, text="Abrir log Rx", command=self.open_messages_COM, style="Small.TButton").grid(row=3, column=1, padx=3, pady=2, sticky="ew")
        ttk.Button(cf, text="Abrir log CSV", command=self.open_csv_file, style="Small.TButton").grid(row=4, column=0, columnspan=2, padx=3, pady=2, sticky="ew")

        # ----- 4. Parâmetros CAN (tabela compacta: 3 linhas visíveis + rolagem) -----
        self.pf = ttk.LabelFrame(self, text="", padding=4)
        self.pf.pack(fill="x", expand=False, padx=12, pady=4)

        cols = ("active", "pid", "name", "can_channel", "can_id", "extended", "start", "length", "endian", "signed", "factor", "offset", "unit")
        self.tree = ttk.Treeview(self.pf, columns=cols, show="headings", selectmode="browse",
                                 height=3, style="Compact.Treeview")

        headings = {"active": "Ativo", "pid": "PID", "name": "Nome", "can_channel": "CAN Ch", "can_id": "CAN ID (hex)", "extended": "Ext.",
                    "start": "Start bit", "length": "Bits", "endian": "Endian", "signed": "Signed",
                    "factor": "Fator", "offset": "Offset", "unit": "Unidade"}
        widths = {"active": 50, "pid": 70, "name": 110, "can_channel": 60, "can_id": 110, "extended": 45, "start": 65,
                  "length": 50, "endian": 75, "signed": 55, "factor": 65, "offset": 65, "unit": 70}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="center")
        self.tree.pack(side="left", fill="x", expand=True)

        sb = ttk.Scrollbar(self.pf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="left", fill="y")

        # Botões em grade de 2 colunas (ocupam menos altura que uma pilha de 5)
        controls = ttk.Frame(self.pf)
        controls.pack(side="right", fill="y", padx=6)
        buttons = [("+ Adicionar", self.add_signal), ("− Remover", self.remove_signal),
                   ("Editar", self.edit_signal), ("Salvar config", self.save_config),
                   ("Carregar config", self.load_config)]
        for i, (text, cmd) in enumerate(buttons):
            ttk.Button(controls, text=text, command=cmd, style="Small.TButton").grid(
                row=i // 2, column=i % 2, padx=2, pady=2, sticky="ew")

        # ----- 5. Layouts CSV e 6. Log -----
        bottom = ttk.Frame(self)
        bottom.pack(fill="both", expand=True, padx=12, pady=4)
        proto = ttk.LabelFrame(bottom, text="5. Layouts CSV configuráveis", padding=8)
        proto.pack(side="left", fill="both", expand=True, padx=4)

        self.tx_layout_var = tk.StringVar(value=self._default_input_text())
        self.rx_layout_var = tk.StringVar(value=self._default_output_text())

        # Os dois layouts lado a lado
        layouts_container = ttk.Frame(proto)
        layouts_container.pack(fill="x", pady=(2, 4))

        # Lado esquerdo : COM → CAN
        f_tx = ttk.Frame(layouts_container)
        f_tx.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Label(f_tx, text="Recebido da COM → CAN:").pack(anchor="w", pady=(0, 2))
        # SEM expand=True: o campo mantém a altura normal (antes ficava espremido)
        ttk.Entry(f_tx, textvariable=self.tx_layout_var).pack(fill="x")

        # Lado direito : CAN → COM
        f_rx = ttk.Frame(layouts_container)
        f_rx.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(f_rx, text="Enviado à COM ← CAN:").pack(anchor="w", pady=(0, 2))
        ttk.Entry(f_rx, textvariable=self.rx_layout_var).pack(fill="x")

        layout_buttons = ttk.Frame(proto)
        layout_buttons.pack(fill="x", pady=(6, 2))
        ttk.Button(layout_buttons, text="Preencher entrada", command=self._default_input_layout, style="Small.TButton").pack(side="left", padx=(0, 4))
        ttk.Button(layout_buttons, text="Preencher saída", command=self._default_output_layout, style="Small.TButton").pack(side="left")

        lf = ttk.LabelFrame(bottom, text="6. Log de eventos", padding=8)
        lf.pack(side="left", fill="both", expand=True, padx=4)
        self.log_text = tk.Text(lf, height=6, bg="#07131f", fg="#eaf2f9", insertbackground="white",
                                relief="flat", font=("Consolas", 10), selectbackground="#14517b",
                                highlightthickness=1, highlightbackground="#29465d")
        self.log_text.pack(fill="both", expand=True)

    # -------------------------------------------------------------------------
    # Layouts (entrada / saída)
    # -------------------------------------------------------------------------
    def _pids(self):
        return [s.get("pid", s["name"]) for s in self.signals]

    def _default_input_text(self):
        return ",".join(self._pids())

    def _default_output_text(self):
        return "timestamp_s,can_id," + ",".join(self._pids())

    def _default_input_layout(self):
        self.tx_layout_var.set(self._default_input_text())

    def _default_output_layout(self):
        self.rx_layout_var.set(self._default_output_text())

    def _sync_layouts(self):
        self._default_input_layout()
        self._default_output_layout()

    @staticmethod
    def _parse_layout(layout_text, label):
        fields = [part.strip() for part in layout_text.split(",") if part.strip()]
        if not fields:
            raise ValueError(f"O layout {label} não pode ficar vazio.")
        if len(fields) != len(set(fields)):
            raise ValueError(f"O layout {label} contém nomes duplicados.")
        return fields

    # -------------------------------------------------------------------------
    # Tabela de sinais: adicionar / editar / remover
    # -------------------------------------------------------------------------
    def _field(self, parent, label, var, row, values=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=3)
        if values:
            w = ttk.Combobox(parent, textvariable=var, values=values, width=12, state="readonly")
        else:
            w = ttk.Entry(parent, textvariable=var, width=14)
        w.grid(row=row, column=1, sticky="ew", padx=4, pady=3)
        parent.columnconfigure(1, weight=1)

    def _populate_table(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, s in enumerate(self.signals):
            self.tree.insert("", "end", iid=str(i), values=(
                "Sim", s.get("pid", s["name"]), s["name"], f"CAN {s.get('can_channel', 0)}", s["can_id"],
                "Sim" if s["extended"] else "Não", s["start_bit"], s["length"],
                s["endian"], "Sim" if s["signed"] else "Não",
                s["factor"], s["offset"], s["unit"]))
        self.pf.configure(text=f"4. Parâmetros CAN (Atual {len(self.signals)}; Máximo {MAX_SIGNALS})")

    def _next_pid(self):
        used = {s.get("pid") for s in self.signals}
        n = 1
        while f"PID{n:03d}" in used:
            n += 1
        return f"PID{n:03d}"

    def add_signal(self):
        if len(self.signals) >= MAX_SIGNALS:
            messagebox.showinfo("Limite", f"A aplicação permite até {MAX_SIGNALS} parâmetros.")
            return
        s = dict(pid=self._next_pid(), name=f"param{len(self.signals)+1}", can_channel=0, can_id="0x123",
                 extended=False, start_bit=0, length=8, endian="Intel", signed=False,
                 factor=1.0, offset=0.0, unit="")
        self._edit_dialog(s, new=True)

    def edit_signal(self):
        selected = self.tree.selection()
        if not selected:
            return
        i = int(selected[0])
        self._edit_dialog(dict(self.signals[i]), index=i)

    def _edit_dialog(self, s, index=None, new=False):
        win = tk.Toplevel(self)
        win.title("Parâmetro CAN")
        win.configure(bg="#0b1724")
        win.transient(self)
        win.grab_set()

        fields = [("PID", "pid"), ("Nome", "name"), ("Canal CAN (0 ou 1)", "can_channel"),
                  ("CAN ID (ex.: 0x123)", "can_id"), ("Start bit", "start_bit"),
                  ("Comprimento (bits)", "length"), ("Fator", "factor"),
                  ("Offset", "offset"), ("Unidade", "unit")]
        vars = {}
        for r, (label, key) in enumerate(fields):
            ttk.Label(win, text=label).grid(row=r, column=0, padx=8, pady=5, sticky="w")
            vars[key] = tk.StringVar(value=str(s.get(key, "")))
            ttk.Entry(win, textvariable=vars[key], width=24).grid(row=r, column=1, padx=8, pady=5)

        vars["extended"] = tk.BooleanVar(value=s.get("extended", False))
        vars["signed"] = tk.BooleanVar(value=s.get("signed", False))
        vars["endian"] = tk.StringVar(value=s.get("endian", "Intel"))

        ttk.Checkbutton(win, text="ID estendido (29 bits)", variable=vars["extended"]).grid(row=9, column=0, columnspan=2, sticky="w", padx=8)
        ttk.Checkbutton(win, text="Signed", variable=vars["signed"]).grid(row=10, column=0, columnspan=2, sticky="w", padx=8)
        ttk.Label(win, text="Endian").grid(row=11, column=0, padx=8, pady=5, sticky="w")
        ttk.Combobox(win, textvariable=vars["endian"], values=["Intel", "Motorola"], state="readonly").grid(row=11, column=1, padx=8, pady=5)

        def commit():
            try:
                out = dict(
                    pid=vars["pid"].get().strip(),
                    name=vars["name"].get().strip(),
                    can_channel=int(vars["can_channel"].get().strip()),
                    can_id=vars["can_id"].get().strip(),
                    start_bit=int(vars["start_bit"].get()),
                    length=int(vars["length"].get()),
                    factor=float(vars["factor"].get().replace(",", ".")),
                    offset=float(vars["offset"].get().replace(",", ".")),
                    unit=vars["unit"].get().strip(),
                    extended=vars["extended"].get(),
                    signed=vars["signed"].get(),
                    endian=vars["endian"].get(),
                )
                if not out["name"]:
                    raise ValueError("Informe o nome.")
                if not out["pid"]:
                    raise ValueError("Informe o PID.")
                _validate_signal(out)

                others = [x for i, x in enumerate(self.signals) if new or i != index]
                if out["pid"] in [x.get("pid") for x in others]:
                    raise ValueError("PID duplicado.")
                if out["name"] in [x["name"] for x in others]:
                    raise ValueError("Nome duplicado.")

                if new:
                    self.signals.append(out)
                else:
                    self.signals[index] = out
                self._populate_table()
                self._sync_layouts()
                win.destroy()
            except Exception as e:
                messagebox.showerror("Parâmetro inválido", str(e), parent=win)

        ttk.Button(win, text="Salvar", command=commit).grid(row=12, column=0, padx=8, pady=8)
        ttk.Button(win, text="Cancelar", command=win.destroy).grid(row=12, column=1, padx=8, pady=8)

    def remove_signal(self):
        selected = self.tree.selection()
        if not selected:
            return
        self.signals.pop(int(selected[0]))
        self._populate_table()
        self._sync_layouts()

    def _choose_csv(self):
        p = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv"), ("Todos", "*.*")])
        if p:
            self.csv_path.set(p)

    def _config(self):
        return dict(
            serial_port=self.com_port.get().strip(),
            serial_baud=int(self.baud.get()),
            message_rate_ms=int(self.msg_rate.get().strip() or 0),
            vector_app=self.app_name.get().strip(),
            channels=[
                {"channel": int(self.channel1.get()), "bitrate": int(self.bitrate1.get())},
                {"channel": int(self.channel2.get()), "bitrate": int(self.bitrate2.get())}
            ],
            csv_path=self.csv_path.get().strip(),
            write_header=self.write_header.get(),
            save_csv_enabled=self.save_csv_enabled.get(),
            send_serial_enabled=self.send_serial_enabled.get(),
            send_can_enabled=self.send_can_enabled.get(),
            signals=self.signals,
            com_to_can_layout=self._parse_layout(self.tx_layout_var.get(), "COM → CAN"),
            can_to_com_layout=self._parse_layout(self.rx_layout_var.get(), "CAN → COM"),
        )

    def save_config(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")],
                                         initialfile="vector_csv_config.json")
        if not p:
            return
        try:
            Path(p).write_text(json.dumps(self._config(), indent=2, ensure_ascii=False), encoding="utf-8")
            self._log(f"Configuração salva: {p}")
        except Exception as e:
            messagebox.showerror("Erro", str(e))

    def load_config(self):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            cfg = json.loads(Path(p).read_text(encoding="utf-8"))
            self.signals = cfg.get("signals", [])[:MAX_SIGNALS]
            for i, s in enumerate(self.signals):
                s.setdefault("pid", f"PID{i+1:03d}")
                s.setdefault("can_channel", 0)

            self.app_name.set(cfg.get("vector_app", cfg.get("app_name", "CAN2CSV")))

            chs = cfg.get("channels", [{"channel": 0, "bitrate": 500000}, {"channel": 1, "bitrate": 250000}])
            if len(chs) > 0:
                self.channel1.set(str(chs[0].get("channel", 0)))
                self.bitrate1.set(str(chs[0].get("bitrate", 500000)))
            if len(chs) > 1:
                self.channel2.set(str(chs[1].get("channel", 1)))
                self.bitrate2.set(str(chs[1].get("bitrate", 250000)))

            self.com_port.set(cfg.get("serial_port", "COM24"))
            self.baud.set(str(cfg.get("serial_baud", 115200)))
            self.msg_rate.set(str(cfg.get("message_rate_ms", 100)))
            self.csv_path.set(cfg.get("csv_path", "dynomax.csv"))
            self.write_header.set(cfg.get("write_header", True))
            self.save_csv_enabled.set(cfg.get("save_csv_enabled", True))
            self.send_serial_enabled.set(cfg.get("send_serial_enabled", True))
            self.send_can_enabled.set(cfg.get("send_can_enabled", True))

            pids = self._pids()
            self.tx_layout_var.set(",".join(cfg.get("com_to_can_layout", pids)))
            self.rx_layout_var.set(",".join(cfg.get("can_to_com_layout", ["timestamp_s", "can_id"] + pids)))

            self._populate_table()
            self._log(f"Configuração carregada: {p}")
        except Exception as e:
            messagebox.showerror("Erro ao carregar", str(e))

    def start(self):
        if can is None or serial is None:
            messagebox.showerror("Dependências", "Instale: py -m pip install python-can pyserial")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Ponte", "A ponte já está em execução.")
            return
        try:
            cfg = self._config()
            if not cfg["signals"]:
                raise ValueError("Adicione ao menos um parâmetro.")
            for s in cfg["signals"]:
                _validate_signal(s)
            rate = cfg["message_rate_ms"]
            if rate != 0 and not 20 <= rate <= 60000:
                raise ValueError("Message rate deve ser 0 ou entre 20 e 60000 ms.")

            self.stop_event.clear()
            self.worker = threading.Thread(target=self._bridge_worker, args=(cfg,), daemon=True)
            self.worker.start()
        except Exception as e:
            messagebox.showerror("Configuração inválida", str(e))

    def stop(self):
        self.stop_event.set()
        self._log("Solicitada parada da ponte.")

    # -------------------------------------------------------------------------
    # Thread de trabalho: ponte COM <-> CAN / CSV (Gerencia múltiplos barramentos)
    # -------------------------------------------------------------------------
    def _bridge_worker(self, cfg):
        ser = csv_handle = None
        writer = None
        buses = {}
        try:
            ser = serial.Serial(cfg["serial_port"], cfg["serial_baud"], timeout=0.02, write_timeout=0.5)

            # Inicializa todos os canais configurados
            for ch_cfg in cfg["channels"]:
                ch = ch_cfg["channel"]
                br = ch_cfg["bitrate"]
                buses[ch] = can.Bus(interface="vector", channel=ch,
                                     app_name=cfg["vector_app"], bitrate=br, fd=False)
                self._log(f"Conectado ao Vector canal {ch} @ {br} bit/s")

            self._log(f"Conectado à porta serial: {cfg['serial_port']}")

            input_layout = cfg["com_to_can_layout"]
            output_layout = cfg["can_to_com_layout"]
            save_csv = cfg.get("save_csv_enabled", True)
            send_serial = cfg.get("send_serial_enabled", True)
            send_can = cfg.get("send_can_enabled", True)

            signals_by_name = {}
            for sig in cfg["signals"]:
                signals_by_name[sig["name"]] = sig
                signals_by_name[sig.get("pid", sig["name"])] = sig

            if save_csv:
                path = Path(cfg["csv_path"])
                if str(path.parent) not in ("", "."):
                    path.parent.mkdir(parents=True, exist_ok=True)
                new_file = not path.exists() or path.stat().st_size == 0

                if not new_file and cfg["write_header"]:
                    with open(path, newline="", encoding="utf-8") as f:
                        first = next(csv.reader(f), [])
                    if first != output_layout:
                        backup = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}.bak.csv")
                        path.rename(backup)
                        self._log(f"Cabeçalho do CSV mudou; antigo salvo em {backup.name}")
                        new_file = True

                csv_handle = open(path, "a", newline="", encoding="utf-8")
                writer = csv.writer(csv_handle)
                if new_file and cfg["write_header"]:
                    writer.writerow(output_layout)
                    csv_handle.flush()

            rxbuf = bytearray()
            last_values = {}
            serial_fields = [f for f in output_layout if f not in ("timestamp_s", "can_id")]
            rate_ms = cfg.get("message_rate_ms", 0)
            last_send = time.monotonic()
            sent_state = {"line": None}

            def send_serial_line():
                serial_line = ",".join(format_value(last_values.get(f, 0)) for f in serial_fields)
                payload = (serial_line + "\r\n").encode("ascii", "replace")
                ser.write(payload)
                if rate_ms == 0 or serial_line != sent_state["line"]:
                    sent_state["line"] = serial_line
                    self._log("CAN → COM: " + repr(payload))
                    self._add_received_message("COM", serial_line)

            self._set_status(True)

            while not self.stop_event.is_set():
                # ================= COM -> CAN =================
                chunk = ser.read(ser.in_waiting or 1)
                if chunk:
                    rxbuf.extend(chunk)
                    if len(rxbuf) > 8192:
                        rxbuf.clear()

                    while True:
                        m = re.search(rb"[\r\n]", rxbuf)
                        if not m:
                            break
                        raw = bytes(rxbuf[:m.start()])
                        del rxbuf[:m.end()]
                        line = raw.decode("ascii", "replace").strip()
                        if not line:
                            continue

                        self._add_received_message("COM", line)

                        if send_can:
                            try:
                                fields = next(csv.reader([line]))
                                if [x.strip() for x in fields] == input_layout:
                                    continue
                                if len(fields) != len(input_layout):
                                    continue

                                values_by_name = {name: float(value.strip())
                                                  for name, value in zip(input_layout, fields)}

                                # Agrupa por Canal CAN e ID
                                groups = {}
                                for name, value in values_by_name.items():
                                    if name not in signals_by_name:
                                        continue
                                    sig = signals_by_name[name]
                                    factor = float(sig["factor"])
                                    if factor == 0:
                                        continue
                                    raw_value = round((value - float(sig["offset"])) / factor)
                                    ch = int(sig.get("can_channel", 0))
                                    key = (ch, parse_can_id(sig["can_id"]), bool(sig["extended"]))
                                    if key not in groups:
                                        groups[key] = bytearray(8)
                                    encode_raw(groups[key], sig, raw_value)

                                for (ch, can_id, ext), data in groups.items():
                                    if ch in buses:
                                        buses[ch].send(can.Message(arbitration_id=can_id, is_extended_id=ext,
                                                                   data=bytes(data), check=True), timeout=0.5)
                                        self._log(f"COM → CAN (Ch {ch}): ID 0x{can_id:X}")
                                        self._inc("tx")
                            except Exception as e:
                                self._log("Erro CSV → CAN: " + str(e))

                # ================= CAN -> COM / CSV (Varre todos os canais) =================
                for ch, bus in buses.items():
                    for _ in range(100):
                        msg = bus.recv(timeout=0)
                        if msg is None:
                            break
                        signal_values = {}
                        matched = False
                        for sig in cfg["signals"]:
                            if int(sig.get("can_channel", 0)) == ch and \
                               parse_can_id(sig["can_id"]) == msg.arbitration_id and \
                               bool(sig["extended"]) == msg.is_extended_id:
                                matched = True
                                try:
                                    raw = decode_raw(msg.data, sig)
                                    physical = "" if raw is None else raw * float(sig["factor"]) + float(sig["offset"])
                                except Exception:
                                    physical = ""
                                signal_values[sig["name"]] = physical
                                signal_values[sig.get("pid", sig["name"])] = physical

                        if matched:
                            for k, v in signal_values.items():
                                if v != "":
                                    last_values[k] = v

                            value_map = {"timestamp_s": f"{msg.timestamp:.6f}",
                                         "can_id": f"0x{msg.arbitration_id:X}"}
                            value_map.update(signal_values)
                            row = [format_value(value_map.get(field, "")) for field in output_layout]
                            csv_line = self._csv_line(row)

                            if save_csv and writer is not None:
                                writer.writerow(row)
                                csv_handle.flush()

                            if send_serial and rate_ms == 0:
                                send_serial_line()

                            self._add_received_message(f"CAN Ch {ch}", csv_line)
                            self._inc("rx")

                if send_serial and rate_ms > 0:
                    now = time.monotonic()
                    if now - last_send >= rate_ms / 1000.0:
                        last_send = now
                        send_serial_line()

                time.sleep(0.005)

        except Exception as e:
            self._log("ERRO na ponte: " + repr(e))
        finally:
            try:
                if csv_handle:
                    csv_handle.close()
                if ser and ser.is_open:
                    ser.close()
                for bus in buses.values():
                    bus.shutdown()
            except Exception:
                pass
            self._set_status(False)
            self._log("Ponte encerrada.")

    def _csv_line(self, row):
        s = io.StringIO()
        csv.writer(s, lineterminator="").writerow(row)
        return s.getvalue()

    def _set_status(self, active):
        self.ui_queue.put(("status", active))

    def _inc(self, which):
        self.ui_queue.put(("inc", which))

    def _log(self, msg):
        self.log_queue.put(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}")

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass

        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]

                if kind == "msg":
                    _, source, ts, details = item
                    attr = "msg_tree_can" if "CAN" in source else "msg_tree_com"
                    tree = getattr(self, attr, None)
                    if tree is not None and tree.winfo_exists():
                        tree.insert("", 0, values=(ts, source, details))
                        kids = tree.get_children()
                        if len(kids) > 150:
                            tree.delete(kids[-1])

                elif kind == "status":
                    active = item[1]
                    self.connection_label.configure(
                        text="● Conectado" if active else "● Desconectado",
                        foreground="#55e887" if active else "#ff6b6b")

                elif kind == "inc":
                    if item[1] == "rx":
                        self.rx_count += 1
                        self.rx_label.configure(text=f"CAN → COM/CSV: {self.rx_count}")
                    else:
                        self.tx_count += 1
                        self.tx_label.configure(text=f"COM → CAN: {self.tx_count}")
        except queue.Empty:
            pass

        self.after(100, self._poll_log)


if __name__ == "__main__":
    app = VectorCsvApp()
    app.mainloop()