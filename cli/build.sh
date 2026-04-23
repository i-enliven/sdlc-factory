#!/usr/bin/env bash

uv build

sudo -k uv pip install --system --break-system-packages .
