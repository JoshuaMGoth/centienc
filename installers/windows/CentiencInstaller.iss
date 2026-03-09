; ¢entien¢ Inno Setup Installer
; Builds a standard Windows Setup .exe that launches the existing install.cmd flow.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName "Centienc"
#define MyAppPublisher "Joshua Goth"
#define MyAppURL "https://joshuagoth.com/tools/centient/"
#define MyAppExeName "Centienc-Installer.bat"

[Setup]
AppId={{B8F4BC39-1CE0-4D7B-9A53-58B8A0E6B8E7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\CentiencInstaller
DefaultGroupName=Centienc Installer
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=Centienc-Installer-Setup
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
Source: "install.ps1"; DestDir: "{tmp}\centient-installer"; Flags: ignoreversion deleteafterinstall
Source: "install.cmd"; DestDir: "{tmp}\centient-installer"; Flags: ignoreversion deleteafterinstall
Source: "Centienc-Installer.bat"; DestDir: "{tmp}\centient-installer"; Flags: ignoreversion deleteafterinstall

[Run]
Filename: "{tmp}\centient-installer\install.cmd"; Description: "Run Centienc installer"; Flags: waituntilterminated
