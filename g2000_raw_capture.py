from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
import time

import serial
import serial.tools.list_ports


DEFAULT_BAUD_RATE = 115200
DEFAULT_OUTPUT_DIR = Path.cwd() / "g2000_capturas"


def list_ports() -> list[str]:
    ports = []
    for port in serial.tools.list_ports.comports():
        description = port.description or ""
        hwid = port.hwid or ""
        ports.append(f"{port.device} | {description} | {hwid}")
    return ports


def choose_port() -> str:
    ports = list_ports()
    if not ports:
        raise RuntimeError("Nenhuma porta serial encontrada.")

    print("Portas seriais encontradas:")
    for index, entry in enumerate(ports, start=1):
        print(f"  {index}. {entry}")

    while True:
        choice = input("Escolha o número da porta: ").strip()
        if not choice.isdigit():
            print("Digite um número válido.")
            continue

        index = int(choice)
        if 1 <= index <= len(ports):
            return ports[index - 1].split(" | ", 1)[0]

        print("Opção fora da faixa.")


def normalize_packet(packet: bytes) -> str:
    text = packet.decode("latin1", errors="replace")
    text = text.replace("\x02", "").replace("\x03", "")
    return text.strip("\r\n")


def write_capture(output_dir: Path, packet_number: int, port: str, baud_rate: int, packet_text: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    file_path = output_dir / f"g2000_{timestamp}_{packet_number:04d}.txt"

    content = (
        f"Porta: {port}\n"
        f"Baud rate: {baud_rate}\n"
        f"Captura: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"\n{packet_text}\n"
    )
    file_path.write_text(content, encoding="latin1", errors="replace")
    return file_path


def capture_loop(port: str, baud_rate: int, output_dir: Path) -> None:
    print(f"Conectando em {port} a {baud_rate} bps...")
    print("Aguardando medições. Pressione Ctrl+C para parar.")

    packet_buffer = bytearray()
    packet_number = 0

    with serial.Serial(
        port=port,
        baudrate=baud_rate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.2,
        xonxoff=False,
        rtscts=False,
    ) as serial_port:
        while True:
            try:
                data = serial_port.read(1)
            except serial.SerialException as exc:
                print(f"Erro na serial: {exc}")
                break

            if not data:
                time.sleep(0.05)
                continue

            packet_buffer.extend(data)

            if packet_buffer.endswith(b"\r\n"):
                packet_number += 1
                raw_packet = bytes(packet_buffer)
                packet_text = normalize_packet(raw_packet)
                file_path = write_capture(output_dir, packet_number, port, baud_rate, packet_text)
                print(f"Captura {packet_number} salva em: {file_path}")
                print(packet_text)
                print("-" * 60)
                packet_buffer.clear()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Captura textos brutos do medidor G2000 e salva em arquivos TXT.")
    parser.add_argument("--port", help="Porta serial, por exemplo COM15.")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD_RATE, help=f"Taxa de transmissão. Padrão: {DEFAULT_BAUD_RATE}.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help=f"Pasta de saída. Padrão: {DEFAULT_OUTPUT_DIR}.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    port = args.port or choose_port()
    output_dir = Path(args.output)

    try:
        capture_loop(port, args.baud, output_dir)
    except KeyboardInterrupt:
        print("\nCaptura encerrada pelo usuário.")
        return 0
    except Exception as exc:
        print(f"Erro: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())