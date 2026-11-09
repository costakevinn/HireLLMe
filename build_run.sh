#!/usr/bin/env bash
# Rebuilds the image. Only needed when the Dockerfile changes.
docker compose build "$@"