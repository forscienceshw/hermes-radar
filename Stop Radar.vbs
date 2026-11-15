Option Explicit
' Launcher shim only. ASCII-only on purpose (see Start Radar.vbs).
Dim shell, fso, base, pythonw, stopper
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base

pythonw = base & "\.venv\Scripts\pythonw.exe"
stopper = base & "\stop_radar.pyw"

If fso.FileExists(pythonw) Then
    shell.Run """" & pythonw & """ """ & stopper & """", 0, True
Else
    Dim stream
    Set stream = fso.CreateTextFile(base & "\data\STOP_RADAR", True)
    stream.Write "stop"
    stream.Close
End If
