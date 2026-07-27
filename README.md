# GEHAKA G2000 - Monitor e Impressão de Tickets

Aplicação Windows para leitura serial do medidor GEHAKA G2000 e impressão automática de tickets.

## Visão geral

O programa foi desenhado para operação simples:

1. Lê `config.json` no diretório de execução.
2. Conecta na porta COM configurada.
3. Recebe a medição do G2000.
4. Formata o ticket e imprime automaticamente.
5. Em caso de perda de conexão, tenta reconectar periodicamente na mesma porta.

Além disso, ele roda em segundo plano com ícone na Área de Notificação (System Tray).

## Funcionalidades atuais

1. Conexão serial fixa por `com_port` no `config.json`.
2. Impressão por compartilhamento SMB (padrão) com fallback de envio.
3. Reimpressão manual da última medição na tela principal.
4. Armazenamento de tickets em rotação (5 arquivos).
5. Logs de execução com retenção dos 10 mais recentes.
6. Janela de manutenção (Ctrl+Shift+F12) para configuração técnica.
7. Execução em System Tray com menu:
: Abrir painel
: Ocultar painel
: Manutenção
: Sair

## Estrutura de pastas geradas em runtime

As pastas são criadas ao lado do executável (`.exe`) quando o app roda empacotado:

1. `logs/`
: Arquivos `execucao_YYYYMMDD_HHMMSS.log` (retenção: 10)
2. `tickets/`
: `TicketGerado_01..05.txt`
: `TicketGerado_preview_01..05.txt`

## Requisitos

1. Windows
2. Python 3.10+
3. Acesso à porta serial do GEHAKA G2000
4. Impressora compartilhada SMB ou impressora TCP/IP

## Dependências

Arquivo [requirements.txt](requirements.txt):

1. `pyserial>=3.5`
2. `pywin32>=308`
3. `Pillow>=12.0.0`
4. `pystray>=0.19.5`

## Instalação (modo Python)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Execução (modo Python)

```bash
python gehaka_monitor_diag2.py
```

## Configuração

Arquivo `config.json` esperado:

Campos:

1. `com_port`: porta serial fixa do G2000.
2. `baudrate`: velocidade serial.
3. `serial_read_timeout_seconds`: tempo máximo de espera por bytes na leitura serial. O padrão atual é `0.5`.
4. `packet_gap_timeout_seconds`: tempo máximo de silêncio entre bytes antes de o programa considerar o pacote concluído. O padrão atual é `1.5`.
5. `printer_mode`: `shared` ou `tcp`.
6. `printer_path`: caminho UNC quando `shared`.
7. `printer_ip` e `printer_port`: usados quando `tcp`.

Se o G2000 estiver em cabo USB longo ou com resposta mais lenta, aumente primeiro `packet_gap_timeout_seconds`. Só reduza `serial_read_timeout_seconds` se quiser que o loop acorde mais rapidamente para verificar novos dados.

## Atalhos

1. `Ctrl+Shift+F12`: abrir manutenção
2. `Ctrl+Shift+M`: mostrar/ocultar painel principal

## Compilação do executável

Use este procedimento na máquina de desenvolvimento.

1. Abra o terminal na pasta do projeto.
2. Ative o ambiente virtual.
3. Instale/atualize dependências.
4. Gere o executável com PyInstaller.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean gehaka_monitor_diag2.spec
```

Resultado esperado:

1. Executável em [dist/gehaka_monitor_diag2.exe](dist/gehaka_monitor_diag2.exe)
2. Ícone embutido a partir de Icon.png

Se quiser validar rapidamente antes de entregar:

1. Execute [dist/gehaka_monitor_diag2.exe](dist/gehaka_monitor_diag2.exe)
2. Confirme abertura em segundo plano (System Tray)
3. Abra Manutenção e verifique COM e impressora

## Implantação no computador do cliente

Use este checklist ao instalar no cliente.

1. Criar pasta de instalação, por exemplo: C:\GehakaMonitor
2. Copiar para essa pasta:
: [dist/gehaka_monitor_diag2.exe](dist/gehaka_monitor_diag2.exe)
: [config.json](config.json)
: Icon.png (opcional, recomendado manter junto)
3. Garantir permissões de leitura/escrita na pasta (o app cria logs e tickets localmente)
4. Conectar o GEHAKA G2000 via USB e identificar a porta COM no Windows
5. Garantir acesso à impressora:
: Modo compartilhado: validar caminho UNC no formato \\servidor\impressora
: Modo TCP: validar IP e porta da impressora
6. Executar o programa e abrir Manutenção (Ctrl+Shift+F12)
7. Ajustar `com_port`, modo de impressora e destino
8. Aplicar ajustes e executar teste de impressão
9. Realizar uma medição real e confirmar:
: impressão do ticket
: criação da pasta logs
: criação da pasta tickets
10. (Opcional) Criar atalho na pasta Inicializar do Windows para iniciar com o sistema

Observações para suporte:

1. Logs ficam em logs\, ao lado do executável
2. Tickets de rotação ficam em tickets\, ao lado do executável
3. Em caso de falha, abrir Manutenção e revisar o último erro e os logs

## Operação no dia a dia

1. Abra o programa.
2. Se necessário, ajuste COM e impressora na manutenção.
3. Deixe o app rodando em segundo plano (System Tray).
4. Quando chegar medição, a impressão é automática.
5. Se a impressora falhar, use “Reimprimir última medição”.

## Arquivos principais

1. [gehaka_monitor_diag2.py](gehaka_monitor_diag2.py): aplicação principal
2. [gehaka_monitor_diag2.spec](gehaka_monitor_diag2.spec): build PyInstaller
3. [requirements.txt](requirements.txt): dependências
4. [config.json](config.json): configuração runtime
