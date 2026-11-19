' purin voice TTS: start hidden at login (scheduled task "purin-tts").
' Window style 0 = hidden. Wait=True keeps this wscript alive while the bat loop runs,
' so the task stays "Running" and MultipleInstances=IgnoreNew blocks a second copy.
Dim sh, fso, dir
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "cmd.exe /c """"" & dir & "\start-purin-tts.bat""""", 0, True
