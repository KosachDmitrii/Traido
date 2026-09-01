#!/bin/sh
# Run npm without Cursor sandbox npm_config_devdir (invalid key warning).
cd "$(dirname "$0")" || exit 1
exec env -u npm_config_devdir -u NPM_CONFIG_DEVDIR npm "$@"
