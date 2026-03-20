#!/bin/bash
# Restart MTO via systemd (auto-restarts on crash)
systemctl --user restart mto.service
sleep 1
systemctl --user status mto.service --no-pager | head -5
