#!/bin/sh
# Used by SSH when it needs a passphrase.
# Prefer elicited passphrase (this run only) else env SSH_KEY_PASSPHRASE.
echo "${GIT_MCP_SSH_PASSPHRASE:-$SSH_KEY_PASSPHRASE}"
