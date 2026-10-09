Output website/public/demo/demo.gif
Output website/public/demo/demo.webm

Require make
Require docker

Set Shell zsh
Set FontSize 14
Set FontFamily "JetBrains Mono, JetBrainsMono Nerd Font, Menlo, monospace"
Set Columns 120
Set Rows 36
Set Padding 20
Set Framerate 10
Set TypingSpeed 35ms
Set Theme { "name": "Open water", "background": "#0B1D2E", "foreground": "#F4F1EA", "cursor": "#1FB6A6", "selection": "#263D52", "black": "#0B1D2E", "red": "#FF6B57", "green": "#1FB6A6", "yellow": "#F5B841", "blue": "#6FA8DC", "magenta": "#C792EA", "cyan": "#8FE3D9", "white": "#CFD3D1", "brightBlack": "#8495A3", "brightRed": "#FF8A78", "brightGreen": "#5CD6C8", "brightYellow": "#FFD070", "brightBlue": "#9CC5EA", "brightMagenta": "#DDB6F2", "brightCyan": "#B5F0E8", "brightWhite": "#F4F1EA" }

Type "cat examples/demo/multihull.yaml"
Enter
Sleep 5s

Type "clear"
Enter
Type "make demo DEMO_ARGS='--duration 54 --scripted --kill-at 10 --restore-at 22'"
Enter
Sleep 66s
