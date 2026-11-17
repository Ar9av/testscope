# vhs bench/demo.tape  (run from a checkout that has .testscope/map.json; DEMO_ENV sets venv + cwd)
Output assets/demo.gif
Set Shell zsh
Set FontSize 15
Set Width 1180
Set Height 620
Set Padding 18
Set TypingSpeed 35ms
Set Theme "Builtin Light"

Hide
Type "source $DEMO_ENV && clear" Enter
Sleep 1s
Show

Type "git diff --stat" Enter
Sleep 2s
Type "pytest -q -n 4 --testscope tests" Enter
Wait+Screen@120s /testscope: ran|ran \d+ of/
Sleep 4s
