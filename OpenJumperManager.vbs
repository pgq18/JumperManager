Option Explicit
Dim shell, files, folder, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
command = "python.exe " & Chr(34) & folder & "\app.py" & Chr(34) & " --launch"
On Error Resume Next
Dim code
code = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    MsgBox "Cannot start JumperManager. Run JumperManager.bat to see the error.", 16, "JumperManager"
ElseIf code <> 0 Then
    MsgBox "JumperManager startup failed. See data\launcher.log or run JumperManager.bat.", 16, "JumperManager"
End If
