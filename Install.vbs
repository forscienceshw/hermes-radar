Option Explicit
' Double-click entry point for people who do not use a terminal.
' Opens a console window, runs install.py, and keeps the window open so the
' result stays readable. ASCII-only (VBScript reads .vbs as ANSI).
Dim shell, fso, base
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base

If Not fso.FileExists(base & "\install.py") Then
    MsgBox "install.py is missing - this folder looks incomplete.", 16, "Hermes Research Radar"
    WScript.Quit 1
End If

' Prefer the py launcher (handles multiple versions), fall back to python.
' The || chain runs the next command only if the previous one failed.
shell.Run "cmd /k cd /d """ & base & """ && (py -3 install.py || python install.py)", 1, False
