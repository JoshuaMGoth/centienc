; ══════════════════════════════════════════════════════════════════
;  ¢entien¢ — Inno Setup Installer Script
;
;  Builds a standard Windows Setup .exe that bundles the PowerShell
;  installer and runs it with admin privileges.
;
;  Build: Run build-exe.ps1 or ISCC.exe centienc-installer.iss
; ══════════════════════════════════════════════════════════════════

#define MyAppName "centient"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Joshua Goth"
#define MyAppURL "https://github.com/joshuagoth/centient"

[Setup]
AppId={{7A8C3D2E-4F5B-6A1D-8E9F-0C1B2A3D4E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputBaseFilename=centient-setup-{#MyAppVersion}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
SetupIconFile=compiler:SetupClassicIcon.ico
UninstallDisplayName={#MyAppName}
CreateUninstallRegKey=no

[Files]
Source: "install.ps1"; DestDir: "{tmp}"; Flags: ignoreversion deleteafterinstall
Source: "install.cmd"; DestDir: "{tmp}"; Flags: ignoreversion deleteafterinstall
Source: "..\..\centient\*"; DestDir: "{tmp}\centient\centient"; Flags: ignoreversion recursesubdirs createallsubdirs deleteafterinstall
Source: "..\..\pyproject.toml"; DestDir: "{tmp}\centient"; Flags: ignoreversion deleteafterinstall
Source: "..\..\requirements.txt"; DestDir: "{tmp}\centient"; Flags: ignoreversion deleteafterinstall
Source: "..\..\README.md"; DestDir: "{tmp}\centient"; Flags: ignoreversion deleteafterinstall

[Run]
Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -File ""{tmp}\install.ps1"""; \
    StatusMsg: "Installing centient..."; \
    Flags: runhidden waituntilterminated

[Messages]
WelcomeLabel1=centient Server Monitor
WelcomeLabel2=This will install centient v{#MyAppVersion} on your computer.%n%ncentient is a professional server, service, and website monitoring dashboard that runs as a background service.%n%nAfter installation, open the dashboard URL shown in the terminal to complete setup.
