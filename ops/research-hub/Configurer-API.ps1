param(
 [ValidateSet('EIA_API_KEY','CONGRESS_API_KEY','ACLED_ACCESS_TOKEN','UCDP_ACCESS_TOKEN','RELIEFWEB_APPNAME','COINGECKO_DEMO_API_KEY','FOOTBALL_DATA_TOKEN')]
 [string]$Api,
 [string]$LabDirectory = 'C:\Users\Rick\Documents\Vps oracle'
)
$ErrorActionPreference = 'Stop'
$sshArgs = @('-i','./ssh-key-2026-10-14.key','-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=known_hosts','ubuntu@141.253.106.8')
Push-Location -LiteralPath $LabDirectory
try {
 if (-not $Api) {
  & ssh @sshArgs 'sudo -n python3 /srv/lab/projects/research-hub/configure.py status'
  if ($LASTEXITCODE -ne 0) { throw 'Inventaire prive non disponible.' }
  return
 }
 $secure = Read-Host "Valeur privée pour $Api (aucun fichier local créé)" -AsSecureString
 $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
 try {
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
  $payload = @{key=$Api;value=$plain} | ConvertTo-Json -Compress
  $payload | & ssh @sshArgs 'sudo -n python3 /srv/lab/projects/research-hub/configure.py'
  if ($LASTEXITCODE -ne 0) { throw 'Configuration non confirmee. Aucune cle affichee.' }
 } finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
  $plain=$null
  $payload=$null
  $secure.Dispose()
 }
} finally {
 Pop-Location
}
