@echo off
REM pssh - paramiko SSH tool wrapper (for cmd / PowerShell)
REM NOTE: keep this file pure ASCII. cmd.exe parses batch files in the
REM system OEM codepage (GBK on Chinese Windows); UTF-8 comments corrupt
REM the line structure and break execution.
REM %~dp0 = directory of this batch file (with trailing backslash)
python "%~dp0pssh.py" %*
