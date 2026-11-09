DYNOMAX LAUNCHER — LABORATÓRIO DE ENSAIO DE MOTORES
Versão 1.0.0 — Protótipo visual

CONTEÚDO
- dynomax_launcher_visual.py : aplicação gráfica em Python/Tkinter
- assets/logo_lem_simbolo.png : logotipo do laboratório sem escritas
- assets/icons/dynomax_launcher.ico : ícone Windows
- assets/icons/dynomax_launcher.png : ícone PNG
- requirements.txt : dependência opcional/necessária para exibir o logotipo
- executar_launcher.bat : inicialização simples no Windows

REQUISITOS
- Windows 10/11
- Python 3.10 ou superior com Tkinter
- Pillow (para carregar o logotipo PNG)

INSTALAÇÃO
1. Extraia todo o conteúdo do ZIP para uma pasta.
2. Abra o Prompt de Comando nessa pasta.
3. Execute: python -m pip install -r requirements.txt
4. Execute com duplo clique em executar_launcher.bat ou rode:
   python dynomax_launcher_visual.py

IMPORTANTE
Esta versão é somente visual. Não verifica nem abre a Vector VN1630A,
não abre canais CAN, não conecta a porta serial, ADS2000 ou CAN Module,
e não inicia o software Dynomax. Os botões de ação são demonstrativos.
