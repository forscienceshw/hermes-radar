Option Explicit
' Launcher shim. All paths derive from this file's own folder, so the whole
' directory can be moved or copied to another machine as-is.
' Keep this file ASCII-only: VBScript reads .vbs as ANSI, UTF-8 Chinese would garble.
'
' Deliberately thin. It hands over to radar_tray.pyw, which starts supervisor,
' opens the app window and keeps a tray icon: closing the window leaves the
' service running, double-clicking the tray icon reopens it. If the tray
' script is missing, fall back to supervisor.pyw directly.
Dim shell, fso, base, pythonw, supervisor, tray
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base

pythonw = base & "\.venv\Scripts\pythonw.exe"
supervisor = base & "\supervisor.pyw"
tray = base & "\radar_tray.pyw"

If Not fso.FileExists(pythonw) Then
    MsgBox "Runtime not installed yet." & vbCrLf & vbCrLf & _
           "Open a terminal in this folder and run:" & vbCrLf & _
           "    python install.py", 48, "Hermes Research Radar"
    WScript.Quit 1
End If

If fso.FileExists(tray) Then
    shell.Run """" & pythonw & """ """ & tray & """", 0, False
    WScript.Quit 0
End If

If Not fso.FileExists(supervisor) Then
    MsgBox "supervisor.pyw is missing - this folder looks incomplete.", 16, "Hermes Research Radar"
    WScript.Quit 1
End If

shell.Run """" & pythonw & """ """ & supervisor & """", 0, False
