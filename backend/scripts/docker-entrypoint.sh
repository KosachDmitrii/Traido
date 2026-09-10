#!/bin/sh
set -e
exec runuser -u traido -- "$@"
