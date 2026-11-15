Option Explicit
' Launcher shim. All paths derive from this file's own folder, so the whole
' directory can be moved or copied to another machine as-is.
' Keep this file ASCII-only: VBScript reads .vbs as ANSI, UTF-8 Chinese would garble.
'
' Deliberately thin: it starts supervisor.pyw directly and nothing else. Every
' extra layer here is a place the chain can break on someone else's machine, and
' supervisor.pyw already clears a stale stop-flag itself.
Dim shell, fso, base, pythonw, supervisor
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base

pythonw = base & "\.venv\Scripts\pythonw.exe"
supervisor = base & "\supervisor.pyw"

If Not fso.FileExists(pythonw) Then
    MsgBox "Runtime not installed yet." & vbCrLf & vbCrLf & _
           "Open a terminal in this folder and run:" & vbCrLf & _
           "    python install.py", 48, "Hermes Research Radar"
    WScript.Quit 1
End If

If Not fso.FileExists(supervisor) Then
    MsgBox "supervisor.pyw is missing - this folder looks incomplete.", 16, "Hermes Research Radar"
    WScript.Quit 1
End If

shell.Run """" & pythonw & """ """ & supervisor & """", 0, False
