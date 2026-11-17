using 'main.bicep'

param location = 'italynorth'
param appName = 'bot-colletta'
// 'bfdc1a' lines up with the hand-made resources. Run `what-if` first and read infra/README.md.
param nameSuffix = 'bfdc1a'
param whatsappEnabled = false
// Object id printed by scripts/setup-oidc.sh; lets CI read the Telegram token to refresh the command menu.
param deployerPrincipalId = ''
