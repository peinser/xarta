#!/bin/bash

set -e

delay=5 # Wait for K8S stdout flush :)

sqitch --chdir "${SQITCH_DIRECTORY:-.}" deploy "db:pg://$POSTGRESQL_CONNECTION_STRING"
