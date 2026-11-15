Option Explicit
' Launcher shim only. All paths are derived from this file's own folder,
' so the whole directory can be moved or copied to another machine as-is.
' Keep this file ASCII-only: VBScript reads .vbs as ANSI, UTF-8 Chinese would garble.
Dim shell, fso, base, pythonw, launcher
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base

pythonw = base & "\.venv\Scripts\pythonw.exe"
launcher = base & "\start_radar.pyw"

If Not fso.FileExists(pythonw) Then
    MsgBox "Runtime not installed yet." & vbCrLf & vbCrLf & _
           "Open a terminal in this folder and run:" & vbCrLf & _
           "    python install.py", 48, "Hermes Research Radar"
    WScript.Quit 1
End If

shell.Run """" & pythonw & """ """ & launcher & """", 0, False
