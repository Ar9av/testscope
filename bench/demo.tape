# vhs bench/demo.tape  (run from a checkout that has .testscope/map.json; DEMO_ENV sets venv + cwd)
Output assets/demo.gif
Set Shell zsh
Set FontSize 15
Set Width 1180
Set Height 460
Set Padding 18
Set TypingSpeed 35ms
Set Theme "Builtin Light"

Hide
Type "source $DEMO_ENV && clear" Enter
Sleep 1s
Show

Type "git diff --stat" Enter
Sleep 2s
Type "pytest -q -n 4 --tb=no --testscope tests" Enter
Sleep 22s
Type "# same suite without testscope: 6,162 tests, 3m36s" 
Sleep 4s
