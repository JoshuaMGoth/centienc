; ¢entien¢ Inno Setup Installer
; Builds a standard Windows Setup .exe that launches the existing install.cmd flow.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName "¢entien¢"
#define MyAppPublisher "Joshua Goth"
#define MyAppURL "https://joshuagoth.com/tools/centienc/"
#define MyAppExeName "centienc-installer.bat"

[Setup]
AppId={{B8F4BC39-1CE0-4D7B-9A53-58B8A0E6B8E7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\centienc-installer
DefaultGroupName=¢entien¢ Installer
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=centienc-installer-setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
Uninstallable=no
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "install.ps1"; DestDir: "{tmp}\centienc-installer"; Flags: ignoreversion deleteafterinstall
Source: "install.cmd"; DestDir: "{tmp}\centienc-installer"; Flags: ignoreversion deleteafterinstall
Source: "centienc-installer.bat"; DestDir: "{tmp}\centienc-installer"; Flags: ignoreversion deleteafterinstall

[Run]
Filename: "{tmp}\centienc-installer\install.cmd"; Description: "Run ¢entien¢ installer"; Flags: waituntilterminated
